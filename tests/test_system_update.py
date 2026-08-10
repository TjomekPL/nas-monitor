from __future__ import annotations

from unittest.mock import MagicMock, patch

from nas_monitor import system_update


def _find_binary_side_effect(name):
    return f"/usr/bin/{name}" if name in ("apt-get", "apt") else None


def _fake_run_factory(responses):
    """responses: dict mapping a substring of the joined command to a
    (code, stdout, stderr) tuple."""

    def _fake_run(cmd, timeout=8, **kwargs):
        joined = " ".join(cmd)
        for key, value in responses.items():
            if key in joined:
                return value
        return (1, "", f"unmocked command: {joined}")

    return _fake_run


def test_check_for_updates_apt_missing():
    with patch.object(system_update.system_tools, "find_binary", return_value=None):
        result = system_update.check_for_updates()
    assert result == {"available": False, "error_code": "system_update.apt_missing"}


def test_check_for_updates_refresh_failed():
    responses = {"update -qq": (1, "", "network unreachable")}
    with patch.object(system_update.system_tools, "find_binary", side_effect=_find_binary_side_effect), \
         patch.object(system_update.system_tools, "run", _fake_run_factory(responses)):
        result = system_update.check_for_updates()
    assert result["available"] is True
    assert result["error_code"] == "system_update.refresh_failed"


def test_check_for_updates_lists_upgradable_packages():
    apt_list_output = (
        "Listing...\n"
        "openssh-server/stable 1:9.2p1-2 amd64 [upgradable from: 1:9.2p1-1]\n"
        "curl/stable 7.88.1-10 amd64 [upgradable from: 7.88.1-9]\n"
    )
    responses = {
        "update -qq": (0, "", ""),
        "list --upgradable": (0, apt_list_output, ""),
    }
    with patch.object(system_update.system_tools, "find_binary", side_effect=_find_binary_side_effect), \
         patch.object(system_update.system_tools, "run", _fake_run_factory(responses)), \
         patch.object(system_update.os.path, "isfile", return_value=False):
        result = system_update.check_for_updates()
    assert result["available"] is True
    assert result["count"] == 2
    assert result["packages"] == ["openssh-server", "curl"]
    assert result["update_available"] is True
    assert result["reboot_required"] is False


def test_check_for_updates_none_upgradable():
    responses = {
        "update -qq": (0, "", ""),
        "list --upgradable": (0, "Listing...\n", ""),
    }
    with patch.object(system_update.system_tools, "find_binary", side_effect=_find_binary_side_effect), \
         patch.object(system_update.system_tools, "run", _fake_run_factory(responses)), \
         patch.object(system_update.os.path, "isfile", return_value=False):
        result = system_update.check_for_updates()
    assert result["count"] == 0
    assert result["update_available"] is False


def test_check_for_updates_flags_reboot_required():
    responses = {
        "update -qq": (0, "", ""),
        "list --upgradable": (0, "Listing...\n", ""),
    }
    with patch.object(system_update.system_tools, "find_binary", side_effect=_find_binary_side_effect), \
         patch.object(system_update.system_tools, "run", _fake_run_factory(responses)), \
         patch.object(system_update.os.path, "isfile", return_value=True):
        result = system_update.check_for_updates()
    assert result["reboot_required"] is True


def test_check_for_updates_list_failed():
    responses = {
        "update -qq": (0, "", ""),
        "list --upgradable": (1, "", "dpkg error"),
    }
    with patch.object(system_update.system_tools, "find_binary", side_effect=_find_binary_side_effect), \
         patch.object(system_update.system_tools, "run", _fake_run_factory(responses)):
        result = system_update.check_for_updates()
    assert result["error_code"] == "system_update.list_failed"


def test_apply_updates_apt_missing():
    with patch.object(system_update.system_tools, "find_binary", return_value=None), \
         patch.object(system_update.subprocess, "Popen") as mock_popen:
        result = system_update.apply_updates()
    assert result == {"success": False, "error_code": "system_update.apt_missing"}
    mock_popen.assert_not_called()


def test_apply_updates_launches_detached_process():
    with patch.object(system_update.system_tools, "find_binary", side_effect=_find_binary_side_effect), \
         patch.object(system_update.subprocess, "Popen") as mock_popen:
        result = system_update.apply_updates()
    assert result == {"success": True}
    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    shell_command = args[0][-1]
    assert "apt-get" in shell_command
    assert "upgrade -y" in shell_command
    assert system_update.DONE_MARKER in shell_command
    assert kwargs.get("start_new_session") is True


def test_get_progress_no_log_yet():
    with patch("builtins.open", side_effect=OSError("no such file")):
        result = system_update.get_progress()
    assert result == {"done": False, "tail": ""}


def test_get_progress_not_done_yet():
    log_content = "Reading package lists...\nCalculating upgrade...\n"
    with patch("builtins.open", MagicMock(return_value=MagicMock(__enter__=lambda s: MagicMock(read=lambda: log_content), __exit__=lambda *a: None))), \
         patch.object(system_update.os.path, "isfile", return_value=False):
        result = system_update.get_progress()
    assert result["done"] is False
    assert "Calculating upgrade" in result["tail"]


def test_get_progress_done():
    log_content = f"Unpacking curl...\nSetting up curl...\n{system_update.DONE_MARKER}\n"
    with patch("builtins.open", MagicMock(return_value=MagicMock(__enter__=lambda s: MagicMock(read=lambda: log_content), __exit__=lambda *a: None))), \
         patch.object(system_update.os.path, "isfile", return_value=True):
        result = system_update.get_progress()
    assert result["done"] is True
    assert result["reboot_required"] is True
