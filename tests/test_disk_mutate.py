from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import disk_mutate  # noqa: E402


def _fake_find_binary(name):
    return f"/usr/bin/{name}"


def _fake_run_factory(responses):
    """responses: dict mapping tool basename -> (code, out, err), or a
    callable(args) -> (code, out, err) for a tool invoked more than once
    with different arguments (lsblk here, for two unrelated purposes)."""

    def _fake_run(cmd, timeout=8, **kwargs):
        tool = os.path.basename(cmd[0])
        args = cmd[1:]
        handler = responses.get(tool)
        if handler is None:
            return (1, "", f"unmocked tool invocation: {tool} {args}")
        if callable(handler):
            return handler(args)
        return handler

    return _fake_run


DISKS = [
    {"name": "sda", "path": "/dev/sda", "size": "500 GiB"},
    {"name": "sdb", "path": "/dev/sdb", "size": "1 TiB"},
    {"name": "sdc", "path": "/dev/sdc", "size": "1 TiB"},
]

LSBLK_MOUNT_JSON = """
{
   "blockdevices": [
      {"name": "sda", "mountpoint": null, "type": "disk", "children": [
         {"name": "sda1", "mountpoint": "/boot", "type": "part"},
         {"name": "sda2", "mountpoint": "/", "type": "part"}
      ]},
      {"name": "sdb", "mountpoint": null, "type": "disk", "children": [
         {"name": "sdb1", "mountpoint": "/mnt/data", "type": "part"}
      ]},
      {"name": "sdc", "mountpoint": null, "type": "disk", "children": []}
   ]
}
"""


def _lsblk_handler(args):
    if "PKNAME" in args:
        return (0, "sda\n", "")
    return (0, LSBLK_MOUNT_JSON, "")


class TestDiskNameParsing(unittest.TestCase):
    def test_disk_name_from_sata_partition(self):
        self.assertEqual(disk_mutate._disk_name("/dev/sda1"), "sda")
        self.assertEqual(disk_mutate._disk_name("/dev/sda"), "sda")

    def test_disk_name_from_nvme_partition(self):
        self.assertEqual(disk_mutate._disk_name("/dev/nvme0n1p1"), "nvme0n1")
        self.assertEqual(disk_mutate._disk_name("/dev/nvme0n1"), "nvme0n1")

    def test_disk_name_from_sd_card_partition(self):
        self.assertEqual(disk_mutate._disk_name("/dev/mmcblk0p1"), "mmcblk0")
        self.assertEqual(disk_mutate._disk_name("/dev/mmcblk0"), "mmcblk0")

    def test_partition_path_sata(self):
        self.assertEqual(disk_mutate._partition_path("/dev/sda"), "/dev/sda1")

    def test_partition_path_nvme(self):
        self.assertEqual(disk_mutate._partition_path("/dev/nvme0n1"), "/dev/nvme0n1p1")

    def test_partition_path_sd_card(self):
        self.assertEqual(disk_mutate._partition_path("/dev/mmcblk0"), "/dev/mmcblk0p1")


class TestListRawDisks(unittest.TestCase):
    def test_excludes_boot_mounted_and_raid_member_disks(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            raw = disk_mutate.list_raw_disks()

        names = {d["name"] for d in raw}
        # sda excluded: boot disk. sdb excluded: has a mounted partition.
        # sdc: nothing mounted, not boot, not a RAID member - genuinely free.
        self.assertEqual(names, {"sdc"})

    def test_excludes_raid_member_disk(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
        }
        raid_arrays = [{"devices": [{"device": "/dev/sdc1"}]}]
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=raid_arrays):
            raw = disk_mutate.list_raw_disks()

        self.assertEqual(raw, [])  # sda=boot, sdb=mounted, sdc=RAID member


