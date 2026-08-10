from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import raid_mutate  # noqa: E402


FREE_DISKS = [
    {"name": "sdb", "path": "/dev/sdb", "fstype": None, "mounted": False, "is_raid_member": False},
    {"name": "sdc", "path": "/dev/sdc", "fstype": None, "mounted": False, "is_raid_member": False},
    {"name": "sdd", "path": "/dev/sdd", "fstype": None, "mounted": False, "is_raid_member": False},
    {"name": "sde", "path": "/dev/sde", "fstype": None, "mounted": False, "is_raid_member": False},
]


class TestMinDevicesForLevel(unittest.TestCase):
    def test_raid3_is_not_offered_at_all(self):
        # mdadm itself doesn't support RAID3 - confirmed via research
        # earlier in this project (see disk_mutate/monitor notes).
        self.assertNotIn("3", raid_mutate.MIN_DEVICES_FOR_LEVEL)


class TestCreateRaidArray(unittest.TestCase):
    def test_rejects_unsupported_level(self):
        result = raid_mutate.create_raid_array(["/dev/sdb", "/dev/sdc"], "3")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "raid.unsupported_level")

    def test_rejects_too_few_devices_for_the_level(self):
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=FREE_DISKS):
            result = raid_mutate.create_raid_array(["/dev/sdb", "/dev/sdc"], "5")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "raid.not_enough_devices")
        self.assertEqual(result["error_context"]["needed"], 3)
        self.assertEqual(result["error_context"]["given"], 2)

    def test_rejects_duplicate_device_in_the_list(self):
        result = raid_mutate.create_raid_array(["/dev/sdb", "/dev/sdb"], "1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "raid.duplicate_device")

    def test_rejects_a_device_this_tool_does_not_manage(self):
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=FREE_DISKS):
            result = raid_mutate.create_raid_array(["/dev/sdb", "/dev/sdzz"], "1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "raid.unknown_device")
        self.assertEqual(result["error_context"]["device"], "/dev/sdzz")

    def test_rejects_a_device_that_already_has_a_filesystem(self):
        disks = [dict(d) for d in FREE_DISKS]
        disks[0]["fstype"] = "ext4"
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=disks):
            result = raid_mutate.create_raid_array(["/dev/sdb", "/dev/sdc"], "1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "raid.device_not_free")
        self.assertEqual(result["error_context"]["device"], "/dev/sdb")

    def test_rejects_a_mounted_device(self):
        disks = [dict(d) for d in FREE_DISKS]
        disks[0]["mounted"] = True
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=disks):
            result = raid_mutate.create_raid_array(["/dev/sdb", "/dev/sdc"], "1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "raid.device_not_free")

    def test_rejects_a_device_already_in_another_array(self):
        disks = [dict(d) for d in FREE_DISKS]
        disks[0]["is_raid_member"] = True
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=disks):
            result = raid_mutate.create_raid_array(["/dev/sdb", "/dev/sdc"], "1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "raid.device_not_free")

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value=None)
    def test_reports_missing_mdadm_tool(self, mock_find):
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=FREE_DISKS):
            result = raid_mutate.create_raid_array(["/dev/sdb", "/dev/sdc"], "1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.tool_missing")

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value="/sbin/mdadm")
    @mock.patch.object(raid_mutate.system_tools, "run")
    def test_picks_the_first_free_array_name(self, mock_run, mock_find):
        mock_run.return_value = (0, "", "")
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=FREE_DISKS), \
             mock.patch.object(raid_mutate.monitor, "get_raid_arrays", return_value=[{"name": "md0"}]):
            result = raid_mutate.create_raid_array(["/dev/sdb", "/dev/sdc"], "1")
        self.assertTrue(result["success"])
        self.assertEqual(result["name"], "md1")
        self.assertEqual(result["path"], "/dev/md1")

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value="/sbin/mdadm")
    @mock.patch.object(raid_mutate.system_tools, "run")
    def test_builds_the_expected_mdadm_command(self, mock_run, mock_find):
        mock_run.return_value = (0, "", "")
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=FREE_DISKS), \
             mock.patch.object(raid_mutate.monitor, "get_raid_arrays", return_value=[]):
            raid_mutate.create_raid_array(["/dev/sdb", "/dev/sdc", "/dev/sdd"], "5")
        mock_run.assert_called_once_with(
            ["/sbin/mdadm", "--create", "/dev/md0", "--level=5", "--raid-devices=3", "--metadata=1.2", "--run",
             "/dev/sdb", "/dev/sdc", "/dev/sdd"],
            timeout=60,
        )

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value="/sbin/mdadm")
    @mock.patch.object(raid_mutate.system_tools, "run")
    def test_surfaces_mdadm_failure(self, mock_run, mock_find):
        mock_run.return_value = (1, "", "mdadm: super1.x cannot open /dev/sdb: Device or resource busy")
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=FREE_DISKS), \
             mock.patch.object(raid_mutate.monitor, "get_raid_arrays", return_value=[]):
            result = raid_mutate.create_raid_array(["/dev/sdb", "/dev/sdc"], "1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")


