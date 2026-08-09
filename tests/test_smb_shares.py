import os
import sys
import tempfile
import shutil
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import smb_shares  # noqa: E402

# A generic disk-backed location, shared by tests that don't otherwise
# care about location specifics but now need one - base_path became
# required (v0.14.9, no more implicit fallback to the system disk).
TEST_LOCATION = {"path": "/srv/TESTDISK", "disk": "sdb", "fstype": "ext4", "label": ""}


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

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "", ""))
    def test_appends_include_once(self, mock_run, mock_find):
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

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "", ""))
    def test_creates_managed_file_if_missing(self, mock_run, mock_find):
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

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "", ""))
    def test_disables_default_homes_share(self, mock_run, mock_find):
        # Regression test for a real report: a normal user logging in
        # via SMB saw a share named after themselves, alongside their
        # actual managed shares - Debian's stock smb.conf ships [homes]
        # active by default, and this tool otherwise only ever exposes
        # what was explicitly created through the dashboard.
        with open(self.smb_conf, "w") as fh:
            fh.write("[global]\n   workgroup = WORKGROUP\n\n[homes]\n   browseable = no\n   read only = no\n\n[printers]\n   path = /var/tmp\n")

        result = smb_shares._ensure_include_directive(self.smb_conf, self.managed)
        self.assertTrue(result["success"])
        with open(self.smb_conf) as fh:
            content = fh.read()
        self.assertIn("; [homes]", content)
        self.assertIn("browseable = no", content)
        self.assertIn("read only = no", content)
        for line in content.splitlines():
            if "browseable = no" in line or "read only = no" in line:
                self.assertTrue(line.strip().startswith(";"), line)
        # never touches anything outside that one section
        self.assertIn("workgroup = WORKGROUP", content)
        self.assertNotIn("; path = /var/tmp", content)  # [printers] untouched

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "", ""))
    def test_disables_homes_even_when_include_already_present(self, mock_run, mock_find):
        # The real case that matters most: an install running since
        # before homes-disabling existed already has the include line,
        # so this must NOT be gated behind "does it have the include
        # line yet" (that would mean this fix silently never runs on
        # any existing install).
        with open(self.smb_conf, "w") as fh:
            fh.write(
                f"[global]\n   include = {self.managed}\n   workgroup = WORKGROUP\n\n"
                "[homes]\n   browseable = no\n\n[printers]\n   path = /var/tmp\n"
            )
        result = smb_shares._ensure_include_directive(self.smb_conf, self.managed)
        self.assertTrue(result["success"])
        with open(self.smb_conf) as fh:
            content = fh.read()
        self.assertIn("; [homes]", content)
        self.assertEqual(content.count("include ="), 1)  # not duplicated

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run")
    def test_no_write_at_all_when_both_already_done(self, mock_run, mock_find):
        with open(self.smb_conf, "w") as fh:
            fh.write(f"[global]\n   include = {self.managed}\n   workgroup = WORKGROUP\n\n; [homes]\n;    browseable = no\n")
        result = smb_shares._ensure_include_directive(self.smb_conf, self.managed)
        self.assertTrue(result["success"])
        mock_run.assert_not_called()  # no testparm call - nothing needed writing at all

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/testparm")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(1, "", "syntax error near line 5"))
    def test_rolls_back_main_conf_when_testparm_rejects_it(self, mock_run, mock_find):
        with open(self.smb_conf, "w") as fh:
            fh.write("[global]\n   workgroup = WORKGROUP\n")
        original = open(self.smb_conf).read()

        result = smb_shares._ensure_include_directive(self.smb_conf, self.managed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "shares.config_rejected")
        with open(self.smb_conf) as fh:
            self.assertEqual(fh.read(), original)  # untouched - rolled back


