import os
import sys
import shlex
import tempfile
import shutil
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
        self.assertEqual(result["error_code"], "users.not_found")

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

    @mock.patch("nas_monitor.ssh_keys.os.path.isfile", return_value=False)
    @mock.patch("nas_monitor.ssh_keys.os.chmod")
    @mock.patch("nas_monitor.ssh_keys.os.chown")
    @mock.patch("nas_monitor.ssh_keys.os.makedirs")
    @mock.patch("nas_monitor.ssh_keys.system_tools.find_binary", return_value="/usr/bin/ssh-keygen")
    @mock.patch("nas_monitor.ssh_keys.system_tools.run", return_value=(0, "", ""))
    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    @mock.patch("builtins.open", new_callable=mock.mock_open, read_data="ssh-ed25519 AAAA... nas-sync@nas-monitor\n")
    def test_nologin_account_can_generate_a_key(self, mock_open, mock_pwnam, mock_run, mock_find, mock_makedirs, mock_chown, mock_chmod, mock_isfile):
        # The dedicated sync account is nologin by design (see
        # SYNC_ACCOUNT_USERNAME) - nologin blocks someone logging INTO
        # this box as that account, which has nothing to do with whether
        # a scheduled job running AS it can use its own key to connect
        # OUT. Generating a key for it must succeed.
        mock_pwnam.return_value = _fake_pwent("nas-sync", "/usr/sbin/nologin")
        result = ssh_keys.generate_key("nas-sync")
        self.assertTrue(result["success"])

    @mock.patch("nas_monitor.ssh_keys.os.path.isfile", return_value=True)
    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    def test_refuses_to_overwrite_existing_key(self, mock_pwnam, mock_isfile):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash")
        result = ssh_keys.generate_key("tomek")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "ssh_keys.already_exists")

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
        self.assertEqual(result["error_code"], "system.tool_missing")
        self.assertEqual(result["error_context"]["tool"], "ssh-keygen")


class TestDeployKeyToRemote(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmpdir, ".ssh"), exist_ok=True)
        with open(os.path.join(self.tmpdir, ".ssh", "id_ed25519.pub"), "w") as fh:
            fh.write("ssh-ed25519 AAAAtest tomek@nas-monitor\n")
        self.state_patch = mock.patch("nas_monitor.ssh_keys.state_store.STATE_DIR", self.tmpdir)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.ssh_keys.get_key_status", return_value={"has_key": True})
    def test_rejects_empty_password(self, mock_status):
        result = ssh_keys.deploy_key_to_remote("tomek", "192.168.0.20", "wieslaw", "")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "ssh_keys.empty_remote_password")

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
        self.assertEqual(result["error_code"], "ssh_keys.no_key_yet")

    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    @mock.patch("nas_monitor.ssh_keys.system_tools.run", return_value=(0, "", ""))
    @mock.patch("nas_monitor.ssh_keys.system_tools.find_binary", side_effect=lambda name: f"/usr/bin/{name}")
    @mock.patch("nas_monitor.ssh_keys.get_key_status", return_value={"has_key": True})
    def test_sends_password_via_env_not_argv(self, mock_status, mock_find, mock_run, mock_pwnam):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash", home=self.tmpdir)
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
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertIn("Permission denied", result["error_context"]["detail"])