class TestDetachMember(unittest.TestCase):
    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value=None)
    def test_reports_missing_mdadm_tool(self, mock_find):
        result = raid_mutate.detach_member("md0", "/dev/sdb")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.tool_missing")

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value="/sbin/mdadm")
    @mock.patch.object(raid_mutate.system_tools, "run")
    def test_fails_then_removes_the_device(self, mock_run, mock_find):
        mock_run.return_value = (0, "", "")
        result = raid_mutate.detach_member("md0", "/dev/sdb")
        self.assertTrue(result["success"])
        mock_run.assert_any_call(["/sbin/mdadm", "--manage", "/dev/md0", "--fail", "/dev/sdb"], timeout=30)
        mock_run.assert_any_call(["/sbin/mdadm", "--manage", "/dev/md0", "--remove", "/dev/sdb"], timeout=30)

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value="/sbin/mdadm")
    @mock.patch.object(raid_mutate.system_tools, "run")
    def test_tolerates_fail_no_op_but_not_remove_failure(self, mock_run, mock_find):
        # --fail can legitimately no-op (device already gone/marked
        # failed) - only --remove actually failing should be an error.
        mock_run.side_effect = [(1, "", "mdadm: set device faulty failed"), (0, "", "")]
        result = raid_mutate.detach_member("md0", "/dev/sdb")
        self.assertTrue(result["success"])

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value="/sbin/mdadm")
    @mock.patch.object(raid_mutate.system_tools, "run")
    def test_surfaces_remove_failure(self, mock_run, mock_find):
        mock_run.side_effect = [(0, "", ""), (1, "", "mdadm: hot remove failed for /dev/sdb: Device or resource busy")]
        result = raid_mutate.detach_member("md0", "/dev/sdb")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")


class TestAddMember(unittest.TestCase):
    def test_rejects_a_device_this_tool_does_not_manage(self):
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=FREE_DISKS):
            result = raid_mutate.add_member("md0", "/dev/sdzz")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "raid.unknown_device")

    def test_rejects_a_device_that_is_not_free(self):
        disks = [dict(d) for d in FREE_DISKS]
        disks[0]["fstype"] = "ext4"
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=disks):
            result = raid_mutate.add_member("md0", "/dev/sdb")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "raid.device_not_free")

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value=None)
    def test_reports_missing_mdadm_tool(self, mock_find):
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=FREE_DISKS):
            result = raid_mutate.add_member("md0", "/dev/sdb")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.tool_missing")

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value="/sbin/mdadm")
    @mock.patch.object(raid_mutate.system_tools, "run")
    def test_adds_the_device(self, mock_run, mock_find):
        mock_run.return_value = (0, "", "")
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=FREE_DISKS):
            result = raid_mutate.add_member("md0", "/dev/sdb")
        self.assertTrue(result["success"])
        mock_run.assert_called_once_with(["/sbin/mdadm", "--manage", "/dev/md0", "--add", "/dev/sdb"], timeout=30)

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value="/sbin/mdadm")
    @mock.patch.object(raid_mutate.system_tools, "run")
    def test_surfaces_add_failure(self, mock_run, mock_find):
        mock_run.return_value = (1, "", "mdadm: add new device failed for /dev/sdb: Invalid argument")
        with mock.patch.object(raid_mutate.disk_mutate, "list_manageable_disks", return_value=FREE_DISKS):
            result = raid_mutate.add_member("md0", "/dev/sdb")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")


