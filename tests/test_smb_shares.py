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

    def test_render_then_read_round_trip_mixed_rw_ro(self):
        shares = [
            {
                "name": "dane",
                "path": "/srv/dane",
                "comment": "Wspólne",
                "access_group": "dane_access",
                "permissions": {"tomek": "rw", "wieslaw": "ro"},
            },
            {
                "name": "backup",
                "path": "/srv/backup",
                "comment": "",
                "access_group": None,
                "permissions": {},
            },
        ]
        content = smb_shares._render_managed_shares(shares)
        with open(self.managed_path, "w") as fh:
            fh.write(content)

        with mock.patch("nas_monitor.smb_shares.grp.getgrnam") as mock_getgrnam:
            mock_getgrnam.return_value = mock.Mock(gr_mem=["tomek", "wieslaw"])
            parsed = smb_shares._read_managed_shares(self.managed_path)

        by_name = {s["name"]: s for s in parsed}
        self.assertEqual(by_name["dane"]["path"], "/srv/dane")
        self.assertEqual(by_name["dane"]["comment"], "Wspólne")
        self.assertEqual(by_name["dane"]["permissions"], {"tomek": "rw", "wieslaw": "ro"})
        self.assertEqual(by_name["dane"]["access_group"], "dane_access")
        self.assertEqual(by_name["backup"]["permissions"], {})
        self.assertIsNone(by_name["backup"]["access_group"])

    def test_render_always_writable_at_share_level_uses_read_list_for_ro(self):
        content = smb_shares._render_managed_shares(
            [
                {
                    "name": "dane",
                    "path": "/srv/dane",
                    "comment": "",
                    "access_group": "dane_access",
                    "permissions": {"tomek": "rw", "wieslaw": "ro"},
                }
            ]
        )
        self.assertIn("read only = no", content)
        self.assertIn("read list = wieslaw", content)
        self.assertNotIn("read list = tomek", content)
        # '+group' (not '@group') to skip the NIS-netgroup-first lookup
        # that caused a real NT_STATUS_NO_SUCH_GROUP failure in production
        # even though the group genuinely existed. force group takes a
        # bare name - no prefix syntax applies to it at all.
        self.assertIn("force group = dane_access", content)
        self.assertIn("valid users = +dane_access", content)

    def test_no_read_list_line_when_everyone_is_rw(self):
        content = smb_shares._render_managed_shares(
            [
                {
                    "name": "dane",
                    "path": "/srv/dane",
                    "comment": "",
                    "access_group": "dane_access",
                    "permissions": {"tomek": "rw"},
                }
            ]
        )
        self.assertNotIn("read list", content)

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
        self.assertEqual(result["error_code"], "shares.global_section_missing")


