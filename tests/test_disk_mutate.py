from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import disk_mutate  # noqa: E402


def _fake_find_binary(name):
    return f"/usr/bin/{name}"


# Real os.path.exists, captured before any test patches it - used as the
# fallback in _partition_exists_stub below so patching "the partition
# node showed up" doesn't also make os.makedirs() elsewhere in the same
# call (see _mount_and_persist) think directories exist that don't.
_real_path_exists = os.path.exists


def _partition_exists_stub(path):
    if re.match(r"^/dev/[a-z0-9]+\d+$", path):
        return True
    return _real_path_exists(path)


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
    device = args[-1] if args and args[-1].startswith("/dev/") else None
    if device is None:
        return (0, LSBLK_MOUNT_JSON, "")
    # Device-scoped query (e.g. `lsblk -J -o ... /dev/sdb`) - real lsblk
    # only ever returns that one disk's own subtree, not everyone
    # else's too, so the mock has to actually filter rather than always
    # handing back the full multi-disk blob regardless of what was
    # asked for (a query for sdb must never see sda's /boot entry).
    import json as _json
    name = device.rsplit("/", 1)[-1]
    full = _json.loads(LSBLK_MOUNT_JSON)
    match = next((d for d in full["blockdevices"] if d["name"] == name), None)
    return (0, _json.dumps({"blockdevices": [match] if match else []}), "")


class TestDefaultMountName(unittest.TestCase):
    def test_uses_serial_when_available(self):
        disks = [{"name": "sdc", "serial": "G0Z056222"}]
        with mock.patch.object(disk_mutate.monitor, "list_disks", return_value=disks):
            self.assertEqual(disk_mutate._default_mount_name("/dev/sdc"), "G0Z056222")

    def test_sanitizes_the_serial_to_the_safe_charset(self):
        # real serials can contain spaces or other characters that
        # aren't safe as a bare directory name component
        disks = [{"name": "sdc", "serial": "WD WCC 4J1234567"}]
        with mock.patch.object(disk_mutate.monitor, "list_disks", return_value=disks):
            self.assertEqual(disk_mutate._default_mount_name("/dev/sdc"), "WDWCC4J1234567")

    def test_falls_back_to_disk_name_when_serial_is_the_unknown_placeholder(self):
        # monitor.list_disks() itself reports "unknown" for a drive
        # that doesn't expose a serial - using that verbatim as a mount
        # name would collide the moment a second such drive shows up
        disks = [{"name": "sdc", "serial": "unknown"}]
        with mock.patch.object(disk_mutate.monitor, "list_disks", return_value=disks):
            self.assertEqual(disk_mutate._default_mount_name("/dev/sdc"), "sdc")

    def test_falls_back_to_disk_name_when_serial_is_empty(self):
        disks = [{"name": "sdc", "serial": ""}]
        with mock.patch.object(disk_mutate.monitor, "list_disks", return_value=disks):
            self.assertEqual(disk_mutate._default_mount_name("/dev/sdc"), "sdc")

    def test_falls_back_to_disk_name_when_disk_not_found_at_all(self):
        with mock.patch.object(disk_mutate.monitor, "list_disks", return_value=[]):
            self.assertEqual(disk_mutate._default_mount_name("/dev/sdc"), "sdc")


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

    def test_disk_name_from_raid_array(self):
        # Real bug this caught: the generic letters+digits fallback
        # stripped an array's own trailing number the same way it
        # would a partition number ("md0" -> "md", pointing at a
        # device that doesn't exist), silently breaking serial lookup
        # and mount naming for every array.
        self.assertEqual(disk_mutate._disk_name("/dev/md0"), "md0")
        self.assertEqual(disk_mutate._disk_name("/dev/md127"), "md127")
        self.assertEqual(disk_mutate._disk_name("/dev/md0p1"), "md0")

    def test_partition_path_sata(self):
        self.assertEqual(disk_mutate._partition_path("/dev/sda"), "/dev/sda1")

    def test_partition_path_nvme(self):
        self.assertEqual(disk_mutate._partition_path("/dev/nvme0n1"), "/dev/nvme0n1p1")

    def test_partition_path_sd_card(self):
        self.assertEqual(disk_mutate._partition_path("/dev/mmcblk0"), "/dev/mmcblk0p1")


