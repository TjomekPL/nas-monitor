import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import users  # noqa: E402


class TestValidUsername(unittest.TestCase):
    def test_accepts_normal_names(self):
        for name in ("tomek", "gosia", "backup_user", "svc-share1", "_svc"):
            self.assertTrue(users.is_valid_username(name), name)

    def test_rejects_bad_names(self):
        for name in ("", "Tomek", "1tomek", "tomek user", "tomek;rm -rf /", "a" * 33, "użytkownik"):
            self.assertFalse(users.is_valid_username(name), name)


class TestCreateUser(unittest.TestCase):
    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/useradd")
    @mock.patch("nas_monitor.users.system_tools.run")
    def test_rejects_invalid_username_before_running_anything(self, mock_run, mock_find, mock_exists):
        result = users.create_user("Not Valid!")
        self.assertFalse(result["success"])
        self.assertIn("Nieprawidłowa", result["error"])
        mock_run.assert_not_called()

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    def test_rejects_existing_user(self, mock_exists):
        result = users.create_user("tomek")
        self.assertFalse(result["success"])
        self.assertIn("już istnieje", result["error"])

    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value=None)
    def test_missing_useradd_binary(self, mock_find, mock_exists):
        result = users.create_user("tomek")
        self.assertFalse(result["success"])
        self.assertIn("not installed", result["error"])

    @mock.patch("nas_monitor.users._default_nologin_shell", return_value="/usr/sbin/nologin")
    @mock.patch("nas_monitor.users.ensure_group_exists", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/useradd")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_builds_correct_useradd_command_with_groups(self, mock_run, mock_find, mock_exists, mock_ensure_group, mock_shell):
        result = users.create_user("share1", groups=["storage", "backup"])
        self.assertTrue(result["success"])
        called_cmd = mock_run.call_args[0][0]
        self.assertEqual(called_cmd[0], "/usr/sbin/useradd")
        self.assertIn("-s", called_cmd)
        self.assertIn("/usr/sbin/nologin", called_cmd)
        self.assertIn("-m", called_cmd)
        self.assertIn("-G", called_cmd)
        self.assertIn("storage,backup", called_cmd)
        self.assertEqual(called_cmd[-1], "share1")
        # both groups should have been ensured to exist before useradd ran
        self.assertEqual(mock_ensure_group.call_count, 2)

    @mock.patch("nas_monitor.users._default_nologin_shell", return_value="/usr/sbin/nologin")
    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/useradd")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_group_creation_failure_aborts_before_useradd(self, mock_run, mock_find, mock_exists, mock_shell):
        with mock.patch(
            "nas_monitor.users.ensure_group_exists",
            return_value={"success": False, "error": "groupadd exited 1"},
        ):
            result = users.create_user("share1", groups=["brandnew"])
        self.assertFalse(result["success"])
        self.assertIn("brandnew", result["error"])
        mock_run.assert_not_called()  # useradd must never run if the group step failed

    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/useradd")
    def test_rejects_invalid_group_name(self, mock_find, mock_exists):
        result = users.create_user("share1", groups=["ok_group", "bad group!"])
        self.assertFalse(result["success"])
        self.assertIn("grupy", result["error"])

    @mock.patch("nas_monitor.users._default_nologin_shell", return_value="/usr/sbin/nologin")
    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/useradd")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(1, "", "useradd: some real failure"))
    def test_propagates_useradd_failure(self, mock_run, mock_find, mock_exists, mock_shell):
        result = users.create_user("share1")
        self.assertFalse(result["success"])
        self.assertIn("some real failure", result["error"])


class TestEnsureGroupExists(unittest.TestCase):
    @mock.patch("nas_monitor.users.grp.getgrnam", return_value=object())
    @mock.patch("nas_monitor.users.system_tools.run")
    def test_existing_group_is_a_noop(self, mock_run, mock_getgrnam):
        result = users.ensure_group_exists("existing")
        self.assertTrue(result["success"])
        mock_run.assert_not_called()

    @mock.patch("nas_monitor.users.grp.getgrnam", side_effect=KeyError)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/groupadd")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_creates_missing_group(self, mock_run, mock_find, mock_getgrnam):
        result = users.ensure_group_exists("brandnew")
        self.assertTrue(result["success"])
        mock_run.assert_called_once_with(["/usr/sbin/groupadd", "brandnew"])

    @mock.patch("nas_monitor.users.grp.getgrnam", side_effect=KeyError)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/groupadd")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(1, "", "groupadd: real failure"))
    def test_propagates_groupadd_failure(self, mock_run, mock_find, mock_getgrnam):
        result = users.ensure_group_exists("brandnew")
        self.assertFalse(result["success"])
        self.assertIn("real failure", result["error"])


class TestListSystemUsersAndGroups(unittest.TestCase):
    def test_returns_lists_without_crashing_on_real_system(self):
        # Not mocked on purpose - this sandbox is a real (if minimal) Linux
        # system, so this exercises the real pwd/grp database end to end.
        result = users.list_system_users()
        self.assertIsInstance(result, list)
        result_groups = users.list_system_groups()
        self.assertIsInstance(result_groups, list)


if __name__ == "__main__":
    unittest.main()
