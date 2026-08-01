import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import ssh_keys  # noqa: E402


def _fake_pwent(name, shell, uid=1000, home=None):
    import pwd
    home = home or f"/home/{name}"
    return pwd.struct_passwd((name, "x", uid, uid, "", home, shell))


class TestGetKeyStatus(unittest.TestCase):
    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam", side_effect=KeyError)
    def test_rejects_nonexistent_user(self, mock_pwnam):
        result = ssh_keys.get_key_status("ghost")
        self.assertFalse(result["has_key"])
        self.assertIn("nie istnieje", result["error"])

    @mock.patch("nas_monitor.ssh_keys.os.path.isfile", return_value=False)
    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    def test_no_key_yet(self, mock_pwnam, mock_isfile):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash")
        result = ssh_keys.get_key_status("tomek")
        self.assertFalse(result["has_key"])
        self.assertIsNone(result["public_key"])
        self.assertTrue(result["can_login"])

    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    def test_nologin_account_flagged(self, mock_pwnam):
        mock_pwnam.return_value = _fake_pwent("share1", "/usr/sbin/nologin")
        with mock.patch("nas_monitor.ssh_keys.os.path.isfile", return_value=False):
            result = ssh_keys.get_key_status("share1")
        self.assertFalse(result["can_login"])


class TestGenerateKey(unittest.TestCase):
    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam", side_effect=KeyError)
    def test_rejects_nonexistent_user(self, mock_pwnam):
        result = ssh_keys.generate_key("ghost")
        self.assertFalse(result["success"])

    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    def test_rejects_nologin_account(self, mock_pwnam):
        mock_pwnam.return_value = _fake_pwent("share1", "/usr/sbin/nologin")
        result = ssh_keys.generate_key("share1")
        self.assertFalse(result["success"])
        self.assertIn("nologin", result["error"])

    @mock.patch("nas_monitor.ssh_keys.os.path.isfile", return_value=True)
    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    def test_refuses_to_overwrite_existing_key(self, mock_pwnam, mock_isfile):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash")
        result = ssh_keys.generate_key("tomek")
        self.assertFalse(result["success"])
        self.assertIn("już istnieje", result["error"])

    @mock.patch("nas_monitor.ssh_keys.system_tools.find_binary", return_value=None)
    @mock.patch("nas_monitor.ssh_keys.os.chmod")
    @mock.patch("nas_monitor.ssh_keys.os.chown")
    @mock.patch("nas_monitor.ssh_keys.os.makedirs")
    @mock.patch("nas_monitor.ssh_keys.os.path.isfile", return_value=False)
    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    def test_missing_ssh_keygen(self, mock_pwnam, mock_isfile, mock_makedirs, mock_chown, mock_chmod, mock_find):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash")
        result = ssh_keys.generate_key("tomek")
        self.assertFalse(result["success"])
        self.assertIn("not installed", result["error"])


class TestDeployKeyToRemote(unittest.TestCase):
    @mock.patch("nas_monitor.ssh_keys.get_key_status", return_value={"has_key": True})
    def test_rejects_empty_password(self, mock_status):
        result = ssh_keys.deploy_key_to_remote("tomek", "192.168.0.20", "wieslaw", "")
        self.assertFalse(result["success"])
        self.assertIn("hasło", result["error"])

    def test_rejects_bad_hostname(self):
        result = ssh_keys.deploy_key_to_remote("tomek", "not a host!", "wieslaw", "x")
        self.assertFalse(result["success"])

    def test_rejects_bad_remote_username(self):
        result = ssh_keys.deploy_key_to_remote("tomek", "192.168.0.20", "Not Valid", "x")
        self.assertFalse(result["success"])

    @mock.patch("nas_monitor.ssh_keys.get_key_status", return_value={"has_key": False})
    def test_rejects_when_local_user_has_no_key_yet(self, mock_status):
        result = ssh_keys.deploy_key_to_remote("tomek", "192.168.0.20", "wieslaw", "x")
        self.assertFalse(result["success"])
        self.assertIn("nie ma jeszcze", result["error"])

    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    @mock.patch("nas_monitor.ssh_keys.system_tools.run", return_value=(0, "", ""))
    @mock.patch("nas_monitor.ssh_keys.system_tools.find_binary", side_effect=lambda name: f"/usr/bin/{name}")
    @mock.patch("nas_monitor.ssh_keys.get_key_status", return_value={"has_key": True})
    def test_sends_password_via_env_not_argv(self, mock_status, mock_find, mock_run, mock_pwnam):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash")
        result = ssh_keys.deploy_key_to_remote("tomek", "192.168.0.20", "wieslaw", "hunter2")
        self.assertTrue(result["success"])
        called_cmd = mock_run.call_args[0][0]
        called_kwargs = mock_run.call_args[1]
        self.assertNotIn("hunter2", called_cmd)  # must never appear in argv (visible via ps)
        self.assertEqual(called_kwargs["extra_env"], {"SSHPASS": "hunter2"})
        self.assertIn("wieslaw@192.168.0.20", called_cmd)

    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    @mock.patch("nas_monitor.ssh_keys.system_tools.run", return_value=(1, "", "Permission denied"))
    @mock.patch("nas_monitor.ssh_keys.system_tools.find_binary", side_effect=lambda name: f"/usr/bin/{name}")
    @mock.patch("nas_monitor.ssh_keys.get_key_status", return_value={"has_key": True})
    def test_propagates_failure(self, mock_status, mock_find, mock_run, mock_pwnam):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash")
        result = ssh_keys.deploy_key_to_remote("tomek", "192.168.0.20", "wieslaw", "wrongpass")
        self.assertFalse(result["success"])
        self.assertIn("Permission denied", result["error"])


if __name__ == "__main__":
    unittest.main()