class TestDiskState(unittest.TestCase):
    def test_reports_fstype_and_mountpoint_of_whole_disk(self):
        lsblk_json = '{"blockdevices": [{"name": "sdc", "fstype": "ext4", "mountpoint": "/srv/dane", "children": []}]}'
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory({"lsblk": (0, lsblk_json, "")})):
            state = disk_mutate._disk_state("/dev/sdc")
        self.assertEqual(state, {"fstype": "ext4", "mountpoint": "/srv/dane", "device_node": "sdc"})

    def test_reports_state_of_first_partition_when_disk_itself_is_bare(self):
        lsblk_json = '{"blockdevices": [{"name": "sdc", "fstype": null, "mountpoint": null, "children": [{"name": "sdc1", "fstype": "xfs", "mountpoint": "/srv/dane"}]}]}'
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory({"lsblk": (0, lsblk_json, "")})):
            state = disk_mutate._disk_state("/dev/sdc")
        self.assertEqual(state, {"fstype": "xfs", "mountpoint": "/srv/dane", "device_node": "sdc1"})

    def test_none_none_when_genuinely_blank(self):
        lsblk_json = '{"blockdevices": [{"name": "sdc", "fstype": null, "mountpoint": null, "children": []}]}'
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory({"lsblk": (0, lsblk_json, "")})):
            state = disk_mutate._disk_state("/dev/sdc")
        self.assertEqual(state, {"fstype": None, "mountpoint": None, "device_node": None})


class TestListManageableRaidArrays(unittest.TestCase):
    def test_shape_matches_manageable_disks_for_shared_ui_rendering(self):
        arrays = [{"name": "md0", "path": "/dev/md0", "level": "5", "devices": [{"device": "/dev/sdb"}, {"device": "/dev/sdc"}, {"device": "/dev/sdd"}], "error": None}]
        lsblk_json = '{"blockdevices": [{"name": "md0", "fstype": "ext4", "mountpoint": "/srv/md0", "children": []}]}'
        size_json = '{"blockdevices": [{"size": 4000000000000}]}'

        def lsblk_dispatch(args, timeout=10):
            if "SIZE" in args:
                return (0, size_json, "")
            return (0, lsblk_json, "")

        with mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=arrays), \
             mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=lsblk_dispatch):
            result = disk_mutate.list_manageable_raid_arrays()

        self.assertEqual(len(result), 1)
        entry = result[0]
        self.assertEqual(entry["name"], "md0")
        self.assertEqual(entry["path"], "/dev/md0")
        self.assertEqual(entry["fstype"], "ext4")
        self.assertEqual(entry["mount_point"], "/srv/md0")
        self.assertTrue(entry["mounted"])
        self.assertFalse(entry["is_raid_member"])
        self.assertIn("RAID5", entry["model"])
        self.assertIn("3", entry["model"])
        # every key list_manageable_disks() entries have too, for the
        # frontend to be able to concatenate both lists and render
        # them identically without special-casing
        for key in ("name", "path", "size", "model", "serial", "transport", "fstype", "mount_point", "mounted", "is_raid_member", "label"):
            self.assertIn(key, entry)

    def test_skips_an_array_that_failed_detection(self):
        arrays = [{"name": "md0", "path": "/dev/md0", "error": "mdadm not installed"}]
        with mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=arrays):
            self.assertEqual(disk_mutate.list_manageable_raid_arrays(), [])


class TestListMountedRaidArrays(unittest.TestCase):
    def test_includes_an_array_mounted_under_srv(self):
        arrays = [{"name": "md0", "path": "/dev/md0", "error": None}]
        lsblk_json = '{"blockdevices": [{"name": "md0", "fstype": "ext4", "mountpoint": "/srv/dane-raid", "children": []}]}'
        with mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=arrays), \
             mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory({"lsblk": (0, lsblk_json, "")})):
            result = disk_mutate.list_mounted_raid_arrays()
        self.assertEqual(result, [{"name": "md0", "mount_point": "/srv/dane-raid", "fstype": "ext4"}])

    def test_excludes_an_array_not_mounted_under_srv(self):
        arrays = [{"name": "md0", "path": "/dev/md0", "error": None}]
        lsblk_json = '{"blockdevices": [{"name": "md0", "fstype": "ext4", "mountpoint": "/mnt/elsewhere", "children": []}]}'
        with mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=arrays), \
             mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory({"lsblk": (0, lsblk_json, "")})):
            result = disk_mutate.list_mounted_raid_arrays()
        self.assertEqual(result, [])

    def test_excludes_an_unmounted_array(self):
        arrays = [{"name": "md0", "path": "/dev/md0", "error": None}]
        lsblk_json = '{"blockdevices": [{"name": "md0", "fstype": null, "mountpoint": null, "children": []}]}'
        with mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=arrays), \
             mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory({"lsblk": (0, lsblk_json, "")})):
            result = disk_mutate.list_mounted_raid_arrays()
        self.assertEqual(result, [])

    def test_skips_an_array_that_failed_detection(self):
        arrays = [{"name": "md0", "path": "/dev/md0", "error": "mdadm not installed"}]
        with mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=arrays), \
             mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary):
            result = disk_mutate.list_mounted_raid_arrays()
        self.assertEqual(result, [])

    def test_no_arrays_at_all(self):
        with mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            self.assertEqual(disk_mutate.list_mounted_raid_arrays(), [])