class TestDeploymentTracking(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_patch = mock.patch("nas_monitor.ssh_keys.state_store.STATE_DIR", self.tmpdir)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    @mock.patch("nas_monitor.ssh_keys.system_tools.run", return_value=(0, "", ""))
    @mock.patch("nas_monitor.ssh_keys.system_tools.find_binary", side_effect=lambda name: f"/usr/bin/{name}")
    @mock.patch("nas_monitor.ssh_keys.get_key_status")
    def test_deploy_records_deployment_and_marks_it_current(self, mock_status, mock_find, mock_run, mock_pwnam):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash", home=self.tmpdir)
        mock_status.return_value = {"has_key": True}
        os.makedirs(os.path.join(self.tmpdir, ".ssh"), exist_ok=True)
        pub_path = os.path.join(self.tmpdir, ".ssh", "id_ed25519.pub")
        with open(pub_path, "w") as fh:
            fh.write("ssh-ed25519 AAAAtest tomek@nas-monitor\n")

        ssh_keys.deploy_key_to_remote("tomek", "192.168.0.20", "wieslaw", "hunter2")
        deployments = ssh_keys.get_deployments("tomek")
        self.assertEqual(len(deployments), 1)
        self.assertEqual(deployments[0]["host"], "192.168.0.20")
        self.assertTrue(deployments[0]["is_current"])
        # no friendly name given - falls back to the host itself, not blank
        self.assertEqual(deployments[0]["display_name"], "192.168.0.20")

    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    @mock.patch("nas_monitor.ssh_keys.system_tools.run", return_value=(0, "", ""))
    @mock.patch("nas_monitor.ssh_keys.system_tools.find_binary", side_effect=lambda name: f"/usr/bin/{name}")
    @mock.patch("nas_monitor.ssh_keys.get_key_status", return_value={"has_key": True})
    def test_deploy_uses_friendly_name_when_given(self, mock_status, mock_find, mock_run, mock_pwnam):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash", home=self.tmpdir)
        os.makedirs(os.path.join(self.tmpdir, ".ssh"), exist_ok=True)
        with open(os.path.join(self.tmpdir, ".ssh", "id_ed25519.pub"), "w") as fh:
            fh.write("ssh-ed25519 AAAAtest tomek@nas-monitor\n")

        ssh_keys.deploy_key_to_remote("tomek", "192.168.0.20", "wieslaw", "hunter2", display_name="vOMV")
        deployments = ssh_keys.get_deployments("tomek")
        self.assertEqual(deployments[0]["display_name"], "vOMV")
        self.assertEqual(deployments[0]["host"], "192.168.0.20")  # actual connection address unchanged

    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    def test_blank_friendly_name_falls_back_to_host(self, mock_pwnam):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash", home=self.tmpdir)
        ssh_keys._record_deployment("tomek", "192.168.0.20", "wieslaw", "ssh-ed25519 AAAAx", display_name="   ")
        deployments = ssh_keys.get_deployments("tomek")
        self.assertEqual(deployments[0]["display_name"], "192.168.0.20")

    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    def test_regenerating_key_marks_old_deployment_stale(self, mock_pwnam):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash", home=self.tmpdir)
        os.makedirs(os.path.join(self.tmpdir, ".ssh"), exist_ok=True)
        pub_path = os.path.join(self.tmpdir, ".ssh", "id_ed25519.pub")

        with open(pub_path, "w") as fh:
            fh.write("ssh-ed25519 AAAAoriginal tomek@nas-monitor\n")
        ssh_keys._record_deployment("tomek", "192.168.0.20", "wieslaw", "ssh-ed25519 AAAAoriginal tomek@nas-monitor")

        # key regenerated - pub file now has DIFFERENT content, deployment record untouched
        with open(pub_path, "w") as fh:
            fh.write("ssh-ed25519 AAAAbrandnew tomek@nas-monitor\n")

        deployments = ssh_keys.get_deployments("tomek")
        self.assertEqual(len(deployments), 1)
        self.assertFalse(deployments[0]["is_current"])

    @mock.patch("nas_monitor.ssh_keys.system_tools.run")
    @mock.patch("nas_monitor.ssh_keys.system_tools.find_binary", side_effect=lambda name: f"/usr/bin/{name}")
    def test_remove_deployment_never_relies_on_env_var_reaching_remote_shell(self, mock_find, mock_run):
        # This is the exact bug class that was caught by hand: env vars set
        # for the LOCAL ssh subprocess do NOT propagate to the REMOTE shell,
        # so the removal command must never reference the pubkey via a
        # remote $VAR - it must be embedded, pre-quoted, in the command
        # string built locally. If this regresses, `grep -vF ""` on the
        # remote would match nothing and silently wipe authorized_keys.
        mock_run.return_value = (0, "", "")
        pubkey_with_spaces = "ssh-ed25519 AAAAtest tomek@nas-monitor"
        ssh_keys._record_deployment("tomek", "192.168.0.20", "wieslaw", pubkey_with_spaces)

        ssh_keys.remove_deployment("tomek", "192.168.0.20", "wieslaw", "hunter2")

        called_cmd = mock_run.call_args[0][0]
        remote_script = called_cmd[-1]
        # the exact pubkey text must appear directly in the command string
        # sent to ssh - not behind a $VAR expansion that depends on
        # something SSH doesn't forward by default
        self.assertIn("AAAAtest", remote_script)
        self.assertNotIn("$NAS_MONITOR_PUBKEY", remote_script)
        self.assertNotIn("grep -vF -- \"\"", remote_script)  # the exact failure mode: empty pattern = wipe everything

    @mock.patch("nas_monitor.ssh_keys.pwd.getpwnam")
    @mock.patch("nas_monitor.ssh_keys.system_tools.run", return_value=(0, "", ""))
    @mock.patch("nas_monitor.ssh_keys.system_tools.find_binary", side_effect=lambda name: f"/usr/bin/{name}")
    def test_remove_deployment_drops_local_record_on_success(self, mock_find, mock_run, mock_pwnam):
        mock_pwnam.return_value = _fake_pwent("tomek", "/bin/bash", home=self.tmpdir)
        ssh_keys._record_deployment("tomek", "192.168.0.20", "wieslaw", "ssh-ed25519 AAAAtest x")
        result = ssh_keys.remove_deployment("tomek", "192.168.0.20", "wieslaw", "hunter2")
        self.assertTrue(result["success"])
        self.assertEqual(ssh_keys.get_deployments("tomek"), [])

    def test_remove_deployment_rejects_unknown_entry(self):
        result = ssh_keys.remove_deployment("tomek", "1.2.3.4", "nobody", "x")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "ssh_keys.deployment_not_found")

    def test_remove_deployment_rejects_empty_password(self):
        ssh_keys._record_deployment("tomek", "192.168.0.20", "wieslaw", "ssh-ed25519 AAAAtest x")
        result = ssh_keys.remove_deployment("tomek", "192.168.0.20", "wieslaw", "")
        self.assertFalse(result["success"])

    def test_remote_removal_script_succeeds_when_removed_key_was_the_only_line(self):
        # Real regression test (runs actual /bin/sh, not mocked): grep -v
        # exits 1 when EVERY line matched and got filtered out - i.e.
        # exactly what happens when the key being removed is the only
        # line in authorized_keys. That is success, not an error - a
        # naive `grep ... && mv ...` chain would skip the mv and silently
        # leave the old file in place. Caught by hand once already.
        import subprocess

        key_text = "ssh-ed25519 AAAAonlyline tomek@nas-monitor"
        auth_keys_path = os.path.join(self.tmpdir, "authorized_keys")
        with open(auth_keys_path, "w") as fh:
            fh.write(key_text + "\n")

        quoted = shlex.quote(key_text)
        script = (
            f"f={shlex.quote(auth_keys_path)}; "
            f"if [ -f \"$f\" ]; then "
            f"grep -vF -- {quoted} \"$f\" > \"$f.tmp\" || true; "
            f"mv \"$f.tmp\" \"$f\"; "
            f"fi"
        )
        proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(auth_keys_path) as fh:
            self.assertEqual(fh.read(), "")


