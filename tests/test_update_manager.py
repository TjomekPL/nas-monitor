from __future__ import annotations

from unittest.mock import MagicMock, patch

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


def test_apply_update_success_skips_pip_when_requirements_unchanged():
    responses = {
        "rev-parse --is-inside-work-tree": (0, "true", ""),
        "rev-parse HEAD": (0, "abc123", ""),
        "fetch --tags --quiet origin main": (0, "", ""),
        "reset --hard origin/main": (0, "", ""),
        "diff --name-only abc123 HEAD": (0, "nas_monitor/static/style.css\n", ""),
        "describe --tags --always --dirty": (0, "v0.4", ""),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", MagicMock(side_effect=_fake_run_factory(responses))) as mock_run, \
         patch.object(update_manager, "_path_exists", return_value=True), \
         patch.object(update_manager.subprocess, "Popen") as mock_popen:
        result = update_manager.apply_update()

    assert result == {"success": True, "version": "v0.4"}
    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]
    assert "systemctl restart nas-monitor" in args[-1]
    # requirements.txt wasn't touched by this update and the venv already
    # exists - pip should never have been invoked at all
    assert not any("pip" in call.args[0][0] for call in mock_run.call_args_list)


def test_apply_update_reinstalls_deps_when_requirements_changed():
    responses = {
        "rev-parse --is-inside-work-tree": (0, "true", ""),
        "rev-parse HEAD": (0, "abc123", ""),
        "fetch --tags --quiet origin main": (0, "", ""),
        "reset --hard origin/main": (0, "", ""),
        "diff --name-only abc123 HEAD": (0, "requirements.txt\n", ""),
        "pip install -q -r": (0, "", ""),
        "describe --tags --always --dirty": (0, "v0.4", ""),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", _fake_run_factory(responses)), \
         patch.object(update_manager, "_path_exists", return_value=True), \
         patch.object(update_manager.subprocess, "Popen") as mock_popen:
        result = update_manager.apply_update()

    assert result == {"success": True, "version": "v0.4"}
    mock_popen.assert_called_once()


def test_apply_update_reinstalls_deps_when_venv_missing():
    responses = {
        "rev-parse --is-inside-work-tree": (0, "true", ""),
        "rev-parse HEAD": (0, "abc123", ""),
        "fetch --tags --quiet origin main": (0, "", ""),
        "reset --hard origin/main": (0, "", ""),
        "diff --name-only abc123 HEAD": (0, "nas_monitor/static/style.css\n", ""),
        "pip install -q -r": (0, "", ""),
        "describe --tags --always --dirty": (0, "v0.4", ""),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", MagicMock(side_effect=_fake_run_factory(responses))) as mock_run, \
         patch.object(update_manager, "_path_exists", side_effect=[True, False]), \
         patch.object(update_manager.subprocess, "Popen") as mock_popen:
        result = update_manager.apply_update()

    assert result == {"success": True, "version": "v0.4"}
    mock_popen.assert_called_once()
    assert any("pip" in call.args[0][0] for call in mock_run.call_args_list)


def test_apply_update_incomplete_checkout_after_reset():
    responses = {
        "rev-parse --is-inside-work-tree": (0, "true", ""),
        "rev-parse HEAD": (0, "abc123", ""),
        "fetch --tags --quiet origin main": (0, "", ""),
        "reset --hard origin/main": (0, "", ""),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", MagicMock(side_effect=_fake_run_factory(responses))), \
         patch.object(update_manager, "_path_exists", return_value=False), \
         patch.object(update_manager.subprocess, "Popen") as mock_popen:
        result = update_manager.apply_update()

    assert result == {"success": False, "error_code": "update.incomplete_checkout"}
    mock_popen.assert_not_called()


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
        "rev-parse HEAD": (0, "abc123", ""),
        "fetch --tags --quiet origin main": (0, "", ""),
        "reset --hard origin/main": (1, "", "conflict"),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", _fake_run_factory(responses)), \
         patch.object(update_manager.subprocess, "Popen") as mock_popen:
        result = update_manager.apply_update()

    assert result == {"success": False, "error_code": "update.apply_failed"}
    mock_popen.assert_not_called()


def test_apply_update_deps_failure_does_not_restart_and_surfaces_detail():
    responses = {
        "rev-parse --is-inside-work-tree": (0, "true", ""),
        "rev-parse HEAD": (0, "abc123", ""),
        "fetch --tags --quiet origin main": (0, "", ""),
        "reset --hard origin/main": (0, "", ""),
        "diff --name-only abc123 HEAD": (0, "requirements.txt\n", ""),
        "pip install -q -r": (1, "", "ERROR: could not find a version that satisfies psutil"),
    }
    with patch.object(update_manager.system_tools, "find_binary", return_value="/usr/bin/git"), \
         patch.object(update_manager.system_tools, "run", _fake_run_factory(responses)), \
         patch.object(update_manager, "_path_exists", return_value=True), \
         patch.object(update_manager.subprocess, "Popen") as mock_popen:
        result = update_manager.apply_update()

    assert result["success"] is False
    assert result["error_code"] == "update.deps_failed"
    assert "psutil" in result["error_context"]["detail"]
    mock_popen.assert_not_called()


def test_git_binary_missing():
    with patch.object(update_manager.system_tools, "find_binary", return_value=None):
        result = update_manager.check_for_update()
    assert result == {"git_managed": False, "current_version": None, "update_available": False}
