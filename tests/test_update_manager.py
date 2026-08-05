from __future__ import annotations

from unittest.mock import patch

from nas_monitor import update_manager


def _fake_run_factory(responses):
    """responses: dict mapping a tuple of args (after -C APP_DIR) to a
    (code, stdout, stderr) tuple, matched by prefix."""

    def _fake_run(cmd, timeout=8, **kwargs):
        joined = " ".join(cmd)
        for key, value in responses.items():
            if key in joined:
                return value
        return (1, "", f"unmocked command: {joined}")

    return _fake_run


def test_check_for_update_not_git_managed():
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", _fake_run_factory({
             "rev-parse --is-inside-work-tree": (1, "", "not a git repo"),
         })):
        result = update_manager.check_for_update()

    assert result == {"git_managed": False, "current_version": None, "update_available": False}


def test_check_for_update_reports_available():
    responses = {
        "rev-parse --is-inside-work-tree": (0, "true", ""),
        "describe --tags --always --dirty": (0, "v0.3", ""),
        "fetch --tags --quiet origin main": (0, "", ""),
        "describe --tags --always origin/main": (0, "v0.4", ""),
        "rev-list --count HEAD..origin/main": (0, "3", ""),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", _fake_run_factory(responses)):
        result = update_manager.check_for_update()

    assert result["git_managed"] is True
    assert result["current_version"] == "v0.3"
    assert result["latest_version"] == "v0.4"
    assert result["update_available"] is True
    assert result["commits_behind"] == 3


def test_check_for_update_up_to_date():
    responses = {
        "rev-parse --is-inside-work-tree": (0, "true", ""),
        "describe --tags --always --dirty": (0, "v0.4", ""),
        "fetch --tags --quiet origin main": (0, "", ""),
        "describe --tags --always origin/main": (0, "v0.4", ""),
        "rev-list --count HEAD..origin/main": (0, "0", ""),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", _fake_run_factory(responses)):
        result = update_manager.check_for_update()

    assert result["update_available"] is False
    assert result["commits_behind"] == 0


def test_check_for_update_fetch_failed():
    responses = {
        "rev-parse --is-inside-work-tree": (0, "true", ""),
        "describe --tags --always --dirty": (0, "v0.3", ""),
        "fetch --tags --quiet origin main": (1, "", "network unreachable"),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", _fake_run_factory(responses)):
        result = update_manager.check_for_update()

    assert result["git_managed"] is True
    assert result["update_available"] is False
    assert result["error_code"] == "update.fetch_failed"


def test_apply_update_success_schedules_restart_and_reinstalls_deps():
    responses = {
        "rev-parse --is-inside-work-tree": (0, "true", ""),
        "fetch --tags --quiet origin main": (0, "", ""),
        "reset --hard origin/main": (0, "", ""),
        "pip install -q -r": (0, "", ""),
        "describe --tags --always --dirty": (0, "v0.4", ""),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", _fake_run_factory(responses)), \
         patch.object(update_manager.subprocess, "Popen") as mock_popen:
        result = update_manager.apply_update()

    assert result == {"success": True, "version": "v0.4"}
    mock_popen.assert_called_once()
    # restart is scheduled via a detached shell command, not a direct
    # systemctl call from this process
    args = mock_popen.call_args[0][0]
    assert "systemctl restart nas-monitor" in args[-1]


def test_apply_update_not_git_managed():
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", _fake_run_factory({
             "rev-parse --is-inside-work-tree": (1, "", ""),
         })):
        result = update_manager.apply_update()

    assert result == {"success": False, "error_code": "update.not_git_managed"}


def test_apply_update_reset_failure_does_not_restart():
    responses = {
        "rev-parse --is-inside-work-tree": (0, "true", ""),
        "fetch --tags --quiet origin main": (0, "", ""),
        "reset --hard origin/main": (1, "", "conflict"),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", _fake_run_factory(responses)), \
         patch.object(update_manager.subprocess, "Popen") as mock_popen:
        result = update_manager.apply_update()

    assert result == {"success": False, "error_code": "update.apply_failed"}
    mock_popen.assert_not_called()


def test_apply_update_deps_failure_does_not_restart():
    responses = {
        "rev-parse --is-inside-work-tree": (0, "true", ""),
        "fetch --tags --quiet origin main": (0, "", ""),
        "reset --hard origin/main": (0, "", ""),
        "pip install -q -r": (1, "", "broken package"),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", _fake_run_factory(responses)), \
         patch.object(update_manager.subprocess, "Popen") as mock_popen:
        result = update_manager.apply_update()

    assert result == {"success": False, "error_code": "update.deps_failed"}
    mock_popen.assert_not_called()


def test_git_binary_missing():
    with patch.object(update_manager.system_tools, "find_binary", return_value=None):
        result = update_manager.check_for_update()
    assert result == {"git_managed": False, "current_version": None, "update_available": False}