class TestListManageableDisks(unittest.TestCase):
    def test_excludes_a_non_boot_disk_carrying_a_stray_system_partition(self):
        # Regression test for a real report: a disk that was NOT the
        # detected boot disk still showed up in the manageable-disks
        # table because it happened to carry a leftover /boot/efi
        # partition (from earlier, unrelated testing) - _boot_disk_name
        # correctly excludes the actual boot disk as a whole, but this
        # is the separate, independent guard for any *other* disk
        # carrying a system partition.
        def lsblk_for_device(args):
            device = args[-1]
            if device == "/dev/sdb":
                return (0, '{"blockdevices": [{"name": "sdb", "fstype": null, "mountpoint": null, "children": [{"name": "sdb1", "fstype": "vfat", "mountpoint": "/boot/efi"}]}]}', "")
            return (0, '{"blockdevices": [{"name": "sdc", "fstype": null, "mountpoint": null, "children": []}]}', "")

        responses = {"findmnt": (0, "/dev/sda2\n", ""), "lsblk": lsblk_for_device}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate, "_boot_disk_name", return_value="sda"):
            manageable = disk_mutate.list_manageable_disks()

        names = {d["name"] for d in manageable}
        self.assertNotIn("sdb", names)
        self.assertIn("sdc", names)

    def test_excludes_a_disk_showing_active_swap(self):
        def lsblk_for_device(args):
            device = args[-1]
            if device == "/dev/sdb":
                return (0, '{"blockdevices": [{"name": "sdb", "fstype": null, "mountpoint": null, "children": [{"name": "sdb1", "fstype": "swap", "mountpoint": "[SWAP]"}]}]}', "")
            return (0, '{"blockdevices": [{"name": "sdc", "fstype": null, "mountpoint": null, "children": []}]}', "")

        responses = {"findmnt": (0, "/dev/sda2\n", ""), "lsblk": lsblk_for_device}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate, "_boot_disk_name", return_value="sda"):
            manageable = disk_mutate.list_manageable_disks()

        self.assertNotIn("sdb", {d["name"] for d in manageable})

    def test_includes_every_disk_except_boot(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            manageable = disk_mutate.list_manageable_disks()

        names = {d["name"] for d in manageable}
        # sda excluded: it's the boot disk. sdb and sdc both included
        # regardless of state - sdb is mounted, sdc is genuinely free -
        # this table manages every non-boot disk, not just empty ones.
        self.assertEqual(names, {"sdb", "sdc"})

    def test_flags_mounted_state_and_mount_point_per_disk(self):
        # A device-aware lsblk stub this time, since _disk_state queries
        # one specific device per call - the shared _lsblk_handler
        # (used elsewhere for the no-argument "every mount on the
        # system" bulk query) always returns the same multi-disk blob
        # regardless of which device was asked about, which isn't
        # accurate for this particular call shape.
        def lsblk_for_device(args):
            device = args[-1]
            if device == "/dev/sdb":
                return (0, '{"blockdevices": [{"name": "sdb", "fstype": null, "mountpoint": null, "children": [{"name": "sdb1", "fstype": "ext4", "mountpoint": "/mnt/data"}]}]}', "")
            return (0, '{"blockdevices": [{"name": "sdc", "fstype": null, "mountpoint": null, "children": []}]}', "")

        responses = {"findmnt": (0, "/dev/sda2\n", ""), "lsblk": lsblk_for_device}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate, "_boot_disk_name", return_value="sda"):
            manageable = disk_mutate.list_manageable_disks()

        by_name = {d["name"]: d for d in manageable}
        self.assertTrue(by_name["sdb"]["mounted"])
        self.assertEqual(by_name["sdb"]["mount_point"], "/mnt/data")
        self.assertFalse(by_name["sdc"]["mounted"])
        self.assertIsNone(by_name["sdc"]["mount_point"])

    def test_flags_raid_membership(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
        }
        raid_arrays = [{"devices": [{"device": "/dev/sdc1"}]}]
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=raid_arrays):
            manageable = disk_mutate.list_manageable_disks()

        by_name = {d["name"]: d for d in manageable}
        self.assertTrue(by_name["sdc"]["is_raid_member"])
        self.assertFalse(by_name["sdb"]["is_raid_member"])

    def test_one_disk_state_lookup_failing_does_not_hide_the_others(self):
        # Same regression as monitor.get_full_status()'s equivalent
        # test - one disk's state lookup blowing up must not take the
        # whole management table down with it.
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate, "_disk_state", side_effect=RuntimeError("lsblk exploded")):
            manageable = disk_mutate.list_manageable_disks()

        # both non-boot disks are still there (state simply unknown), not vanished
        names = {d["name"] for d in manageable}
        self.assertEqual(names, {"sdb", "sdc"})
        for disk in manageable:
            self.assertIsNone(disk["fstype"])
            self.assertIsNone(disk["mount_point"])
            self.assertFalse(disk["mounted"])


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
        result = self._run("/dev/sdc", raid_arrays=[{"name": "md0", "devices": [{"device": "/dev/sdc1"}]}])
        self.assertFalse(result["safe"])
        self.assertEqual(result["error_code"], "disks.is_raid_member")

    def test_rejects_unknown_disk(self):
        result = self._run("/dev/sdz")
        self.assertFalse(result["safe"])
        self.assertEqual(result["error_code"], "disks.not_found")

    def test_accepts_genuinely_free_disk(self):
        result = self._run("/dev/sdc")
        self.assertTrue(result["safe"])