class TestCheckDiskSafeToModify(unittest.TestCase):
    def _run(self, device, raid_arrays=None):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=raid_arrays or []):
            return disk_mutate.check_disk_safe_to_modify(device)

    def test_rejects_boot_disk(self):
        result = self._run("/dev/sda")
        self.assertFalse(result["safe"])
        self.assertEqual(result["error_code"], "disks.is_boot_disk")

    def test_rejects_mounted_disk(self):
        result = self._run("/dev/sdb")
        self.assertFalse(result["safe"])
        self.assertEqual(result["error_code"], "disks.is_mounted")

    def test_rejects_raid_member_disk(self):
        result = self._run("/dev/sdc", raid_arrays=[{"devices": [{"device": "/dev/sdc1"}]}])
        self.assertFalse(result["safe"])
        self.assertEqual(result["error_code"], "disks.is_raid_member")

    def test_rejects_unknown_disk(self):
        result = self._run("/dev/sdz")
        self.assertFalse(result["safe"])
        self.assertEqual(result["error_code"], "disks.not_found")

    def test_accepts_genuinely_free_disk(self):
        result = self._run("/dev/sdc")
        self.assertTrue(result["safe"])


class TestWipeDisk(unittest.TestCase):
    def test_refuses_to_wipe_boot_disk(self):
        responses = {"findmnt": (0, "/dev/sda2\n", ""), "lsblk": _lsblk_handler}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            result = disk_mutate.wipe_disk("/dev/sda")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.is_boot_disk")

    def test_wipes_a_free_disk_successfully(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
            "wipefs": (0, "", ""),
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            result = disk_mutate.wipe_disk("/dev/sdc")

        self.assertTrue(result["success"])

    def test_wipefs_failure_is_surfaced(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
            "wipefs": (1, "", "device busy"),
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            result = disk_mutate.wipe_disk("/dev/sdc")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertIn("device busy", result["error_context"]["detail"])


class TestFormatDisk(unittest.TestCase):
    def test_rejects_unsupported_filesystem(self):
        result = disk_mutate.format_disk("/dev/sdc", "zfs")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.unsupported_filesystem")

    def test_refuses_to_format_a_raid_member(self):
        responses = {"findmnt": (0, "/dev/sda2\n", ""), "lsblk": _lsblk_handler}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays",
                                return_value=[{"devices": [{"device": "/dev/sdc1"}]}]):
            result = disk_mutate.format_disk("/dev/sdc", "ext4")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.is_raid_member")

    def test_full_success_path_creates_partition_and_formats_it(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
            "wipefs": (0, "", ""),
            "parted": (0, "", ""),
            "mkfs.ext4": (0, "", ""),
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate.os.path, "exists", return_value=True):
            result = disk_mutate.format_disk("/dev/sdc", "ext4")

        self.assertTrue(result["success"])
        self.assertEqual(result["partition"], "/dev/sdc1")

    def test_parted_failure_stops_before_mkfs(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
            "wipefs": (0, "", ""),
            "parted": (1, "", "unrecognised disk label"),
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)) as mock_run, \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            result = disk_mutate.format_disk("/dev/sdc", "ext4")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertNotIn("mkfs.ext4", [os.path.basename(c.args[0][0]) for c in mock_run.call_args_list])

    def test_partition_never_appears_reports_clear_error(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
            "wipefs": (0, "", ""),
            "parted": (0, "", ""),
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate.os.path, "exists", return_value=False), \
             mock.patch.object(disk_mutate.time, "sleep"):
            result = disk_mutate.format_disk("/dev/sdc", "ext4")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.partition_not_ready")

    def test_missing_mkfs_tool_is_reported(self):
        def find_binary(name):
            if name == "mkfs.btrfs":
                return None
            return f"/usr/bin/{name}"

        responses = {"findmnt": (0, "/dev/sda2\n", ""), "lsblk": _lsblk_handler}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            result = disk_mutate.format_disk("/dev/sdc", "btrfs")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.tool_missing")
        self.assertEqual(result["error_context"]["tool"], "mkfs.btrfs")


if __name__ == "__main__":
    unittest.main()