class TestValidateAndApply(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")
        with open(self.managed, "w") as fh:
            fh.write("# old content\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares._ensure_include_directive", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.reload_smbd", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "Loaded services file OK.", ""))
    def test_keeps_new_content_when_testparm_passes(self, mock_run, mock_find, mock_reload, mock_include):
        result = smb_shares._validate_and_apply("[dane]\n   path = /srv/dane\n", self.managed)
        self.assertTrue(result["success"])
        with open(self.managed) as fh:
            self.assertIn("[dane]", fh.read())

    @mock.patch("nas_monitor.smb_shares._ensure_include_directive", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(1, "", "Error loading services."))
    def test_rolls_back_when_testparm_fails(self, mock_run, mock_find, mock_include):
        result = smb_shares._validate_and_apply("[dane\n   broken\n", self.managed)
        self.assertFalse(result["success"])
        with open(self.managed) as fh:
            content = fh.read()
        self.assertEqual(content, "# old content\n")
        self.assertNotIn("[dane", content)

    @mock.patch("nas_monitor.smb_shares._ensure_include_directive", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value=None)
    def test_rolls_back_when_testparm_missing(self, mock_find, mock_include):
        result = smb_shares._validate_and_apply("[dane]\n   path = /srv/dane\n", self.managed)
        self.assertFalse(result["success"])
        with open(self.managed) as fh:
            self.assertEqual(fh.read(), "# old content\n")

    @mock.patch("nas_monitor.smb_shares._ensure_include_directive", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.reload_smbd", return_value={"success": False, "error_code": "system.command_failed", "error_context": {"detail": "no pid"}})
    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "OK", ""))
    def test_reload_failure_is_a_soft_warning_not_a_rollback(self, mock_run, mock_find, mock_reload, mock_include):
        result = smb_shares._validate_and_apply("[dane]\n   path = /srv/dane\n", self.managed)
        self.assertTrue(result["success"])
        self.assertIn("warnings", result)
        self.assertEqual(result["warnings"][0]["code"], "shares.reload_failed")
        with open(self.managed) as fh:
            self.assertIn("[dane]", fh.read())


class TestMissingSmbPasswordWarning(unittest.TestCase):
    @mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users")
    def test_warns_about_users_without_smb_password(self, mock_list):
        mock_list.return_value = {"available": True, "usernames": ["wieslaw"], "error": None}
        warning = smb_shares._missing_smb_password_warning(["tomek", "wieslaw"])
        self.assertIsNotNone(warning)
        self.assertEqual(warning["code"], "shares.missing_smb_password")
        self.assertIn("tomek", warning["context"]["usernames"])
        self.assertNotIn("wieslaw", warning["context"]["usernames"])

    @mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users")
    def test_no_warning_when_everyone_has_a_password(self, mock_list):
        mock_list.return_value = {"available": True, "usernames": ["tomek", "wieslaw"], "error": None}
        self.assertIsNone(smb_shares._missing_smb_password_warning(["tomek", "wieslaw"]))

    def test_no_warning_for_empty_user_list(self):
        self.assertIsNone(smb_shares._missing_smb_password_warning([]))

    @mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users")
    def test_no_crash_when_samba_unavailable(self, mock_list):
        mock_list.return_value = {"available": False, "usernames": [], "error": "pdbedit not installed"}
        self.assertIsNone(smb_shares._missing_smb_password_warning(["tomek"]))


@mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users", return_value={"available": False, "usernames": [], "error": None})
class TestCreateShareWithPermissions(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")
        open(self.managed, "a").close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": True, "error": None})
    def test_adds_every_permission_holder_to_the_access_group(self, mock_add, mock_dir, mock_apply, mock_smb):
        result = smb_shares.create_share(
            "dane", permissions={"tomek": "rw", "wacek": "ro"}, managed_conf_path=self.managed
        )
        self.assertTrue(result["success"])
        self.assertEqual(mock_add.call_count, 2)
        mock_add.assert_any_call("tomek", "dane_access")
        mock_add.assert_any_call("wacek", "dane_access")
        mock_dir.assert_called_once_with(smb_shares.share_path("dane"), "dane_access")

    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    def test_no_permissions_means_no_access_group(self, mock_dir, mock_smb):
        with mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True}):
            smb_shares.create_share("public", permissions={}, managed_conf_path=self.managed)
        mock_dir.assert_called_once_with(smb_shares.share_path("public"), None)

    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": False, "error": "brak takiego uzytkownika"})
    def test_stops_and_reports_error_if_a_user_cannot_be_added(self, mock_add, mock_smb):
        result = smb_shares.create_share("dane", permissions={"ghost": "rw"}, managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_context"]["user"], "ghost")

    def test_rejects_invalid_permission_level(self, mock_smb):
        result = smb_shares.create_share("dane", permissions={"tomek": "admin"}, managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "shares.invalid_permission_level")
        self.assertEqual(result["error_context"]["user"], "tomek")


@mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users", return_value={"available": False, "usernames": [], "error": None})
class TestLegacyAccessGroupMigration(unittest.TestCase):
    """A share created through the old single-group picker (before this
    became per-user) could point its access_group at ANY existing group -
    e.g. someone's own personal account group, exactly what happened in
    production. These lock in that such a share gets migrated to the
    tool's own dedicated group on next edit, and never has its foreign
    group's membership touched."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")
        content = smb_shares._render_managed_shares(
            [{"name": "test", "path": "/srv/test", "comment": "", "access_group": "tomek", "permissions": {}}]
        )
        with open(self.managed, "w") as fh:
            fh.write(content)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.users_mod.remove_user_from_group", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": True, "error": None})
    def test_update_migrates_to_dedicated_group_and_never_touches_foreign_group(
        self, mock_add, mock_remove, mock_dir, mock_apply, mock_smb
    ):
        result = smb_shares.update_share("test", permissions={"wieslaw": "rw"}, managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        mock_add.assert_called_once_with("wieslaw", "test_access")
        mock_remove.assert_not_called()
        mock_dir.assert_called_once_with("/srv/test", "test_access")

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/sbin/groupdel")
    @mock.patch("nas_monitor.smb_shares.system_tools.run")
    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    def test_delete_never_groupdels_a_foreign_group(self, mock_apply, mock_run, mock_find, mock_smb):
        smb_shares.delete_share("test", managed_conf_path=self.managed)
        mock_run.assert_not_called()  # "tomek" must never be passed to groupdel

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/sbin/groupdel")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "", ""))
    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    def test_delete_does_groupdel_its_own_dedicated_group(self, mock_apply, mock_run, mock_find, mock_smb):
        content = smb_shares._render_managed_shares(
            [{"name": "test", "path": "/srv/test", "comment": "", "access_group": "test_access", "permissions": {}}]
        )
        with open(self.managed, "w") as fh:
            fh.write(content)
        smb_shares.delete_share("test", managed_conf_path=self.managed)
        mock_run.assert_called_once_with(["/usr/sbin/groupdel", "test_access"])


@mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users", return_value={"available": False, "usernames": [], "error": None})
class TestUpdateShareWithPermissions(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")
        open(self.managed, "a").close()
        self.existing_share = {
            "name": "dane",
            "path": "/srv/dane",
            "comment": "",
            "access_group": "dane_access",
            "permissions": {"tomek": "rw"},
            "managed": True,
        }

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.users_mod.remove_user_from_group", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares._read_managed_shares")
    def test_adds_new_member_without_touching_existing_one(self, mock_read, mock_add, mock_remove, mock_dir, mock_apply, mock_smb):
        mock_read.return_value = [dict(self.existing_share)]
        result = smb_shares.update_share(
            "dane", permissions={"tomek": "rw", "wacek": "ro"}, managed_conf_path=self.managed
        )
        self.assertTrue(result["success"])
        mock_add.assert_called_once_with("wacek", "dane_access")
        mock_remove.assert_not_called()

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.users_mod.remove_user_from_group", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares._read_managed_shares")
    def test_changing_rw_to_ro_does_not_touch_group_membership(self, mock_read, mock_add, mock_remove, mock_dir, mock_apply, mock_smb):
        mock_read.return_value = [dict(self.existing_share)]
        result = smb_shares.update_share("dane", permissions={"tomek": "ro"}, managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        mock_add.assert_not_called()
        mock_remove.assert_not_called()

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.users_mod.remove_user_from_group", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": True, "error": None})
    @mock.patch("nas_monitor.smb_shares._read_managed_shares")
    def test_removing_a_user_revokes_group_membership(self, mock_read, mock_add, mock_remove, mock_dir, mock_apply, mock_smb):
        mock_read.return_value = [dict(self.existing_share)]
        result = smb_shares.update_share("dane", permissions={}, managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        mock_remove.assert_called_once_with("tomek", "dane_access")
        mock_add.assert_not_called()

    @mock.patch("nas_monitor.smb_shares._read_managed_shares")
    def test_rejects_update_on_unknown_share(self, mock_read, mock_smb):
        mock_read.return_value = [dict(self.existing_share)]
        result = smb_shares.update_share("does-not-exist", comment="x", managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "shares.not_found")

    @mock.patch("nas_monitor.smb_shares._read_managed_shares")
    def test_rejects_invalid_permission_level(self, mock_read, mock_smb):
        mock_read.return_value = [dict(self.existing_share)]
        result = smb_shares.update_share("dane", permissions={"tomek": "sudo"}, managed_conf_path=self.managed)
        self.assertFalse(result["success"])


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
        self.assertEqual(result["error_code"], "shares.group_not_found")

    def test_creates_directory_without_group(self):
        result = smb_shares._prepare_share_directory(self.path, None)
        self.assertTrue(result["success"])
        self.assertTrue(os.path.isdir(self.path))


if __name__ == "__main__":
    unittest.main()