class TestMountDisk(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.fstab_path = os.path.join(self.tmpdir, "fstab")
        open(self.fstab_path, "w").close()
        self.mount_base = os.path.join(self.tmpdir, "srv")
        self.mount_base_patch = mock.patch.object(disk_mutate, "MOUNT_BASE", self.mount_base)
        self.fstab_patch = mock.patch.object(disk_mutate, "FSTAB_PATH", self.fstab_path)
        self.mount_base_patch.start()
        self.fstab_patch.start()

    def tearDown(self):
        self.mount_base_patch.stop()
        self.fstab_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rejects_a_disk_with_no_filesystem(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": (0, '{"blockdevices": [{"name": "sdc", "fstype": null, "mountpoint": null, "children": []}]}', ""),
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            result = disk_mutate.mount_disk("/dev/sdc", label="dane")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.no_filesystem")

    def test_rejects_boot_disk(self):
        responses = {"findmnt": (0, "/dev/sda2\n", ""), "lsblk": _lsblk_handler}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            result = disk_mutate.mount_disk("/dev/sda", label="dane")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.is_boot_disk")

    def test_rejects_already_mounted_disk(self):
        responses = {"findmnt": (0, "/dev/sda2\n", ""), "lsblk": _lsblk_handler}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            result = disk_mutate.mount_disk("/dev/sdb", label="dane")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.is_mounted")

    def test_mounts_at_serial_based_path_regardless_of_label(self):
        def lsblk_for_call(args):
            if "PKNAME" in args:
                return (0, "sda\n", "")
            device = args[-1] if args and args[-1].startswith("/dev/") else None
            if device == "/dev/sdc":
                return (0, '{"blockdevices": [{"name": "sdc", "fstype": null, "mountpoint": null, "children": [{"name": "sdc1", "fstype": "ext4", "mountpoint": null}]}]}', "")
            return (0, LSBLK_MOUNT_JSON, "")

        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": lsblk_for_call,
            "blkid": (0, "1234-ABCD-uuid\n", ""),
            "mount": (0, "", ""),
        }
        disks_with_serial = [dict(d) for d in DISKS]
        disks_with_serial[2]["serial"] = "G0Z056222"  # sdc
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)) as mock_run, \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=disks_with_serial), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate.disk_labels, "set_label") as mock_set_label:
            # a label IS given, but must never influence the path itself
            # (his explicit design call, v0.14.4) - it only gets saved
            # as a cosmetic display name via disk_labels
            result = disk_mutate.mount_disk("/dev/sdc", label="dane")

        self.assertTrue(result["success"], result)
        self.assertEqual(result["mount_point"], os.path.join(self.mount_base, "G0Z056222"))
        mock_set_label.assert_called_once_with("G0Z056222", "dane")
        with open(self.fstab_path) as f:
            fstab_content = f.read()
        self.assertIn("1234-ABCD-uuid", fstab_content)
        blkid_call = next(c for c in mock_run.call_args_list if os.path.basename(c.args[0][0]) == "blkid")
        self.assertIn("/dev/sdc1", blkid_call.args[0])

    def test_rejects_overly_long_label(self):
        result = disk_mutate.mount_disk("/dev/sdc", label="x" * 100)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.label_too_long")

    def test_label_is_entirely_optional(self):
        # No label at all is completely fine now - mounting always uses
        # the serial (or disk name, with no serial available in this
        # fixture) regardless, so there's nothing a label is required
        # for anymore.
        def lsblk_for_call(args):
            if "PKNAME" in args:
                return (0, "sda\n", "")
            device = args[-1] if args and args[-1].startswith("/dev/") else None
            if device == "/dev/sdc":
                return (0, '{"blockdevices": [{"name": "sdc", "fstype": null, "mountpoint": null, "children": [{"name": "sdc1", "fstype": "ext4", "mountpoint": null}]}]}', "")
            return (0, LSBLK_MOUNT_JSON, "")

        responses = {"findmnt": (0, "/dev/sda2\n", ""), "lsblk": lsblk_for_call, "blkid": (0, "1234-ABCD-uuid\n", ""), "mount": (0, "", "")}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]):
            result = disk_mutate.mount_disk("/dev/sdc")

        self.assertTrue(result["success"], result)
        self.assertEqual(result["mount_point"], os.path.join(self.mount_base, "sdc"))  # no serial in fixture -> falls back to disk name