class TestDeleteRaidArray(unittest.TestCase):
    ARR = {
        "name": "md0",
        "path": "/dev/md0",
        "devices": [
            {"device": "/dev/sdb", "role": "0"},
            {"device": "/dev/sdc", "role": "1"},
        ],
    }

    def test_rejects_unknown_array(self):
        with mock.patch.object(raid_mutate.monitor, "get_raid_arrays", return_value=[]):
            result = raid_mutate.delete_raid_array("md0")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "raid.unknown_array")

    def test_rejects_a_still_mounted_array(self):
        with mock.patch.object(raid_mutate.monitor, "get_raid_arrays", return_value=[self.ARR]), \
             mock.patch.object(raid_mutate.disk_mutate, "list_manageable_raid_arrays", return_value=[{"name": "md0", "mounted": True}]):
            result = raid_mutate.delete_raid_array("md0")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "raid.still_mounted")

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value=None)
    def test_reports_missing_mdadm_tool(self, mock_find):
        with mock.patch.object(raid_mutate.monitor, "get_raid_arrays", return_value=[self.ARR]), \
             mock.patch.object(raid_mutate.disk_mutate, "list_manageable_raid_arrays", return_value=[{"name": "md0", "mounted": False}]):
            result = raid_mutate.delete_raid_array("md0")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.tool_missing")

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value="/sbin/mdadm")
    @mock.patch.object(raid_mutate.system_tools, "run")
    def test_stops_array_and_zeroes_every_member(self, mock_run, mock_find):
        mock_run.return_value = (0, "", "")
        with mock.patch.object(raid_mutate.monitor, "get_raid_arrays", return_value=[self.ARR]), \
             mock.patch.object(raid_mutate.disk_mutate, "list_manageable_raid_arrays", return_value=[{"name": "md0", "mounted": False}]), \
             mock.patch.object(raid_mutate.disk_labels, "set_label") as mock_set_label:
            result = raid_mutate.delete_raid_array("md0")
        self.assertTrue(result["success"])
        self.assertEqual(result["members"], ["/dev/sdb", "/dev/sdc"])
        mock_run.assert_any_call(["/sbin/mdadm", "--stop", "/dev/md0"], timeout=30)
        mock_run.assert_any_call(["/sbin/mdadm", "--zero-superblock", "/dev/sdb"], timeout=30)
        mock_run.assert_any_call(["/sbin/mdadm", "--zero-superblock", "/dev/sdc"], timeout=30)
        mock_set_label.assert_called_once_with("md0", "")

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value="/sbin/mdadm")
    @mock.patch.object(raid_mutate.system_tools, "run")
    def test_surfaces_stop_failure(self, mock_run, mock_find):
        mock_run.return_value = (1, "", "mdadm: Cannot get exclusive access to /dev/md0")
        with mock.patch.object(raid_mutate.monitor, "get_raid_arrays", return_value=[self.ARR]), \
             mock.patch.object(raid_mutate.disk_mutate, "list_manageable_raid_arrays", return_value=[{"name": "md0", "mounted": False}]):
            result = raid_mutate.delete_raid_array("md0")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")

    @mock.patch.object(raid_mutate.system_tools, "find_binary", return_value="/sbin/mdadm")
    @mock.patch.object(raid_mutate.system_tools, "run")
    def test_a_single_member_zero_superblock_failure_is_a_warning_not_a_failure(self, mock_run, mock_find):
        # --stop already succeeded (the part that actually matters) -
        # one member's zero-superblock failing must not make the whole
        # operation look like it failed.
        mock_run.side_effect = [(0, "", ""), (1, "", "mdadm: Unrecognised md component device"), (0, "", "")]
        with mock.patch.object(raid_mutate.monitor, "get_raid_arrays", return_value=[self.ARR]), \
             mock.patch.object(raid_mutate.disk_mutate, "list_manageable_raid_arrays", return_value=[{"name": "md0", "mounted": False}]), \
             mock.patch.object(raid_mutate.disk_labels, "set_label"):
            result = raid_mutate.delete_raid_array("md0")
        self.assertTrue(result["success"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["warnings"][0]["code"], "raid.zero_superblock_failed")
        self.assertEqual(result["warnings"][0]["context"]["device"], "/dev/sdb")


if __name__ == "__main__":
    unittest.main()
