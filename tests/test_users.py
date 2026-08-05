import os
import pwd
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
        self.assertEqual(result["error_code"], "users.invalid_username")
        mock_run.assert_not_called()

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    def test_rejects_existing_user(self, mock_exists):
        result = users.create_user("tomek")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "users.already_exists")
        self.assertEqual(result["error_context"]["username"], "tomek")

    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value=None)
    def test_missing_useradd_binary(self, mock_find, mock_exists):
        result = users.create_user("tomek")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.tool_missing")
        self.assertEqual(result["error_context"]["tool"], "useradd")

    @mock.patch("nas_monitor.users.default_nologin_shell", return_value="/usr/sbin/nologin")
    @mock.patch("nas_monitor.users.ensure_group_exists", return_value={"success": True})
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

    @mock.patch("nas_monitor.users.default_nologin_shell", return_value="/usr/sbin/nologin")
    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/useradd")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_group_creation_failure_aborts_before_useradd(self, mock_run, mock_find, mock_exists, mock_shell):
        with mock.patch(
            "nas_monitor.users.ensure_group_exists",
            return_value={"success": False, "error_code": "system.command_failed", "error_context": {"detail": "groupadd exited 1"}},
        ):
            result = users.create_user("share1", groups=["brandnew"])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertEqual(result["error_context"]["group"], "brandnew")
        mock_run.assert_not_called()  # useradd must never run if the group step failed

    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/useradd")
    def test_rejects_invalid_group_name(self, mock_find, mock_exists):
        result = users.create_user("share1", groups=["ok_group", "bad group!"])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "users.invalid_group_name")
        self.assertEqual(result["error_context"]["group"], "bad group!")

    @mock.patch("nas_monitor.users.default_nologin_shell", return_value="/usr/sbin/nologin")
    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/useradd")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(1, "", "useradd: some real failure"))
    def test_propagates_useradd_failure(self, mock_run, mock_find, mock_exists, mock_shell):
        result = users.create_user("share1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertIn("some real failure", result["error_context"]["detail"])

    @mock.patch("nas_monitor.users.default_nologin_shell", return_value="/usr/sbin/nologin")
    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/useradd")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_capitalized_input_becomes_lowercase_account_with_display_name(self, mock_run, mock_find, mock_exists, mock_shell):
        result = users.create_user("Tomek")
        self.assertTrue(result["success"])
        self.assertEqual(result["username"], "tomek")
        self.assertEqual(result["display_name"], "Tomek")
        called_cmd = mock_run.call_args[0][0]
        self.assertEqual(called_cmd[-1], "tomek")  # the actual system account is lowercase
        self.assertIn("-c", called_cmd)
        self.assertEqual(called_cmd[called_cmd.index("-c") + 1], "Tomek")  # GECOS keeps original casing

    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    def test_rejects_colon_in_display_name(self, mock_exists):
        result = users.create_user("Tom:ek")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "users.invalid_display_name")

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    def test_existence_check_uses_lowercased_name(self, mock_exists):
        # "Tomek" typed again should collide with existing account "tomek",
        # not silently create a second "Tomek"-cased duplicate
        result = users.create_user("Tomek")
        self.assertFalse(result["success"])
        mock_exists.assert_called_once_with("tomek")


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
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertIn("real failure", result["error_context"]["detail"])


class TestCreateGroup(unittest.TestCase):
    @mock.patch("nas_monitor.users.grp.getgrnam", side_effect=KeyError)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/groupadd")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_creates_new_group(self, mock_run, mock_find, mock_getgrnam):
        result = users.create_group("editors")
        self.assertTrue(result["success"])
        mock_run.assert_called_once_with(["/usr/sbin/groupadd", "editors"])

    @mock.patch("nas_monitor.users.grp.getgrnam", return_value=object())
    def test_rejects_duplicate_group(self, mock_getgrnam):
        result = users.create_group("existing")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "users.group_already_exists")

    def test_rejects_invalid_name(self):
        result = users.create_group("bad name!")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "users.invalid_group_name")


class TestDeleteGroup(unittest.TestCase):
    @mock.patch("nas_monitor.users.grp.getgrnam", return_value=object())
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/groupdel")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_deletes_existing_group(self, mock_run, mock_find, mock_getgrnam):
        result = users.delete_group("editors")
        self.assertTrue(result["success"])
        mock_run.assert_called_once_with(["/usr/sbin/groupdel", "editors"])

    @mock.patch("nas_monitor.users.grp.getgrnam", side_effect=KeyError)
    def test_rejects_nonexistent_group(self, mock_getgrnam):
        result = users.delete_group("ghost")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "users.group_not_found")

    @mock.patch("nas_monitor.users.grp.getgrnam", return_value=object())
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/groupdel")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(1, "", "groupdel: cannot remove the primary group of user 'x'"))
    def test_propagates_groupdel_failure_eg_primary_group(self, mock_run, mock_find, mock_getgrnam):
        result = users.delete_group("someones-primary")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertIn("primary group", result["error_context"]["detail"])