class TestUnmountDisk(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.fstab_path = os.path.join(self.tmpdir, "fstab")
        self.mount_base = os.path.join(self.tmpdir, "mnt")
        self.fstab_patch = mock.patch.object(disk_mutate, "FSTAB_PATH", self.fstab_path)
        self.mount_base_patch = mock.patch.object(disk_mutate, "MOUNT_BASE", self.mount_base)
        self.fstab_patch.start()
        self.mount_base_patch.start()

    def tearDown(self):
        self.fstab_patch.stop()
        self.mount_base_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _lsblk_mounted_under_our_base(self, args):
        mount_point = f"{self.mount_base}/dane"
        return (0, f'{{"blockdevices": [{{"name": "sdc", "mountpoint": null, "children": [{{"name": "sdc1", "mountpoint": "{mount_point}"}}]}}]}}', "")

    def test_unmounts_and_removes_fstab_entry(self):
        mount_point = f"{self.mount_base}/dane"
        with open(self.fstab_path, "w") as f:
            f.write(f"UUID=1234-ABCD  {mount_point}  ext4  defaults,nofail  0  2\n")
            f.write("UUID=OTHER-UUID  /mnt/other  ext4  defaults,nofail  0  2\n")

        responses = {"lsblk": self._lsblk_mounted_under_our_base, "umount": (0, "", "")}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)) as mock_run, \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate, "_raid_member_disk_names", return_value=set()):
            result = disk_mutate.unmount_disk("/dev/sdc")

        self.assertTrue(result["success"])
        umount_call = next(c for c in mock_run.call_args_list if os.path.basename(c.args[0][0]) == "umount")
        self.assertIn(mount_point, umount_call.args[0])
        with open(self.fstab_path) as f:
            content = f.read()
        self.assertNotIn(mount_point, content)
        self.assertIn("OTHER-UUID", content)  # unrelated entries untouched

    def test_unmounts_a_disk_mounted_outside_our_own_convention(self):
        # Real report: a desktop session's own automounter mounting a
        # USB drive at /media/<user>/<label> left it with no Unmount
        # option at all - already mounted (so Format/Wipe don't apply),
        # and not under MOUNT_BASE (so the old, stricter check refused
        # it too) - a dead end. The boot-disk/RAID-member checks are
        # the actual safety net, not the mount path.
        lsblk_json = '{"blockdevices": [{"name": "sdc", "mountpoint": null, "children": [{"name": "sdc1", "mountpoint": "/media/tomek/Test"}]}]}'
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory({"lsblk": (0, lsblk_json, ""), "umount": (0, "", "")})) as mock_run, \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate, "_raid_member_disk_names", return_value=set()):
            result = disk_mutate.unmount_disk("/dev/sdc")

        self.assertTrue(result["success"])
        umount_call = next(c for c in mock_run.call_args_list if os.path.basename(c.args[0][0]) == "umount")
        self.assertIn("/media/tomek/Test", umount_call.args[0])

    def test_refuses_a_disk_that_is_not_mounted_at_all(self):
        lsblk_json = '{"blockdevices": [{"name": "sdc", "mountpoint": null, "children": []}]}'
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory({"lsblk": (0, lsblk_json, "")})), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate, "_raid_member_disk_names", return_value=set()):
            result = disk_mutate.unmount_disk("/dev/sdc")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.not_mounted")

    def test_refuses_raid_member(self):
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate, "_raid_member_disk_names", return_value={"sdc"}):
            result = disk_mutate.unmount_disk("/dev/sdc")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.is_raid_member")

    def test_refuses_boot_disk_even_if_mounted_under_our_base(self):
        # A boot disk that also happens to carry a spare partition
        # mounted under /mnt/ must still be refused outright - this is
        # a hard rule of its own, not just a side effect of the /mnt/
        # check (see the real report this guards against: the Unmount
        # button showing up on the system disk's own card).
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate, "_boot_disk_name", return_value="sdc"), \
             mock.patch.object(disk_mutate, "_raid_member_disk_names", return_value=set()):
            result = disk_mutate.unmount_disk("/dev/sdc")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.is_boot_disk")

    def test_umount_failure_is_surfaced_and_fstab_untouched(self):
        mount_point = f"{self.mount_base}/dane"
        with open(self.fstab_path, "w") as f:
            f.write(f"UUID=1234-ABCD  {mount_point}  ext4  defaults,nofail  0  2\n")

        responses = {"lsblk": self._lsblk_mounted_under_our_base, "umount": (1, "", "target is busy")}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate, "_raid_member_disk_names", return_value=set()):
            result = disk_mutate.unmount_disk("/dev/sdc")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")
        with open(self.fstab_path) as f:
            self.assertIn(mount_point, f.read())  # untouched - umount never actually succeeded


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
    def setUp(self):
        # _mount_and_persist touches real system paths (/mnt, /etc/fstab)
        # by default - redirect both to a scratch tempdir for every test
        # in this class so nothing here ever writes to the real machine
        # running the test suite.
        self.tmpdir = tempfile.mkdtemp()
        self.fstab_path = os.path.join(self.tmpdir, "fstab")
        open(self.fstab_path, "w").close()
        self.mount_base = os.path.join(self.tmpdir, "mnt")
        self.mount_base_patch = mock.patch.object(disk_mutate, "MOUNT_BASE", self.mount_base)
        self.fstab_patch = mock.patch.object(disk_mutate, "FSTAB_PATH", self.fstab_path)
        self.mount_base_patch.start()
        self.fstab_patch.start()

    def tearDown(self):
        self.mount_base_patch.stop()
        self.fstab_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rejects_unsupported_filesystem(self):
        result = disk_mutate.format_disk("/dev/sdc", "zfs")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.unsupported_filesystem")

    def test_passes_label_to_mkfs(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
            "wipefs": (0, "", ""),
            "parted": (0, "", ""),
            "mkfs.ext4": (0, "", ""),
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)) as mock_run, \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate.os.path, "exists", side_effect=_partition_exists_stub):
            result = disk_mutate.format_disk("/dev/sdc", "ext4", label="dane", auto_mount=False)

        self.assertTrue(result["success"])
        mkfs_call = next(c for c in mock_run.call_args_list if os.path.basename(c.args[0][0]) == "mkfs.ext4")
        self.assertIn("-L", mkfs_call.args[0])
        self.assertIn("dane", mkfs_call.args[0])

    def test_auto_mount_false_never_touches_mount_or_fstab(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
            "wipefs": (0, "", ""),
            "parted": (0, "", ""),
            "mkfs.ext4": (0, "", ""),
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)) as mock_run, \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate.os.path, "exists", side_effect=_partition_exists_stub):
            result = disk_mutate.format_disk("/dev/sdc", "ext4", auto_mount=False)

        self.assertTrue(result["success"])
        self.assertNotIn("warnings", result)
        used_tools = {os.path.basename(c.args[0][0]) for c in mock_run.call_args_list}
        self.assertNotIn("blkid", used_tools)
        self.assertNotIn("mount", used_tools)
        self.assertFalse(os.path.isdir(self.mount_base))

    def test_auto_mount_success_creates_fstab_entry_and_mounts(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
            "wipefs": (0, "", ""),
            "parted": (0, "", ""),
            "mkfs.ext4": (0, "", ""),
            "blkid": (0, "1234-ABCD-uuid\n", ""),
            "mount": (0, "", ""),
        }
        disks_with_serial = [dict(d) for d in DISKS]
        disks_with_serial[2]["serial"] = "G0Z056222"  # sdc
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)) as mock_run, \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=disks_with_serial), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate.os.path, "exists", side_effect=_partition_exists_stub), \
             mock.patch.object(disk_mutate.disk_labels, "set_label") as mock_set_label:
            # a label IS given, but must never influence the path itself
            result = disk_mutate.format_disk("/dev/sdc", "ext4", label="dane")

        self.assertTrue(result["success"])
        self.assertNotIn("warnings", result)
        self.assertEqual(result["mount_point"], os.path.join(self.mount_base, "G0Z056222"))
        self.assertTrue(os.path.isdir(result["mount_point"]))
        mock_set_label.assert_called_once_with("G0Z056222", "dane")
        with open(self.fstab_path) as f:
            fstab_content = f.read()
        self.assertIn("1234-ABCD-uuid", fstab_content)
        self.assertIn(result["mount_point"], fstab_content)
        self.assertIn("nofail", fstab_content)
        mount_call = next(c for c in mock_run.call_args_list if os.path.basename(c.args[0][0]) == "mount")
        self.assertIn(result["mount_point"], mount_call.args[0])

    def test_auto_mount_uses_disk_name_when_no_serial_or_label(self):
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
            "wipefs": (0, "", ""),
            "parted": (0, "", ""),
            "mkfs.ext4": (0, "", ""),
            "blkid": (0, "1234-ABCD-uuid\n", ""),
            "mount": (0, "", ""),
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate.os.path, "exists", side_effect=_partition_exists_stub):
            # No label at all - completely fine now (v0.14.4), never
            # required, since the mount path never came from it anyway.
            result = disk_mutate.format_disk("/dev/sdc", "ext4")

        self.assertTrue(result["success"])
        self.assertEqual(result["mount_point"], os.path.join(self.mount_base, "sdc"))

    def test_rejects_overly_long_label(self):
        result = disk_mutate.format_disk("/dev/sdc", "ext4", label="x" * 100)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "disks.label_too_long")

    def test_auto_mount_failure_is_a_warning_not_a_failure(self):
        # blkid failing (or missing) means no UUID, which means no safe
        # fstab entry can be written - the filesystem itself is still
        # correctly created, so this must not turn the whole format
        # into a reported failure, just flag that mounting needs doing
        # by hand.
        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
            "wipefs": (0, "", ""),
            "parted": (0, "", ""),
            "mkfs.ext4": (0, "", ""),
            "blkid": (1, "", "no such device"),
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate.os.path, "exists", side_effect=_partition_exists_stub):
            result = disk_mutate.format_disk("/dev/sdc", "ext4", label="dane")

        self.assertTrue(result["success"])
        self.assertIsNone(result["mount_point"])
        self.assertEqual(result["warnings"][0]["code"], "disks.uuid_not_found")

    def test_mount_and_persist_does_not_duplicate_an_existing_fstab_entry(self):
        with open(self.fstab_path, "w") as f:
            f.write("UUID=1234-ABCD-uuid  /mnt/dane  ext4  defaults,nofail  0  2\n")

        responses = {"blkid": (0, "1234-ABCD-uuid\n", ""), "mount": (0, "", "")}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)):
            result = disk_mutate._mount_and_persist("/dev/sdc1", "ext4", "dane")

        self.assertTrue(result["success"])
        with open(self.fstab_path) as f:
            content = f.read()
        # exactly one line for this UUID, not two
        self.assertEqual(content.count("1234-ABCD-uuid"), 1)

    def test_refuses_to_format_a_raid_member(self):
        responses = {"findmnt": (0, "/dev/sda2\n", ""), "lsblk": _lsblk_handler}
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)), \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays",
                                return_value=[{"name": "md0", "devices": [{"device": "/dev/sdc1"}]}]):
            result = disk_mutate.format_disk("/dev/sdc", "ext4", label="dane")

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
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)) as mock_run, \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate.os.path, "exists", side_effect=_partition_exists_stub):
            result = disk_mutate.format_disk("/dev/sdc", "ext4", label="dane")

        self.assertTrue(result["success"])
        self.assertEqual(result["partition"], "/dev/sdc1")

        # Regression check: wipefs must run against BOTH the whole disk
        # (clears its GPT/MBR header) AND the newly-created partition
        # (clears any old filesystem signature still sitting in that
        # data region) - see the docstring on the second wipefs call in
        # format_disk. A wipe that only touches the whole disk leaves
        # mkfs refusing to run on a partition that reused an old offset,
        # exactly the "appears to contain an existing filesystem" error
        # this was written to prevent.
        wipefs_targets = [c.args[0][-1] for c in mock_run.call_args_list if os.path.basename(c.args[0][0]) == "wipefs"]
        self.assertEqual(wipefs_targets, ["/dev/sdc", "/dev/sdc1"])

    def test_mkfs_receives_the_right_force_flag_per_filesystem(self):
        # wipefs alone proved insufficient in practice - a real report
        # showed mkfs.xfs still refusing over a leftover "partition
        # table" signature even after both wipefs passes ran clean.
        # Each mkfs tool's own force flag is the actually-robust
        # override, and each one spells it differently.
        cases = {"ext4": "-F", "btrfs": "-f", "xfs": "-f"}
        for fs, expected_flag in cases.items():
            responses = {
                "findmnt": (0, "/dev/sda2\n", ""),
                "lsblk": _lsblk_handler,
                "wipefs": (0, "", ""),
                "parted": (0, "", ""),
                f"mkfs.{fs}": (0, "", ""),
            }
            with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
                 mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)) as mock_run, \
                 mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
                 mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
                 mock.patch.object(disk_mutate.os.path, "exists", side_effect=_partition_exists_stub):
                result = disk_mutate.format_disk("/dev/sdc", fs, label="dane")

            self.assertTrue(result["success"], fs)
            mkfs_call = next(c for c in mock_run.call_args_list if os.path.basename(c.args[0][0]) == f"mkfs.{fs}")
            self.assertIn(expected_flag, mkfs_call.args[0], fs)

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
            result = disk_mutate.format_disk("/dev/sdc", "ext4", label="dane")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertNotIn("mkfs.ext4", [os.path.basename(c.args[0][0]) for c in mock_run.call_args_list])

    def test_wipefs_on_partition_failure_is_surfaced(self):
        # The second wipefs call (on the new partition) can fail too -
        # must be reported the same way as the first, not silently
        # ignored (which would let mkfs run into the same "existing
        # filesystem" refusal this whole second call exists to prevent).
        call_count = {"wipefs": 0}

        def wipefs_handler(args):
            call_count["wipefs"] += 1
            if call_count["wipefs"] == 1:
                return (0, "", "")  # whole-disk wipe succeeds
            return (1, "", "device or resource busy")  # partition wipe fails

        responses = {
            "findmnt": (0, "/dev/sda2\n", ""),
            "lsblk": _lsblk_handler,
            "wipefs": wipefs_handler,
            "parted": (0, "", ""),
        }
        with mock.patch.object(disk_mutate.system_tools, "find_binary", side_effect=_fake_find_binary), \
             mock.patch.object(disk_mutate.system_tools, "run", side_effect=_fake_run_factory(responses)) as mock_run, \
             mock.patch.object(disk_mutate.monitor, "list_disks", return_value=DISKS), \
             mock.patch.object(disk_mutate.monitor, "get_raid_arrays", return_value=[]), \
             mock.patch.object(disk_mutate.os.path, "exists", side_effect=_partition_exists_stub):
            result = disk_mutate.format_disk("/dev/sdc", "ext4", label="dane")

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
            result = disk_mutate.format_disk("/dev/sdc", "ext4", label="dane")

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
            result = disk_mutate.format_disk("/dev/sdc", "btrfs", label="dane")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.tool_missing")
        self.assertEqual(result["error_context"]["tool"], "mkfs.btrfs")


if __name__ == "__main__":
    unittest.main()
