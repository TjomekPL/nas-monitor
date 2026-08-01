import os
import sys
import tempfile
import shutil
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import smb_shares  # noqa: E402


class TestValidShareName(unittest.TestCase):
    def test_accepts_normal_names(self):
        for name in ("dane", "backup", "foto-rodzinne", "udzial_1"):
            self.assertTrue(smb_shares.is_valid_share_name(name), name)

    def test_rejects_reserved_names(self):
        for name in ("global", "homes", "printers", "print$", "netlogon", "profiles"):
            self.assertFalse(smb_shares.is_valid_share_name(name), name)

    def test_rejects_bad_syntax(self):
        for name in ("", "Dane", "1dane", "dane share", "dane;rm -rf /"):
            self.assertFalse(smb_shares.is_valid_share_name(name), name)


class TestManagedFileRoundTrip(unittest.TestCase):
    """Uses real temp files (not mocks) for the read/render logic - it's
    pure text handling, cheap to test for real, and catches actual
    configparser quirks that a mock would hide."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed_path = os.path.join(self.tmpdir, "shares.conf")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_empty_file_is_empty_list(self):
        open(self.managed_path, "a").close()
        self.assertEqual(smb_shares._read_managed_shares(self.managed_path), [])

    def test_missing_file_is_empty_list(self):
        self.assertEqual(smb_shares._read_managed_shares(self.managed_path), [])

    def test_render_then_read_round_trip(self):
        shares = [
            {"name": "dane", "path": "/srv/dane", "comment": "Wspólne", "read_only": False, "access_group": "dane_access"},
            {"name": "backup", "path": "/srv/backup", "comment": "", "read_only": True, "access_group": None},
        ]
        content = smb_shares._render_managed_shares(shares)
        with open(self.managed_path, "w") as fh:
            fh.write(content)

        with mock.patch("nas_monitor.smb_shares.grp.getgrnam") as mock_getgrnam:
            mock_getgrnam.return_value = mock.Mock(gr_mem=["tomek", "wacek"])
            parsed = smb_shares._read_managed_shares(self.managed_path)

        by_name = {s["name"]: s for s in parsed}
        self.assertEqual(by_name["dane"]["path"], "/srv/dane")
        self.assertEqual(by_name["dane"]["comment"], "Wspólne")
        self.assertFalse(by_name["dane"]["read_only"])
        self.assertEqual(by_name["dane"]["users"], ["tomek", "wacek"])
        self.assertEqual(by_name["dane"]["access_group"], "dane_access")
        self.assertTrue(by_name["backup"]["read_only"])
        self.assertEqual(by_name["backup"]["users"], [])
        self.assertIsNone(by_name["backup"]["access_group"])

    def test_render_includes_force_group_for_reliable_write_permissions(self):
        content = smb_shares._render_managed_shares(
            [{"name": "dane", "path": "/srv/dane", "comment": "", "read_only": False, "access_group": "dane_access"}]
        )
        self.assertIn("force group = @dane_access", content)
        self.assertIn("valid users = @dane_access", content)

    def test_corrupted_managed_file_does_not_crash_detection(self):
        with open(self.managed_path, "w") as fh:
            fh.write("[unterminated\n   path = /srv/x\n")
        self.assertEqual(smb_shares._read_managed_shares(self.managed_path), [])


class TestEnsureIncludeDirective(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.smb_conf = os.path.join(self.tmpdir, "smb.conf")
        self.managed = os.path.join(self.tmpdir, "sub", "shares.conf")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_appends_include_once(self):
        with open(self.smb_conf, "w") as fh:
            fh.write("[global]\n   workgroup = WORKGROUP\n\n[printers]\n   path = /var/tmp\n")

        r1 = smb_shares._ensure_include_directive(self.smb_conf, self.managed)
        self.assertTrue(r1["success"])
        with open(self.smb_conf) as fh:
            content_after_first = fh.read()
        self.assertIn(f"include = {self.managed}", content_after_first)
        self.assertIn("workgroup = WORKGROUP", content_after_first)  # untouched

        # calling again must not duplicate the line
        r2 = smb_shares._ensure_include_directive(self.smb_conf, self.managed)
        self.assertTrue(r2["success"])
        with open(self.smb_conf) as fh:
            content_after_second = fh.read()
        self.assertEqual(content_after_second.count("include ="), 1)

    def test_creates_managed_file_if_missing(self):
        with open(self.smb_conf, "w") as fh:
            fh.write("[global]\n   workgroup = WORKGROUP\n")
        smb_shares._ensure_include_directive(self.smb_conf, self.managed)
        self.assertTrue(os.path.isfile(self.managed))

    def test_fails_gracefully_without_global_section(self):
        with open(self.smb_conf, "w") as fh:
            fh.write("[printers]\n   path = /var/tmp\n")
        result = smb_shares._ensure_include_directive(self.smb_conf, self.managed)
        self.assertFalse(result["success"])
        self.assertIn("global", result["error"])


class TestValidateAndApply(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")
        with open(self.managed, "w") as fh:
            fh.write("# old content\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares._ensure_include_directive", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares.reload_smbd", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "Loaded services file OK.", ""))
    def test_keeps_new_content_when_testparm_passes(self, mock_run, mock_find, mock_reload, mock_include):
        result = smb_shares._validate_and_apply("[dane]\n   path = /srv/dane\n", self.managed)
        self.assertTrue(result["success"])
        with open(self.managed) as fh:
            self.assertIn("[dane]", fh.read())

    @mock.patch("nas_monitor.smb_shares._ensure_include_directive", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(1, "", "Error loading services."))
    def test_rolls_back_when_testparm_fails(self, mock_run, mock_find, mock_include):
        result = smb_shares._validate_and_apply("[dane\n   broken\n", self.managed)
        self.assertFalse(result["success"])
        with open(self.managed) as fh:
            content = fh.read()
        self.assertEqual(content, "# old content\n")  # rolled back, not the broken content
        self.assertNotIn("[dane", content)

    @mock.patch("nas_monitor.smb_shares._ensure_include_directive", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value=None)
    def test_rolls_back_when_testparm_missing(self, mock_find, mock_include):
        result = smb_shares._validate_and_apply("[dane]\n   path = /srv/dane\n", self.managed)
        self.assertFalse(result["success"])
        with open(self.managed) as fh:
            self.assertEqual(fh.read(), "# old content\n")

    @mock.patch("nas_monitor.smb_shares._ensure_include_directive", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares.reload_smbd", return_value={"success": False, "error": "no pid"})
    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "OK", ""))
    def test_reload_failure_is_a_soft_warning_not_a_rollback(self, mock_run, mock_find, mock_reload, mock_include):
        result = smb_shares._validate_and_apply("[dane]\n   path = /srv/dane\n", self.managed)
        self.assertTrue(result["success"])  # config is valid and saved
        self.assertIn("warning", result)
        with open(self.managed) as fh:
            self.assertIn("[dane]", fh.read())  # new content kept, not rolled back


class TestCreateShareWithUsers(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")
        open(self.managed, "a").close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": True, "error": None})
    def test_adds_each_selected_user_to_auto_managed_group(self, mock_add, mock_dir, mock_apply):
        result = smb_shares.create_share("dane", users=["tomek", "wacek"], managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        self.assertEqual(mock_add.call_count, 2)
        mock_add.assert_any_call("tomek", "dane_access")
        mock_add.assert_any_call("wacek", "dane_access")
        mock_dir.assert_called_once_with(smb_shares.share_path("dane"), "dane_access")

    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    def test_no_users_means_no_access_group(self, mock_dir):
        with mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True}):
            smb_shares.create_share("public", users=[], managed_conf_path=self.managed)
        mock_dir.assert_called_once_with(smb_shares.share_path("public"), None)

    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": False, "error": "brak takiego uzytkownika"})
    def test_stops_and_reports_error_if_a_user_cannot_be_added(self, mock_add):
        result = smb_shares.create_share("dane", users=["ghost"], managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertIn("ghost", result["error"])


class TestUpdateShareWithUsers(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")
        content = smb_shares._render_managed_shares(
            [{"name": "dane", "path": "/srv/dane", "comment": "", "read_only": False, "access_group": "dane_access"}]
        )
        with open(self.managed, "w") as fh:
            fh.write(content)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.users_mod.remove_user_from_group", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares._resolve_group_members", return_value=["tomek"])
    def test_diffs_membership_add_and_remove(self, mock_resolve, mock_add, mock_remove, mock_dir, mock_apply):
        # currently only "tomek" has access - update to ["tomek", "wacek"]
        # should add wacek, and NOT touch tomek (already a member)
        result = smb_shares.update_share("dane", users=["tomek", "wacek"], managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        mock_add.assert_called_once_with("wacek", "dane_access")
        mock_remove.assert_not_called()

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.users_mod.remove_user_from_group", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares._resolve_group_members", return_value=["tomek", "wacek"])
    def test_removing_a_user_from_the_list_revokes_group_membership(self, mock_resolve, mock_add, mock_remove, mock_dir, mock_apply):
        # currently tomek+wacek have access - update to just ["tomek"]
        result = smb_shares.update_share("dane", users=["tomek"], managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        mock_remove.assert_called_once_with("wacek", "dane_access")
        mock_add.assert_not_called()

    def test_rejects_update_on_unknown_share(self):
        result = smb_shares.update_share("does-not-exist", comment="x", managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertIn("nie istnieje", result["error"])


class TestPrepareShareDirectory(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "share1")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares.grp.getgrnam", side_effect=KeyError)
    def test_rejects_nonexistent_group(self, mock_getgrnam):
        result = smb_shares._prepare_share_directory(self.path, "ghostgroup")
        self.assertFalse(result["success"])
        self.assertIn("nie istnieje", result["error"])

    def test_creates_directory_without_group(self):
        result = smb_shares._prepare_share_directory(self.path, None)
        self.assertTrue(result["success"])
        self.assertTrue(os.path.isdir(self.path))


if __name__ == "__main__":
    unittest.main()