class TestDisableDefaultHomesShare(unittest.TestCase):
    def test_comments_out_an_active_homes_section(self):
        content = "[global]\n   workgroup = WORKGROUP\n\n[homes]\n   browseable = no\n   read only = no\n\n[printers]\n   path = /var/tmp\n"
        result = smb_shares._disable_default_homes_share(content)
        self.assertIn("; [homes]", result)
        self.assertIn("browseable = no", result)
        self.assertIn("read only = no", result)
        for line in result.splitlines():
            if "browseable = no" in line or "read only = no" in line:
                self.assertTrue(line.strip().startswith(";"), line)
        self.assertIn("[printers]", result)
        self.assertNotIn("; path = /var/tmp", result)

    def test_is_a_no_op_when_homes_is_already_commented_out(self):
        content = "[global]\n   workgroup = WORKGROUP\n\n; [homes]\n;   browseable = no\n"
        result = smb_shares._disable_default_homes_share(content)
        self.assertEqual(result, content)

    def test_is_a_no_op_when_homes_is_absent(self):
        content = "[global]\n   workgroup = WORKGROUP\n\n[printers]\n   path = /var/tmp\n"
        result = smb_shares._disable_default_homes_share(content)
        self.assertEqual(result, content)


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
        self.loc_patcher = mock.patch.object(smb_shares, "list_share_locations", return_value=[TEST_LOCATION])
        self.loc_patcher.start()
        self.addCleanup(self.loc_patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": True, "error": None})
    def test_adds_every_permission_holder_to_the_access_group(self, mock_add, mock_dir, mock_apply, mock_smb):
        result = smb_shares.create_share(
            "dane", permissions={"tomek": "rw", "wacek": "ro"}, base_path=TEST_LOCATION["path"], managed_conf_path=self.managed
        )
        self.assertTrue(result["success"])
        self.assertEqual(mock_add.call_count, 2)
        mock_add.assert_any_call("tomek", "dane_access")
        mock_add.assert_any_call("wacek", "dane_access")
        mock_dir.assert_called_once_with(smb_shares.share_path("dane", TEST_LOCATION["path"]), "dane_access")

    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    def test_no_permissions_means_no_access_group(self, mock_dir, mock_smb):
        with mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True}):
            smb_shares.create_share("public", permissions={}, base_path=TEST_LOCATION["path"], managed_conf_path=self.managed)
        mock_dir.assert_called_once_with(smb_shares.share_path("public", TEST_LOCATION["path"]), None)

    @mock.patch("nas_monitor.smb_shares.users_mod.add_user_to_group", return_value={"success": False, "error": "brak takiego uzytkownika"})
    def test_stops_and_reports_error_if_a_user_cannot_be_added(self, mock_add, mock_smb):
        result = smb_shares.create_share("dane", permissions={"ghost": "rw"}, base_path=TEST_LOCATION["path"], managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_context"]["user"], "ghost")

    def test_rejects_invalid_permission_level(self, mock_smb):
        result = smb_shares.create_share("dane", permissions={"tomek": "admin"}, base_path=TEST_LOCATION["path"], managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "shares.invalid_permission_level")
        self.assertEqual(result["error_context"]["user"], "tomek")


@mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users", return_value={"available": False, "usernames": [], "error": None})
class TestListShareLocations(unittest.TestCase):
    def test_always_includes_the_default_location(self, mock_smb):
        with mock.patch("nas_monitor.disk_mutate.list_manageable_disks", return_value=[]):
            locations = smb_shares.list_share_locations()
        self.assertEqual(locations, [{"path": smb_shares.BASE_SHARE_PATH, "disk": None, "fstype": None, "label": ""}])

    def test_includes_disks_mounted_under_mount_base(self, mock_smb):
        disks = [
            {"name": "sdb", "mounted": True, "mount_point": "/srv/dane", "fstype": "ext4"},
            {"name": "sdc", "mounted": False, "mount_point": None, "fstype": None},
            {"name": "sdd", "mounted": True, "mount_point": "/media/other", "fstype": "ext4"},
        ]
        with mock.patch("nas_monitor.disk_mutate.list_manageable_disks", return_value=disks):
            locations = smb_shares.list_share_locations()

        paths = {loc["path"] for loc in locations}
        # sdb: mounted under /mnt/ - included. sdc: not mounted at all -
        # excluded. sdd: mounted, but NOT under /mnt/ (some unrelated
        # mount point) - excluded, this table only ever offers locations
        # this tool's own mount convention actually set up.
        self.assertEqual(paths, {smb_shares.BASE_SHARE_PATH, "/srv/dane"})
        sdb_entry = next(loc for loc in locations if loc["path"] == "/srv/dane")
        self.assertEqual(sdb_entry["disk"], "sdb")
        self.assertEqual(sdb_entry["fstype"], "ext4")


@mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users", return_value={"available": False, "usernames": [], "error": None})
class TestListRecoverableDirectories(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        for name in ["dane", "filmy", ".hidden", "already_used"]:
            os.makedirs(os.path.join(self.tmpdir, name))
        open(os.path.join(self.tmpdir, "not_a_directory"), "a").close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _locations(self, fstype="ext4"):
        return [
            {"path": smb_shares.BASE_SHARE_PATH, "disk": None, "fstype": None},
            {"path": self.tmpdir, "disk": "sdb", "fstype": fstype},
        ]

    def test_lists_top_level_directories_only(self, mock_smb):
        shares = {"available": True, "shares": []}
        with mock.patch.object(smb_shares, "list_share_locations", return_value=self._locations()), \
             mock.patch.object(smb_shares, "list_shares", return_value=shares):
            result = smb_shares.list_recoverable_directories(self.tmpdir)
        self.assertEqual(set(result["directories"]), {"dane", "filmy", "already_used"})
        self.assertFalse(result["truncated"])
        self.assertNotIn("not_a_directory", result["directories"])
        self.assertNotIn(".hidden", result["directories"])

    def test_excludes_directories_already_used_by_a_managed_share(self, mock_smb):
        shares = {"available": True, "shares": [{"name": "x", "path": os.path.join(self.tmpdir, "already_used")}]}
        with mock.patch.object(smb_shares, "list_share_locations", return_value=self._locations()), \
             mock.patch.object(smb_shares, "list_shares", return_value=shares):
            result = smb_shares.list_recoverable_directories(self.tmpdir)
        self.assertNotIn("already_used", result["directories"])
        self.assertIn("dane", result["directories"])

    def test_empty_for_the_default_srv_location(self, mock_smb):
        # Never in scope, regardless of what's actually sitting in
        # /srv - the system disk was never a removable disk that could
        # have come from "elsewhere", so recovering directories there
        # doesn't apply the same way.
        with mock.patch.object(smb_shares, "list_share_locations", return_value=self._locations()):
            result = smb_shares.list_recoverable_directories(smb_shares.BASE_SHARE_PATH)
        self.assertEqual(result["directories"], [])

    def test_empty_for_a_filesystem_this_tool_could_never_have_created(self, mock_smb):
        # His explicit reasoning: an NTFS disk could never have held a
        # share this tool made (format_disk never offers NTFS), so
        # anything on it has nothing to do with a past installation.
        with mock.patch.object(smb_shares, "list_share_locations", return_value=self._locations(fstype="ntfs")):
            result = smb_shares.list_recoverable_directories(self.tmpdir)
        self.assertEqual(result["directories"], [])

    def test_empty_for_an_unknown_location(self, mock_smb):
        with mock.patch.object(smb_shares, "list_share_locations", return_value=self._locations()):
            result = smb_shares.list_recoverable_directories("/srv/not-a-real-location")
        self.assertEqual(result["directories"], [])

    def test_caps_results_and_flags_truncation(self, mock_smb):
        # Patch the cap down for the test itself - creating thousands of
        # real directories just to prove the cap logic works would be
        # wasteful; the behavior being tested doesn't depend on the
        # actual production value.
        with mock.patch.object(smb_shares, "MAX_RECOVERABLE_DIRECTORIES", 20):
            many_dir = tempfile.mkdtemp()
            try:
                for i in range(30):
                    os.makedirs(os.path.join(many_dir, f"folder{i:03d}"))
                locations = [{"path": many_dir, "disk": "sdb", "fstype": "ext4"}]
                shares = {"available": True, "shares": []}
                with mock.patch.object(smb_shares, "list_share_locations", return_value=locations), \
                     mock.patch.object(smb_shares, "list_shares", return_value=shares):
                    result = smb_shares.list_recoverable_directories(many_dir)
                self.assertTrue(result["truncated"])
                self.assertEqual(len(result["directories"]), 20)
                # alphabetically first, not some arbitrary OS-listing order
                self.assertEqual(result["directories"][0], "folder000")
            finally:
                shutil.rmtree(many_dir)


@mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users", return_value={"available": False, "usernames": [], "error": None})
class TestCreateShareWithLocation(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")
        open(self.managed, "a").close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    def test_rejects_no_location_given_at_all(self, mock_dir, mock_apply, mock_smb):
        # System disk is no longer an implicit fallback (v0.14.9, his
        # explicit call - he only ever needed that briefly, before
        # connecting real storage, and would rather it be impossible).
        result = smb_shares.create_share("dane", managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "shares.invalid_location")
        mock_dir.assert_not_called()

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    def test_rejects_the_system_disk_explicitly(self, mock_dir, mock_apply, mock_smb):
        result = smb_shares.create_share("dane", base_path=smb_shares.BASE_SHARE_PATH, managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "shares.system_disk_not_allowed")
        mock_dir.assert_not_called()

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    def test_uses_the_chosen_valid_location(self, mock_dir, mock_apply, mock_smb):
        disks = [{"name": "sdb", "mounted": True, "mount_point": "/srv/dane", "fstype": "ext4"}]
        with mock.patch("nas_monitor.disk_mutate.list_manageable_disks", return_value=disks):
            result = smb_shares.create_share("filmy", base_path="/srv/dane", managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        mock_dir.assert_called_once_with("/srv/dane/filmy", None)

    def test_rejects_a_location_that_is_not_currently_valid(self, mock_smb):
        # e.g. the disk was unmounted in the time between the page
        # loading the location list and the form actually being
        # submitted - re-checked server-side, never trusted from the
        # client.
        with mock.patch("nas_monitor.disk_mutate.list_manageable_disks", return_value=[]):
            result = smb_shares.create_share("filmy", base_path="/mnt/dane", managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "shares.invalid_location")
        self.assertEqual(result["error_context"]["location"], "/mnt/dane")


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

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/sbin/groupdel")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(1, "", "groupdel: cannot remove the group, it's still used"))
    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    def test_delete_warns_but_still_succeeds_when_groupdel_fails(self, mock_apply, mock_run, mock_find, mock_smb):
        # Regression test for a real report: an access group survived
        # its share's deletion (groupdel's result used to be silently
        # ignored) and later "leaked" into the general Groups tab, with
        # no indication anything had gone wrong. The share deletion
        # itself must still succeed either way - only the group cleanup
        # is what's failing, and that's surfaced as a warning now.
        content = smb_shares._render_managed_shares(
            [{"name": "test", "path": "/srv/test", "comment": "", "access_group": "test_access", "permissions": {}}]
        )
        with open(self.managed, "w") as fh:
            fh.write(content)
        result = smb_shares.delete_share("test", managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        self.assertEqual(result["warnings"][0]["code"], "shares.access_group_cleanup_failed")
        self.assertEqual(result["warnings"][0]["context"]["group"], "test_access")

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value=None)
    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    def test_delete_warns_when_groupdel_binary_missing(self, mock_apply, mock_find, mock_smb):
        content = smb_shares._render_managed_shares(
            [{"name": "test", "path": "/srv/test", "comment": "", "access_group": "test_access", "permissions": {}}]
        )
        with open(self.managed, "w") as fh:
            fh.write(content)
        result = smb_shares.delete_share("test", managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        self.assertEqual(result["warnings"][0]["code"], "shares.access_group_cleanup_tool_missing")


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


class TestGroupAcl(unittest.TestCase):
    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/setfacl")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "", ""))
    def test_set_group_acl_rw_uses_rwx_and_sets_default_too(self, mock_run, mock_find):
        result = smb_shares._set_group_acl("/srv/rodzina", "rodzina", "rw")
        self.assertTrue(result["success"])
        self.assertEqual(mock_run.call_count, 2)
        mock_run.assert_any_call(["/usr/bin/setfacl", "-R", "-m", "g:rodzina:rwx", "/srv/rodzina"])
        mock_run.assert_any_call(["/usr/bin/setfacl", "-R", "-d", "-m", "g:rodzina:rwx", "/srv/rodzina"])

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/setfacl")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "", ""))
    def test_set_group_acl_ro_uses_r_x(self, mock_run, mock_find):
        smb_shares._set_group_acl("/srv/rodzina", "rodzina", "ro")
        mock_run.assert_any_call(["/usr/bin/setfacl", "-R", "-m", "g:rodzina:r-x", "/srv/rodzina"])

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value=None)
    def test_set_group_acl_missing_tool_reports_error_not_raises(self, mock_find):
        result = smb_shares._set_group_acl("/srv/rodzina", "rodzina", "rw")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.tool_missing")

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value="/usr/bin/setfacl")
    @mock.patch("nas_monitor.smb_shares.system_tools.run", return_value=(0, "", ""))
    def test_remove_group_acl_removes_both_regular_and_default(self, mock_run, mock_find):
        smb_shares._remove_group_acl("/srv/rodzina", "rodzina")
        mock_run.assert_any_call(["/usr/bin/setfacl", "-R", "-x", "g:rodzina", "/srv/rodzina"])
        mock_run.assert_any_call(["/usr/bin/setfacl", "-R", "-d", "-x", "g:rodzina", "/srv/rodzina"])

    @mock.patch("nas_monitor.smb_shares.system_tools.find_binary", return_value=None)
    def test_remove_group_acl_missing_tool_is_not_an_error(self, mock_find):
        result = smb_shares._remove_group_acl("/srv/rodzina", "rodzina")
        self.assertTrue(result["success"])

    @mock.patch("nas_monitor.smb_shares._set_group_acl")
    @mock.patch("nas_monitor.smb_shares._remove_group_acl")
    def test_sync_removes_ungranted_and_applies_new_or_changed(self, mock_remove, mock_set):
        mock_set.return_value = {"success": True}
        result = smb_shares._sync_group_acls(
            "/srv/x",
            desired_grants={"rodzina": "rw", "goscie": "ro"},
            current_grants={"rodzina": "ro", "stara_grupa": "rw"},
        )
        self.assertTrue(result["success"])
        mock_remove.assert_called_once_with("/srv/x", "stara_grupa")
        self.assertEqual(mock_set.call_count, 2)
        mock_set.assert_any_call("/srv/x", "rodzina", "rw")
        mock_set.assert_any_call("/srv/x", "goscie", "ro")

    @mock.patch("nas_monitor.smb_shares._set_group_acl")
    @mock.patch("nas_monitor.smb_shares._remove_group_acl")
    def test_sync_skips_unchanged_grants(self, mock_remove, mock_set):
        smb_shares._sync_group_acls("/srv/x", desired_grants={"rodzina": "rw"}, current_grants={"rodzina": "rw"})
        mock_set.assert_not_called()
        mock_remove.assert_not_called()

    @mock.patch("nas_monitor.smb_shares._set_group_acl", return_value={"success": False, "error_code": "system.tool_missing", "error_context": {"detail": "setfacl not found"}})
    def test_sync_reports_failure_as_warning_not_hard_stop(self, mock_set):
        result = smb_shares._sync_group_acls("/srv/x", desired_grants={"rodzina": "rw"}, current_grants={})
        self.assertTrue(result["success"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["warnings"][0]["context"]["group"], "rodzina")


class TestGroupGrantsRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_render_then_read_round_trip(self):
        share = {
            "name": "wakacje",
            "path": "/srv/wakacje",
            "comment": "",
            "permissions": {"tomek": "rw"},
            "group_grants": {"rodzina": "rw", "goscie": "ro"},
            "access_group": "wakacje_access",
        }
        content = smb_shares._render_managed_shares([share])
        self.assertIn("valid users = +wakacje_access +goscie +rodzina", content)
        self.assertIn("read list = +goscie", content)

        with open(self.managed, "w") as fh:
            fh.write(content)
        with mock.patch("nas_monitor.smb_shares._resolve_group_members", side_effect=lambda gs: ["tomek"] if "wakacje_access" in gs else []):
            result = smb_shares._read_managed_shares(self.managed)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["group_grants"], {"rodzina": "rw", "goscie": "ro"})
        self.assertEqual(result[0]["access_group"], "wakacje_access")

    def test_group_only_share_still_gets_a_dedicated_access_group_for_force_group(self):
        share = {
            "name": "wspolne",
            "path": "/srv/wspolne",
            "comment": "",
            "permissions": {},
            "group_grants": {"rodzina": "rw"},
            "access_group": "wspolne_access",
        }
        content = smb_shares._render_managed_shares([share])
        self.assertIn("valid users = +wspolne_access +rodzina", content)
        self.assertIn("force group = wspolne_access", content)


@mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users", return_value={"available": False, "usernames": [], "error": None})
class TestCreateShareWithGroupGrants(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")
        open(self.managed, "a").close()
        self.loc_patcher = mock.patch.object(smb_shares, "list_share_locations", return_value=[TEST_LOCATION])
        self.loc_patcher.start()
        self.addCleanup(self.loc_patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._sync_group_acls", return_value={"success": True, "warnings": []})
    @mock.patch("nas_monitor.smb_shares.grp.getgrnam", return_value=object())
    def test_creates_share_with_only_group_grants(self, mock_getgrnam, mock_sync, mock_dir, mock_apply, mock_smb):
        result = smb_shares.create_share("wspolne", group_grants={"rodzina": "rw"}, base_path=TEST_LOCATION["path"], managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        mock_sync.assert_called_once_with(smb_shares.share_path("wspolne", TEST_LOCATION["path"]), {"rodzina": "rw"}, {})
        mock_dir.assert_called_once_with(smb_shares.share_path("wspolne", TEST_LOCATION["path"]), "wspolne_access")

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._sync_group_acls", return_value={"success": True, "warnings": []})
    @mock.patch("nas_monitor.smb_shares.grp.getgrnam", return_value=object())
    @mock.patch("nas_monitor.smb_shares.users_mod.ensure_group_exists", return_value={"success": True})
    def test_group_grants_only_still_creates_the_dedicated_access_group(
        self, mock_ensure, mock_getgrnam, mock_sync, mock_dir, mock_apply, mock_smb
    ):
        # Regression test for a real report: a share created with ONLY
        # a group grant (no individual user permissions at all) failed
        # with "Group 'X_access' doesn't exist" - the dedicated group
        # used to only ever get created as a side effect of adding an
        # individual user to it (add_user_to_group calls
        # ensure_group_exists internally), and that loop is empty when
        # `permissions` is empty. ensure_group_exists must now be
        # called explicitly, independent of whether there are any
        # individual permissions to add.
        result = smb_shares.create_share("wspolne", group_grants={"rodzina": "rw"}, base_path=TEST_LOCATION["path"], managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        mock_ensure.assert_called_once_with("wspolne_access")

    @mock.patch("nas_monitor.smb_shares.grp.getgrnam", side_effect=KeyError)
    def test_rejects_nonexistent_group(self, mock_getgrnam, mock_smb):
        result = smb_shares.create_share("wspolne", group_grants={"ghost": "rw"}, base_path=TEST_LOCATION["path"], managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "shares.group_not_found")

    @mock.patch("nas_monitor.smb_shares.grp.getgrnam", return_value=object())
    def test_rejects_invalid_permission_level(self, mock_getgrnam, mock_smb):
        result = smb_shares.create_share("wspolne", group_grants={"rodzina": "admin"}, base_path=TEST_LOCATION["path"], managed_conf_path=self.managed)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "shares.invalid_permission_level")


@mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users", return_value={"available": False, "usernames": [], "error": None})
class TestUpdateShareWithGroupGrants(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")
        self.existing_share = {
            "name": "wakacje",
            "path": "/srv/wakacje",
            "comment": "",
            "permissions": {},
            "group_grants": {"rodzina": "rw"},
            "access_group": "wakacje_access",
        }
        with open(self.managed, "w") as fh:
            fh.write(smb_shares._render_managed_shares([self.existing_share]))

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._sync_group_acls", return_value={"success": True, "warnings": []})
    @mock.patch("nas_monitor.smb_shares.grp.getgrnam", return_value=object())
    def test_adds_a_new_group_grant(self, mock_getgrnam, mock_sync, mock_dir, mock_apply, mock_smb):
        with mock.patch("nas_monitor.smb_shares._resolve_group_members", return_value=[]):
            result = smb_shares.update_share(
                "wakacje", group_grants={"rodzina": "rw", "goscie": "ro"}, managed_conf_path=self.managed
            )
        self.assertTrue(result["success"])
        mock_sync.assert_called_once_with(
            "/srv/wakacje", {"rodzina": "rw", "goscie": "ro"}, {"rodzina": "rw"}
        )

    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    def test_permissions_none_leaves_group_grants_untouched(self, mock_apply, mock_smb):
        with mock.patch("nas_monitor.smb_shares._resolve_group_members", return_value=[]):
            result = smb_shares.update_share("wakacje", comment="nowy opis", managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        reread = smb_shares._read_managed_shares(self.managed)
        self.assertEqual(reread[0]["group_grants"], {"rodzina": "rw"})


class TestUpdateShareFirstEverGroupGrant(unittest.TestCase):
    """A share that started with zero access (created bare, or every
    grant since removed) getting its very first group_grant, with no
    individual permissions ever added, is the same gap
    TestCreateShareWithGroupGrants guards at creation time - its
    dedicated access group has never been created by anything yet."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.managed = os.path.join(self.tmpdir, "shares.conf")
        self.existing_share = {
            "name": "wakacje",
            "path": "/srv/wakacje",
            "comment": "",
            "permissions": {},
            "group_grants": {},
            "access_group": None,
        }
        with open(self.managed, "w") as fh:
            fh.write(smb_shares._render_managed_shares([self.existing_share]))

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @mock.patch("nas_monitor.smb_shares.smb_mod.list_samba_users", return_value={"available": False, "usernames": [], "error": None})
    @mock.patch("nas_monitor.smb_shares._validate_and_apply", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._prepare_share_directory", return_value={"success": True})
    @mock.patch("nas_monitor.smb_shares._sync_group_acls", return_value={"success": True, "warnings": []})
    @mock.patch("nas_monitor.smb_shares.grp.getgrnam", return_value=object())
    @mock.patch("nas_monitor.smb_shares.users_mod.ensure_group_exists", return_value={"success": True})
    def test_ensures_the_dedicated_group_exists(self, mock_ensure, mock_getgrnam, mock_sync, mock_dir, mock_apply, mock_smb):
        with mock.patch("nas_monitor.smb_shares._resolve_group_members", return_value=[]):
            result = smb_shares.update_share("wakacje", group_grants={"rodzina": "rw"}, managed_conf_path=self.managed)
        self.assertTrue(result["success"])
        mock_ensure.assert_called_once_with("wakacje_access")


class TestRemoveUserFromAllShares(unittest.TestCase):
    def test_removes_the_user_from_every_managed_share_that_has_them(self):
        shares_result = {"available": True, "shares": [
            {"name": "dane", "managed": True, "permissions": {"gosia": "rw", "malina": "ro"}},
            {"name": "filmy", "managed": True, "permissions": {"gosia": "ro"}},
            {"name": "inny", "managed": True, "permissions": {"malina": "rw"}},
        ]}
        with mock.patch.object(smb_shares, "list_shares", return_value=shares_result), \
             mock.patch.object(smb_shares, "update_share", return_value={"success": True}) as mock_update:
            result = smb_shares.remove_user_from_all_shares("gosia")

        self.assertTrue(result["success"])
        self.assertEqual(set(result["updated_shares"]), {"dane", "filmy"})
        # "inny" never had gosia - update_share must not even be called for it
        called_names = {c.args[0] for c in mock_update.call_args_list}
        self.assertEqual(called_names, {"dane", "filmy"})
        # the OTHER user's access on "dane" must be preserved, not wiped
        dane_call = next(c for c in mock_update.call_args_list if c.args[0] == "dane")
        self.assertEqual(dane_call.kwargs["permissions"], {"malina": "ro"})

    def test_never_touches_unmanaged_foreign_shares(self):
        shares_result = {"available": True, "shares": [
            {"name": "obcy", "managed": False, "permissions": {"gosia": "rw"}},
        ]}
        with mock.patch.object(smb_shares, "list_shares", return_value=shares_result), \
             mock.patch.object(smb_shares, "update_share") as mock_update:
            result = smb_shares.remove_user_from_all_shares("gosia")

        self.assertTrue(result["success"])
        self.assertEqual(result["updated_shares"], [])
        mock_update.assert_not_called()

    def test_no_op_when_user_has_no_access_anywhere(self):
        shares_result = {"available": True, "shares": [
            {"name": "dane", "managed": True, "permissions": {"malina": "rw"}},
        ]}
        with mock.patch.object(smb_shares, "list_shares", return_value=shares_result), \
             mock.patch.object(smb_shares, "update_share") as mock_update:
            result = smb_shares.remove_user_from_all_shares("gosia")

        self.assertEqual(result["updated_shares"], [])
        mock_update.assert_not_called()


class TestRemoveGroupFromAllShares(unittest.TestCase):
    def test_removes_the_group_from_every_managed_share_that_grants_it(self):
        shares_result = {"available": True, "shares": [
            {"name": "dane", "managed": True, "group_grants": {"rodzina": "rw", "znajomi": "ro"}},
            {"name": "filmy", "managed": True, "group_grants": {"rodzina": "ro"}},
            {"name": "inny", "managed": True, "group_grants": {"znajomi": "rw"}},
        ]}
        with mock.patch.object(smb_shares, "list_shares", return_value=shares_result), \
             mock.patch.object(smb_shares, "update_share", return_value={"success": True}) as mock_update:
            result = smb_shares.remove_group_from_all_shares("rodzina")

        self.assertTrue(result["success"])
        self.assertEqual(set(result["updated_shares"]), {"dane", "filmy"})
        called_names = {c.args[0] for c in mock_update.call_args_list}
        self.assertEqual(called_names, {"dane", "filmy"})
        dane_call = next(c for c in mock_update.call_args_list if c.args[0] == "dane")
        self.assertEqual(dane_call.kwargs["group_grants"], {"znajomi": "ro"})

    def test_never_touches_unmanaged_foreign_shares(self):
        shares_result = {"available": True, "shares": [
            {"name": "obcy", "managed": False, "group_grants": {"rodzina": "rw"}},
        ]}
        with mock.patch.object(smb_shares, "list_shares", return_value=shares_result), \
             mock.patch.object(smb_shares, "update_share") as mock_update:
            result = smb_shares.remove_group_from_all_shares("rodzina")

        self.assertEqual(result["updated_shares"], [])
        mock_update.assert_not_called()

    def test_never_touches_individual_permissions(self):
        shares_result = {"available": True, "shares": [
            {"name": "dane", "managed": True, "permissions": {"rodzina": "rw"}, "group_grants": {}},
        ]}
        with mock.patch.object(smb_shares, "list_shares", return_value=shares_result), \
             mock.patch.object(smb_shares, "update_share") as mock_update:
            result = smb_shares.remove_group_from_all_shares("rodzina")

        # "rodzina" only appears in permissions (a user, coincidentally
        # same name), not group_grants - must not be touched
        self.assertEqual(result["updated_shares"], [])
        mock_update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