class TestUpdateUser(unittest.TestCase):
    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    def test_rejects_nonexistent_user(self, mock_exists):
        result = users.update_user("ghost", groups=["dane"])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "users.not_found")

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/usermod")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_noop_when_nothing_to_change(self, mock_run, mock_find, mock_exists):
        result = users.update_user("tomek")
        self.assertTrue(result["success"])
        mock_run.assert_not_called()

    @mock.patch("nas_monitor.users.ensure_group_exists", return_value={"success": True})
    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/usermod")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_sends_full_replacement_group_list(self, mock_run, mock_find, mock_exists, mock_ensure_group):
        result = users.update_user("tomek", groups=["dane", "backup"])
        self.assertTrue(result["success"])
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("-G", called_cmd)
        self.assertIn("dane,backup", called_cmd)
        self.assertEqual(called_cmd[-1], "tomek")

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/usermod")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_toggles_shell_for_login_capability(self, mock_run, mock_find, mock_exists):
        result = users.update_user("tomek", shell="/bin/bash")
        self.assertTrue(result["success"])
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("-s", called_cmd)
        self.assertIn("/bin/bash", called_cmd)

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    def test_rejects_colon_in_display_name(self, mock_exists):
        result = users.update_user("tomek", display_name="Tom:ek")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "users.invalid_display_name")

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/usermod")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(1, "", "usermod: real failure"))
    def test_propagates_usermod_failure(self, mock_run, mock_find, mock_exists):
        result = users.update_user("tomek", shell="/bin/bash")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertIn("real failure", result["error_context"]["detail"])


class TestDeleteUser(unittest.TestCase):
    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    def test_rejects_nonexistent_user(self, mock_exists):
        result = users.delete_user("ghost")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "users.not_found")
        self.assertEqual(result["error_context"]["username"], "ghost")

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/userdel")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_default_does_not_remove_home(self, mock_run, mock_find, mock_exists):
        result = users.delete_user("tomek")
        self.assertTrue(result["success"])
        called_cmd = mock_run.call_args[0][0]
        self.assertNotIn("-r", called_cmd)

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/userdel")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_remove_home_opt_in(self, mock_run, mock_find, mock_exists):
        result = users.delete_user("tomek", remove_home=True)
        self.assertTrue(result["success"])
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("-r", called_cmd)

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/userdel")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(1, "", "userdel: real failure"))
    def test_propagates_userdel_failure(self, mock_run, mock_find, mock_exists):
        result = users.delete_user("tomek")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertIn("real failure", result["error_context"]["detail"])


class TestGroupMembershipHelpers(unittest.TestCase):
    @mock.patch("nas_monitor.users.user_exists", return_value=False)
    def test_add_rejects_nonexistent_user(self, mock_exists):
        result = users.add_user_to_group("ghost", "dane_access")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "users.not_found")

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    @mock.patch("nas_monitor.users.ensure_group_exists", return_value={"success": True})
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/usermod")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_add_uses_append_flag_not_replace(self, mock_run, mock_find, mock_ensure, mock_exists):
        result = users.add_user_to_group("tomek", "dane_access")
        self.assertTrue(result["success"])
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("-aG", called_cmd)  # append, never bare -G (which would wipe other groups)
        self.assertNotIn("-G", [c for c in called_cmd if c == "-G"])

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    @mock.patch("nas_monitor.users._groups_for_user", return_value=["dane_access", "backup"])
    @mock.patch("nas_monitor.users.system_tools.find_binary", return_value="/usr/sbin/usermod")
    @mock.patch("nas_monitor.users.system_tools.run", return_value=(0, "", ""))
    def test_remove_keeps_other_memberships(self, mock_run, mock_find, mock_groups, mock_exists):
        result = users.remove_user_from_group("tomek", "dane_access")
        self.assertTrue(result["success"])
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("-G", called_cmd)
        self.assertIn("backup", called_cmd)
        self.assertNotIn("dane_access", called_cmd)

    @mock.patch("nas_monitor.users.user_exists", return_value=True)
    @mock.patch("nas_monitor.users._groups_for_user", return_value=["backup"])
    def test_remove_is_noop_if_not_a_member(self, mock_groups, mock_exists):
        result = users.remove_user_from_group("tomek", "dane_access")
        self.assertTrue(result["success"])