class TestEnsureSyncAccountExists(unittest.TestCase):
    @mock.patch("nas_monitor.ssh_keys.users_mod.user_exists", return_value=True)
    @mock.patch("nas_monitor.ssh_keys.users_mod.create_user")
    def test_noop_when_already_exists(self, mock_create, mock_exists):
        result = ssh_keys.ensure_sync_account_exists()
        self.assertTrue(result["success"])
        self.assertEqual(result["username"], ssh_keys.SYNC_ACCOUNT_USERNAME)
        mock_create.assert_not_called()

    @mock.patch("nas_monitor.ssh_keys.users_mod.user_exists", return_value=False)
    @mock.patch("nas_monitor.ssh_keys.users_mod.create_user")
    def test_creates_when_missing(self, mock_create, mock_exists):
        mock_create.return_value = {"success": True, "username": ssh_keys.SYNC_ACCOUNT_USERNAME}
        result = ssh_keys.ensure_sync_account_exists()
        self.assertTrue(result["success"])
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        self.assertIsNone(kwargs.get("shell"))  # nologin - intentional, see module docstring

    @mock.patch("nas_monitor.ssh_keys.users_mod.user_exists", return_value=False)
    @mock.patch("nas_monitor.ssh_keys.users_mod.create_user")
    def test_propagates_creation_failure(self, mock_create, mock_exists):
        mock_create.return_value = {"success": False, "error_code": "system.tool_missing", "error_context": {"tool": "useradd"}}
        result = ssh_keys.ensure_sync_account_exists()
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.tool_missing")


if __name__ == "__main__":
    unittest.main()
