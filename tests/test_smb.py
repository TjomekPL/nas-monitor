import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import smb  # noqa: E402


PDBEDIT_SAMPLE = """\
tomek:1000:Tomek
gosia:1001:Gosia
share1:1010:
"""


class TestListSambaUsers(unittest.TestCase):
    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value="/usr/bin/pdbedit")
    @mock.patch("nas_monitor.smb.system_tools.run")
    def test_parses_pdbedit_output(self, mock_run, mock_find):
        mock_run.return_value = (0, PDBEDIT_SAMPLE, "")
        result = smb.list_samba_users()
        self.assertTrue(result["available"])
        self.assertEqual(result["usernames"], ["gosia", "share1", "tomek"])

    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value=None)
    def test_missing_pdbedit(self, mock_find):
        result = smb.list_samba_users()
        self.assertFalse(result["available"])
        self.assertEqual(result["error_code"], "system.tool_missing")
        self.assertEqual(result["error_context"]["tool"], "pdbedit")

    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value="/usr/bin/pdbedit")
    @mock.patch("nas_monitor.smb.system_tools.run", return_value=(0, "", ""))
    def test_empty_samba_db(self, mock_run, mock_find):
        result = smb.list_samba_users()
        self.assertTrue(result["available"])
        self.assertEqual(result["usernames"], [])


class TestSetPassword(unittest.TestCase):
    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value="/usr/bin/smbpasswd")
    @mock.patch("nas_monitor.smb.system_tools.run", return_value=(0, "", ""))
    def test_sends_password_twice_via_stdin_not_argv(self, mock_run, mock_find):
        result = smb.set_password("share1", "hunter2")
        self.assertTrue(result["success"])
        called_cmd = mock_run.call_args[0][0]
        called_kwargs = mock_run.call_args[1]
        # password must never appear in argv (would leak via `ps`)
        self.assertNotIn("hunter2", called_cmd)
        self.assertEqual(called_kwargs["input_text"], "hunter2\nhunter2\n")
        self.assertIn("-a", called_cmd)
        self.assertIn("share1", called_cmd)

    def test_rejects_empty_password(self):
        result = smb.set_password("share1", "")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "smb.empty_password")

    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value="/usr/bin/smbpasswd")
    @mock.patch("nas_monitor.smb.system_tools.run", return_value=(1, "", "Failed to find entry for user."))
    def test_propagates_failure_eg_no_system_account(self, mock_run, mock_find):
        result = smb.set_password("ghost", "hunter2")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertIn("Failed to find entry", result["error_context"]["detail"])


class TestDisableEnableAccount(unittest.TestCase):
    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value="/usr/bin/smbpasswd")
    @mock.patch("nas_monitor.smb.system_tools.run", return_value=(0, "", ""))
    def test_disable_uses_dash_d(self, mock_run, mock_find):
        result = smb.disable_account("gosia")
        self.assertTrue(result["success"])
        mock_run.assert_called_once_with(["/usr/bin/smbpasswd", "-d", "gosia"])

    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value="/usr/bin/smbpasswd")
    @mock.patch("nas_monitor.smb.system_tools.run", return_value=(0, "", ""))
    def test_enable_uses_dash_e(self, mock_run, mock_find):
        result = smb.enable_account("gosia")
        self.assertTrue(result["success"])
        mock_run.assert_called_once_with(["/usr/bin/smbpasswd", "-e", "gosia"])

    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value="/usr/bin/smbpasswd")
    @mock.patch("nas_monitor.smb.system_tools.run", return_value=(1, "", "Failed to find entry for user."))
    def test_disable_propagates_failure(self, mock_run, mock_find):
        result = smb.disable_account("ghost")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")

    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value=None)
    def test_disable_reports_missing_tool(self, mock_find):
        result = smb.disable_account("gosia")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.tool_missing")


class TestGetAccountFlags(unittest.TestCase):
    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value="/usr/bin/pdbedit")
    @mock.patch("nas_monitor.smb.system_tools.run")
    def test_disabled_account(self, mock_run, mock_find):
        mock_run.return_value = (0, "username:       gosia\nAccount Flags:  [DU         ]\n", "")
        result = smb.get_account_flags("gosia")
        self.assertTrue(result["disabled"])

    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value="/usr/bin/pdbedit")
    @mock.patch("nas_monitor.smb.system_tools.run")
    def test_enabled_account(self, mock_run, mock_find):
        mock_run.return_value = (0, "username:       gosia\nAccount Flags:  [U          ]\n", "")
        result = smb.get_account_flags("gosia")
        self.assertFalse(result["disabled"])

    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value="/usr/bin/pdbedit")
    @mock.patch("nas_monitor.smb.system_tools.run")
    def test_never_requests_password_hashes(self, mock_run, mock_find):
        mock_run.return_value = (0, "username:       gosia\nAccount Flags:  [U          ]\n", "")
        smb.get_account_flags("gosia")
        called_cmd = mock_run.call_args[0][0]
        self.assertNotIn("-w", called_cmd)

    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value="/usr/bin/pdbedit")
    @mock.patch("nas_monitor.smb.system_tools.run", return_value=(1, "", "user not found"))
    def test_no_smb_account_reports_not_disabled(self, mock_run, mock_find):
        result = smb.get_account_flags("nosmbuser")
        self.assertFalse(result["disabled"])

    @mock.patch("nas_monitor.smb.system_tools.find_binary", return_value=None)
    def test_missing_pdbedit_reports_not_disabled_not_raises(self, mock_find):
        result = smb.get_account_flags("gosia")
        self.assertFalse(result["disabled"])


if __name__ == "__main__":
    unittest.main()