class TestListSystemUsersDisplayName(unittest.TestCase):
    def _fake_pwent(self, name, gecos, uid=1000):
        return pwd.struct_passwd((name, "x", uid, uid, gecos, f"/home/{name}", "/usr/sbin/nologin"))

    @mock.patch("nas_monitor.users.grp.getgrall", return_value=[])
    @mock.patch("nas_monitor.users.pwd.getpwall")
    def test_gecos_full_name_used_as_display_name(self, mock_getpwall, mock_getgrall):
        mock_getpwall.return_value = [self._fake_pwent("tomek", "Tomek")]
        result = users.list_system_users()
        self.assertEqual(result[0]["display_name"], "Tomek")
        self.assertEqual(result[0]["username"], "tomek")

    @mock.patch("nas_monitor.users.grp.getgrall", return_value=[])
    @mock.patch("nas_monitor.users.pwd.getpwall")
    def test_empty_gecos_falls_back_to_username(self, mock_getpwall, mock_getgrall):
        mock_getpwall.return_value = [self._fake_pwent("share1", "")]
        result = users.list_system_users()
        self.assertEqual(result[0]["display_name"], "share1")

    @mock.patch("nas_monitor.users.grp.getgrall", return_value=[])
    @mock.patch("nas_monitor.users.pwd.getpwall")
    def test_gecos_takes_only_first_comma_field(self, mock_getpwall, mock_getgrall):
        mock_getpwall.return_value = [self._fake_pwent("tomek", "Tomek,,,")]
        result = users.list_system_users()
        self.assertEqual(result[0]["display_name"], "Tomek")


class TestListSystemUsersAndGroups(unittest.TestCase):
    def test_returns_lists_without_crashing_on_real_system(self):
        # Not mocked on purpose - this sandbox is a real (if minimal) Linux
        # system, so this exercises the real pwd/grp database end to end.
        result = users.list_system_users()
        self.assertIsInstance(result, list)
        result_groups = users.list_system_groups()
        self.assertIsInstance(result_groups, list)


def _fake_grent(name, gid, members=None):
    import grp
    return grp.struct_group((name, "x", gid, members or []))


def _fake_pwent_with_shell(name, shell, uid=1000, home=None):
    home = home or f"/home/{name}"
    return pwd.struct_passwd((name, "x", uid, uid, "", home, shell))


class TestListSystemGroupsFiltersPrivateGroups(unittest.TestCase):
    @mock.patch("nas_monitor.users.grp.getgrall")
    @mock.patch("nas_monitor.users.pwd.getpwall")
    def test_excludes_each_users_own_private_group_and_nogroup(self, mock_getpwall, mock_getgrall):
        # Reproduces a real report: useradd's default "user private group"
        # scheme creates a same-named group as every user's primary group
        # (gosia, maliniak, wieslaw, nas-sync below) - these have zero
        # real members (primary-group membership lives in /etc/passwd's
        # GID field, never as a member entry in /etc/group), so they'd
        # otherwise show up in the Grupy tab looking like a pile of
        # empty, orphaned groups. 'nogroup' is Debian's standard
        # placeholder (GID 65534, well above the normal min_gid filter).
        mock_getpwall.return_value = [
            _fake_pwent_with_shell("gosia", "/usr/sbin/nologin", uid=1001),
            _fake_pwent_with_shell("maliniak", "/usr/sbin/nologin", uid=1002),
            _fake_pwent_with_shell("wieslaw", "/usr/sbin/nologin", uid=1003),
            _fake_pwent_with_shell("nas-sync", "/usr/sbin/nologin", uid=1004),
            _fake_pwent_with_shell("tomek", "/bin/bash", uid=1000),
        ]
        mock_getgrall.return_value = [
            _fake_grent("gosia", 1001, []),
            _fake_grent("maliniak", 1002, []),
            _fake_grent("wieslaw", 1003, []),
            _fake_grent("nas-sync", 1004, []),
            _fake_grent("tomek", 1000, ["tomek"]),
            _fake_grent("nogroup", 65534, []),
            _fake_grent("test_access", 2000, ["gosia", "wieslaw"]),
        ]
        result = users.list_system_groups()
        self.assertEqual([g["name"] for g in result], ["test_access"])

    @mock.patch("nas_monitor.users.grp.getgrall")
    @mock.patch("nas_monitor.users.pwd.getpwall")
    def test_keeps_a_namesake_group_if_no_longer_that_users_actual_primary_gid(self, mock_getpwall, mock_getgrall):
        # Edge case: a group named "gosia" only counts as gosia's private
        # group while it's ALSO her current primary GID. If she'd been
        # reassigned to a different primary group after creation (leaving
        # the old auto-created "gosia" group orphaned but unclaimed), it's
        # no longer functioning as anyone's private group and shouldn't
        # be filtered just because the name still matches a username.
        mock_getpwall.return_value = [_fake_pwent_with_shell("gosia", "/usr/sbin/nologin", uid=1001)]
        # her primary gid now points elsewhere (3000), not her own group's gid (1001)
        mock_getpwall.return_value = [pwd.struct_passwd(("gosia", "x", 1001, 3000, "", "/home/gosia", "/usr/sbin/nologin"))]
        mock_getgrall.return_value = [_fake_grent("gosia", 1001, [])]
        result = users.list_system_groups()
        self.assertEqual([g["name"] for g in result], ["gosia"])


if __name__ == "__main__":
    unittest.main()
