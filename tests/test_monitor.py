"""
Tests for nas_monitor.monitor using canned smartctl/mdstat/mdadm output.
Run with: python3 -m pytest tests/ -v   (or plain unittest, see bottom)

These exercise the parsing logic without needing real disks/arrays or the
smartctl/mdadm binaries to be installed - that's what makes them runnable
in a plain sandbox and in CI.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "nas_monitor"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import monitor  # noqa: E402


SAMPLE_SMART_ATA_HEALTHY = {
    "smart_status": {"passed": True},
    "ata_smart_attributes": {
        "table": [
            {"id": 5, "raw": {"value": 0}},
            {"id": 194, "raw": {"value": 34}},
            {"id": 197, "raw": {"value": 0}},
            {"id": 198, "raw": {"value": 0}},
            {"id": 9, "raw": {"value": 12000}},
        ]
    },
}

SAMPLE_SMART_ATA_FAILING = {
    "smart_status": {"passed": True},
    "ata_smart_attributes": {
        "table": [
            {"id": 5, "raw": {"value": 12}},
            {"id": 194, "raw": {"value": 41}},
            {"id": 197, "raw": {"value": 3}},
            {"id": 198, "raw": {"value": 0}},
            {"id": 9, "raw": {"value": 30000}},
        ]
    },
}

SAMPLE_SMART_NVME = {
    "smart_status": {"passed": True},
    "nvme_smart_health_information_log": {
        "temperature": 38,
        "power_on_hours": 5000,
        "media_errors": 0,
        "percentage_used": 4,
        "critical_warning": 0,
        "unsafe_shutdowns": 2,
    },
}

# A real report from a USB-attached drive: attribute 194's raw value
# packed current/min/max temperature history into one 48-bit field
# (0x2100210021 = 33°C current, 33/33 min/max) instead of just the
# current reading - smartctl's own human-readable output shows this
# correctly as "33 (Min/Max 33/33)", but the JSON raw.value is the
# whole packed integer, 141736083489.
SAMPLE_SMART_ATA_PACKED_TEMPERATURE = {
    "smart_status": {"passed": True},
    "ata_smart_attributes": {
        "table": [
            {"id": 5, "raw": {"value": 0}},
            {"id": 194, "raw": {"value": 141736083489}},
            {"id": 197, "raw": {"value": 0}},
            {"id": 198, "raw": {"value": 0}},
            {"id": 9, "raw": {"value": 900}},
        ]
    },
}

MDSTAT_HEALTHY = """\
Personalities : [raid1]
md0 : active raid1 sdb1[1] sda1[0]
      976630464 blocks super 1.2 [2/2] [UU]
      bitmap: 0/8 pages [0KB], 65536KB chunk

unused devices: <none>
"""

MDSTAT_RESYNCING = """\
Personalities : [raid1]
md0 : active raid1 sdb1[1] sda1[0]
      976630464 blocks super 1.2 [2/2] [UU]
      [=====>...............]  resync = 27.3% (267184320/976630464) finish=120.4min speed=45000K/sec

unused devices: <none>
"""

MDADM_DETAIL_EXPORT = """\
MD_LEVEL=raid1
MD_DEVICES=2
MD_METADATA=1.2
MD_ARRAY_STATE=clean
MD_DEVICE_dev0_DEV=/dev/sda1
MD_DEVICE_dev0_ROLE=0
MD_DEVICE_dev1_DEV=/dev/sdb1
MD_DEVICE_dev1_ROLE=1
"""


class TestClassifyHealth(unittest.TestCase):
    def test_healthy_ata_disk(self):
        smart = {
            "available": True,
            "passed": True,
            "attributes": {
                "reallocated_sectors": 0,
                "pending_sectors": 0,
                "offline_uncorrectable": 0,
            },
        }
        self.assertEqual(monitor.classify_health(smart), "ok")

    def test_failed_smart_status(self):
        smart = {"available": True, "passed": False, "attributes": {}}
        self.assertEqual(monitor.classify_health(smart), "critical")

    def test_pending_sectors_is_critical(self):
        smart = {
            "available": True,
            "passed": True,
            "attributes": {"pending_sectors": 3},
        }
        self.assertEqual(monitor.classify_health(smart), "critical")

    def test_reallocated_sectors_is_warning(self):
        smart = {
            "available": True,
            "passed": True,
            "attributes": {"reallocated_sectors": 5, "pending_sectors": 0},
        }
        self.assertEqual(monitor.classify_health(smart), "warning")

    def test_unavailable_is_unknown(self):
        self.assertEqual(monitor.classify_health({"available": False}), "unknown")


class TestGetSmartHealth(unittest.TestCase):
    @mock.patch("nas_monitor.system_tools.shutil.which", return_value="/usr/sbin/smartctl")
    @mock.patch("nas_monitor.monitor._run")
    def test_parses_ata_disk(self, mock_run, mock_which):
        mock_run.return_value = (0, json.dumps(SAMPLE_SMART_ATA_HEALTHY), "")
        result = monitor.get_smart_health("/dev/sda")
        self.assertTrue(result["available"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["temperature_c"], 34)
        self.assertEqual(result["power_on_hours"], 12000)
        self.assertEqual(monitor.classify_health(result), "ok")

    @mock.patch("nas_monitor.system_tools.shutil.which", return_value="/usr/sbin/smartctl")
    @mock.patch("nas_monitor.monitor._run")
    def test_parses_failing_ata_disk(self, mock_run, mock_which):
        mock_run.return_value = (0, json.dumps(SAMPLE_SMART_ATA_FAILING), "")
        result = monitor.get_smart_health("/dev/sda")
        self.assertEqual(monitor.classify_health(result), "critical")  # pending sectors

    @mock.patch("nas_monitor.system_tools.shutil.which", return_value="/usr/sbin/smartctl")
    @mock.patch("nas_monitor.monitor._run")
    def test_parses_nvme_disk(self, mock_run, mock_which):
        mock_run.return_value = (0, json.dumps(SAMPLE_SMART_NVME), "")
        result = monitor.get_smart_health("/dev/nvme0n1")
        self.assertEqual(result["temperature_c"], 38)
        self.assertEqual(result["attributes"]["percentage_used"], 4)
        self.assertEqual(monitor.classify_health(result), "ok")

    @mock.patch("nas_monitor.system_tools.shutil.which", return_value="/usr/sbin/smartctl")
    @mock.patch("nas_monitor.monitor._run")
    def test_unpacks_multi_value_temperature_attribute(self, mock_run, mock_which):
        # Regression test for a real report: a USB drive's raw 194 value
        # (141736083489) was shown as-is, i.e. "141736083489°C" - only
        # the low byte (33) is the actual current temperature.
        mock_run.return_value = (0, json.dumps(SAMPLE_SMART_ATA_PACKED_TEMPERATURE), "")
        result = monitor.get_smart_health("/dev/sda")
        self.assertEqual(result["temperature_c"], 33)

    @mock.patch("nas_monitor.monitor._find_binary", return_value=None)
    def test_missing_smartctl_binary(self, mock_find_binary):
        result = monitor.get_smart_health("/dev/sda")
        self.assertFalse(result["available"])
        self.assertIn("not installed", result["error"])

    @mock.patch("nas_monitor.system_tools.shutil.which", return_value="/usr/sbin/smartctl")
    @mock.patch("nas_monitor.monitor._run")
    def test_garbage_output_does_not_crash(self, mock_run, mock_which):
        mock_run.return_value = (0, "not json at all", "")
        result = monitor.get_smart_health("/dev/sda")
        self.assertFalse(result["available"])
        self.assertIsNotNone(result["error"])


class TestMdstatParsing(unittest.TestCase):
    def _with_mdstat(self, content):
        tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".mdstat")
        tmp.write(content)
        tmp.close()
        return tmp.name

    def test_parses_healthy_array(self):
        path = self._with_mdstat(MDSTAT_HEALTHY)
        arrays = monitor._parse_mdstat(path)
        os.unlink(path)
        self.assertIn("md0", arrays)
        self.assertEqual(arrays["md0"]["level"], "raid1")
        self.assertTrue(arrays["md0"]["active"])
        self.assertIsNone(arrays["md0"]["progress_percent"])

    def test_parses_resyncing_array(self):
        path = self._with_mdstat(MDSTAT_RESYNCING)
        arrays = monitor._parse_mdstat(path)
        os.unlink(path)
        self.assertEqual(arrays["md0"]["progress_action"], "resync")
        self.assertAlmostEqual(arrays["md0"]["progress_percent"], 27.3)

    def test_no_mdstat_file(self):
        arrays = monitor._parse_mdstat("/nonexistent/path/mdstat")
        self.assertEqual(arrays, {})

    def test_inactive_array_with_no_level_token_does_not_swallow_a_device_as_the_level(self):
        # Real gap this fixes: "md0 : inactive sda1[0](S)" has no
        # level shown at all - too few members for mdadm to even pick
        # a personality. The old regex grabbed the first whitespace-
        # separated token unconditionally, so "sda1[0](S)" (a device)
        # got misread as the array's "level", and the device itself
        # silently vanished from members_raw.
        content = "Personalities : [raid1]\nmd0 : inactive sda1[0](S)\n      1953260544 blocks super 1.2\n\nunused devices: <none>\n"
        path = self._with_mdstat(content)
        arrays = monitor._parse_mdstat(path)
        os.unlink(path)
        self.assertEqual(arrays["md0"]["level"], "unknown")
        self.assertFalse(arrays["md0"]["active"])
        self.assertEqual(arrays["md0"]["members_raw"], "sda1[0](S)")

    def test_active_array_with_a_recognized_level_still_parses_normally(self):
        # Guards against the fix above being too aggressive - a normal,
        # healthy line must still correctly split level from members.
        path = self._with_mdstat(MDSTAT_HEALTHY)
        arrays = monitor._parse_mdstat(path)
        os.unlink(path)
        self.assertEqual(arrays["md0"]["level"], "raid1")
        self.assertEqual(arrays["md0"]["members_raw"], "sdb1[1] sda1[0]")


class TestParseMdstatMembers(unittest.TestCase):
    def test_parses_active_and_spare_and_faulty_members(self):
        members = monitor._parse_mdstat_members("sda1[0] sdb1[1] sdc1[2](S) sdd1[3](F)")
        self.assertEqual(members, [
            {"device": "/dev/sda1", "role": "0"},
            {"device": "/dev/sdb1", "role": "1"},
            {"device": "/dev/sdc1", "role": "spare"},
            {"device": "/dev/sdd1", "role": "faulty"},
        ])

    def test_empty_string_gives_no_members(self):
        self.assertEqual(monitor._parse_mdstat_members(""), [])

    def test_ignores_tokens_that_do_not_match_the_device_pattern(self):
        # e.g. a stray annotation that isn't a "name[N]" device token
        self.assertEqual(monitor._parse_mdstat_members("garbage"), [])


class TestGetRaidArrays(unittest.TestCase):
    @mock.patch("nas_monitor.system_tools.shutil.which", return_value="/sbin/mdadm")
    @mock.patch("nas_monitor.monitor._run")
    @mock.patch("nas_monitor.monitor._parse_mdstat")
    def test_healthy_array_end_to_end(self, mock_mdstat, mock_run, mock_which):
        mock_mdstat.return_value = {
            "md0": {
                "active": True,
                "level": "raid1",
                "members_raw": "sdb1[1] sda1[0]",
                "progress_percent": None,
                "progress_action": None,
            }
        }
        mock_run.return_value = (0, MDADM_DETAIL_EXPORT, "")
        arrays = monitor.get_raid_arrays()
        self.assertEqual(len(arrays), 1)
        self.assertEqual(arrays[0]["level"], "raid1")
        self.assertEqual(arrays[0]["array_state"], "clean")
        self.assertEqual(len(arrays[0]["devices"]), 2)
        self.assertEqual(arrays[0]["health"], "ok")
        self.assertEqual(arrays[0]["working_devices"], 2)
        self.assertEqual(arrays[0]["failed_devices"], 0)

    @mock.patch("nas_monitor.system_tools.shutil.which", return_value="/sbin/mdadm")
    @mock.patch("nas_monitor.monitor._run")
    @mock.patch("nas_monitor.monitor._parse_mdstat")
    def test_missing_member_flags_critical_even_when_state_string_says_clean(self, mock_mdstat, mock_run, mock_which):
        # Real report: an array with a disk physically disconnected (3
        # of 4 present) still showed as healthy. A single substring
        # check against MD_ARRAY_STATE ("degraded" in state) isn't
        # reliable on its own - this fixture deliberately keeps
        # MD_ARRAY_STATE at "clean" (as some mdadm versions/situations
        # report it) while only listing 3 of the 4 expected devices, to
        # prove the independent device-count comparison catches this
        # regardless of what the state string itself claims.
        export = (
            "MD_LEVEL=raid5\n"
            "MD_DEVICES=4\n"
            "MD_METADATA=1.2\n"
            "MD_ARRAY_STATE=clean\n"
            "MD_DEVICE_dev0_DEV=/dev/sda1\n"
            "MD_DEVICE_dev0_ROLE=0\n"
            "MD_DEVICE_dev1_DEV=/dev/sdb1\n"
            "MD_DEVICE_dev1_ROLE=1\n"
            "MD_DEVICE_dev2_DEV=/dev/sdc1\n"
            "MD_DEVICE_dev2_ROLE=2\n"
        )
        mock_mdstat.return_value = {
            "md0": {"active": True, "level": "raid5", "members_raw": "", "progress_percent": None, "progress_action": None}
        }
        mock_run.return_value = (0, export, "")
        arrays = monitor.get_raid_arrays()
        self.assertEqual(arrays[0]["working_devices"], 3)
        self.assertEqual(arrays[0]["expected_devices"], 4)
        # His explicit correction: degraded-but-STILL-ACTIVE (mdstat
        # itself reports it active - one missing disk out of a RAID5 is
        # still fully functional, just without redundancy) is a
        # warning, not the same red as something actually broken.
        self.assertEqual(arrays[0]["health"], "warning")

    @mock.patch("nas_monitor.system_tools.shutil.which", return_value="/sbin/mdadm")
    @mock.patch("nas_monitor.monitor._run")
    @mock.patch("nas_monitor.monitor._parse_mdstat")
    def test_inactive_array_is_critical_not_just_warning(self, mock_mdstat, mock_run, mock_which):
        # The genuinely-broken case: mdstat itself reports the array as
        # inactive (too many members missing to function at all, not
        # just running without full redundancy) - this is the one that
        # should still get the red/critical tier.
        export = (
            "MD_LEVEL=raid5\n"
            "MD_DEVICES=4\n"
            "MD_METADATA=1.2\n"
            "MD_ARRAY_STATE=inactive\n"
            "MD_DEVICE_dev0_DEV=/dev/sda1\n"
            "MD_DEVICE_dev0_ROLE=0\n"
        )
        mock_mdstat.return_value = {
            "md0": {"active": False, "level": "raid5", "members_raw": "", "progress_percent": None, "progress_action": None}
        }
        mock_run.return_value = (0, export, "")
        arrays = monitor.get_raid_arrays()
        self.assertEqual(arrays[0]["health"], "critical")

    @mock.patch("nas_monitor.system_tools.shutil.which", return_value="/sbin/mdadm")
    @mock.patch("nas_monitor.monitor._run")
    @mock.patch("nas_monitor.monitor._parse_mdstat")
    def test_faulty_spare_role_does_not_count_as_working(self, mock_mdstat, mock_run, mock_which):
        # A device slot can be PRESENT (has a device path) but not
        # actually contributing - mdadm marks it with a non-numeric
        # role ("spare"/"faulty spare") rather than a plain RAID slot
        # number once it's failed out of the array.
        export = (
            "MD_LEVEL=raid1\n"
            "MD_DEVICES=2\n"
            "MD_METADATA=1.2\n"
            "MD_ARRAY_STATE=clean\n"
            "MD_DEVICE_dev0_DEV=/dev/sda1\n"
            "MD_DEVICE_dev0_ROLE=0\n"
            "MD_DEVICE_dev1_DEV=/dev/sdb1\n"
            "MD_DEVICE_dev1_ROLE=faulty spare\n"
        )
        mock_mdstat.return_value = {
            "md0": {"active": True, "level": "raid1", "members_raw": "", "progress_percent": None, "progress_action": None}
        }
        mock_run.return_value = (0, export, "")
        arrays = monitor.get_raid_arrays()
        self.assertEqual(arrays[0]["working_devices"], 1)
        self.assertEqual(arrays[0]["failed_devices"], 1)
        # active RAID1 with one working mirror - degraded, still
        # serving data, so warning rather than critical (see the
        # inactive-array test below for the genuinely broken case).
        self.assertEqual(arrays[0]["health"], "warning")

    @mock.patch("nas_monitor.monitor._find_binary", return_value=None)
    @mock.patch("nas_monitor.monitor._parse_mdstat")
    def test_missing_mdadm_binary_still_lists_array(self, mock_mdstat, mock_find_binary):
        mock_mdstat.return_value = {
            "md0": {
                "active": True,
                "level": "raid1",
                "members_raw": "",
                "progress_percent": None,
                "progress_action": None,
            }
        }
        arrays = monitor.get_raid_arrays()
        self.assertEqual(len(arrays), 1)
        self.assertEqual(arrays[0]["error"], "mdadm not installed")

    @mock.patch("nas_monitor.monitor._find_binary", return_value=None)
    @mock.patch("nas_monitor.monitor._parse_mdstat")
    def test_missing_mdadm_still_reports_members_from_mdstat_itself(self, mock_mdstat, mock_find_binary):
        # Real gap: without mdadm, "which disks does this contain" used
        # to come back completely empty even though /proc/mdstat's own
        # raw member string already has that answer - falls back to
        # parsing it directly instead of just giving up.
        mock_mdstat.return_value = {
            "md0": {
                "active": True,
                "level": "raid5",
                "members_raw": "sdb1[1] sdc1[2] sdd1[3](F)",
                "progress_percent": None,
                "progress_action": None,
            }
        }
        arrays = monitor.get_raid_arrays()
        self.assertEqual(
            arrays[0]["devices"],
            [
                {"device": "/dev/sdb1", "role": "1"},
                {"device": "/dev/sdc1", "role": "2"},
                {"device": "/dev/sdd1", "role": "faulty"},
            ],
        )
        self.assertEqual(arrays[0]["working_devices"], 2)
        self.assertEqual(arrays[0]["failed_devices"], 1)

    @mock.patch("nas_monitor.monitor._parse_mdstat", return_value={})
    def test_no_arrays_present(self, mock_mdstat):
        self.assertEqual(monitor.get_raid_arrays(), [])


class TestFindBinary(unittest.TestCase):
    @mock.patch("nas_monitor.system_tools.shutil.which", return_value="/usr/bin/lsblk")
    def test_uses_which_when_path_is_sane(self, mock_which):
        self.assertEqual(monitor._find_binary("lsblk"), "/usr/bin/lsblk")

    def test_falls_back_when_path_is_restricted(self):
        # Simulates the actual bug: systemd's PATH= only contains the venv
        # dir, so shutil.which() (which only searches PATH) finds nothing -
        # but the binary is still on disk at its usual location.
        with mock.patch("nas_monitor.system_tools.shutil.which", return_value=None), \
             mock.patch("nas_monitor.system_tools.os.path.isfile") as mock_isfile, \
             mock.patch("nas_monitor.system_tools.os.access", return_value=True):
            mock_isfile.side_effect = lambda p: p == "/usr/sbin/smartctl"
            self.assertEqual(monitor._find_binary("smartctl"), "/usr/sbin/smartctl")

    def test_returns_none_when_truly_not_installed(self):
        with mock.patch("nas_monitor.system_tools.shutil.which", return_value=None), \
             mock.patch("nas_monitor.system_tools.os.path.isfile", return_value=False):
            self.assertIsNone(monitor._find_binary("nope-does-not-exist"))


class TestHumanSizeIec(unittest.TestCase):
    def test_uses_iec_symbols_not_si(self):
        # 1 GiB = 1024**3 bytes exactly - this must render as "1.0 GiB",
        # never "1.0 GB" (which would imply decimal/1000-based math that
        # was never actually happening here)
        self.assertEqual(monitor._human_size(1024**3), "1.0 GiB")

    def test_bytes_have_no_decimal(self):
        self.assertEqual(monitor._human_size(500), "500 B")

    def test_scales_through_all_units(self):
        self.assertEqual(monitor._human_size(1024), "1.0 KiB")
        self.assertEqual(monitor._human_size(1024**2), "1.0 MiB")
        self.assertEqual(monitor._human_size(1024**4), "1.0 TiB")
        self.assertEqual(monitor._human_size(1024**5), "1.0 PiB")

    def test_very_large_value_still_shows_pib_not_a_new_unit(self):
        self.assertEqual(monitor._human_size(1024**5 * 3), "3.0 PiB")


class TestGetFilesystemUsage(unittest.TestCase):
    @mock.patch("nas_monitor.monitor._find_binary", return_value="/usr/bin/lsblk")
    @mock.patch("nas_monitor.monitor._run")
    def test_single_filesystem_directly_on_the_device(self, mock_run, mock_find):
        sample = json.dumps(
            {
                "blockdevices": [
                    {"name": "sda", "mountpoint": "/srv", "fstype": "ext4", "fssize": 4000000000000, "fsavail": 1000000000000, "fsused": 3000000000000}
                ]
            }
        )
        mock_run.return_value = (0, sample, "")
        result = monitor.get_filesystem_usage("/dev/sda")
        self.assertTrue(result["mounted"])
        self.assertEqual(result["mountpoints"], ["/srv"])
        self.assertEqual(result["total_bytes"], 4000000000000)
        self.assertEqual(result["used_bytes"], 3000000000000)
        self.assertEqual(result["available_bytes"], 1000000000000)

    @mock.patch("nas_monitor.monitor._find_binary", return_value="/usr/bin/lsblk")
    @mock.patch("nas_monitor.monitor._run")
    def test_real_world_layout_sums_data_partitions_excludes_efi_and_swap(self, mock_run, mock_find):
        # The exact layout that was reported broken: a desktop-style disk
        # with EFI + btrfs root ("System") + swap + a separate ext4 data
        # partition. The old "just take the first mounted one" logic
        # landed on the ~1 GiB EFI partition and showed "9 MiB of 974 MiB"
        # instead of anything resembling the real ~490 GB of actual data.
        gib = 1024**3
        sample = json.dumps(
            {
                "blockdevices": [
                    {
                        "name": "nvme0n1",
                        "mountpoint": None,
                        "fstype": None,
                        "fssize": None,
                        "fsavail": None,
                        "fsused": None,
                        "children": [
                            {"name": "nvme0n1p1", "mountpoint": "/boot/efi", "fstype": "vfat", "fssize": int(0.951 * gib), "fsavail": int(0.94 * gib), "fsused": int(0.009 * gib)},
                            {"name": "nvme0n1p2", "mountpoint": "/", "fstype": "btrfs", "fssize": 152 * gib, "fsavail": 64 * gib, "fsused": 88 * gib},
                            {"name": "nvme0n1p3", "mountpoint": "/srv", "fstype": "ext4", "fssize": 339 * gib, "fsavail": 200 * gib, "fsused": 139 * gib},
                            {"name": "nvme0n1p4", "mountpoint": "[SWAP]", "fstype": "swap", "fssize": None, "fsavail": None, "fsused": None},
                        ],
                    }
                ]
            }
        )
        mock_run.return_value = (0, sample, "")
        result = monitor.get_filesystem_usage("/dev/nvme0n1")
        self.assertTrue(result["mounted"])
        self.assertNotIn("/boot/efi", result["mountpoints"])
        self.assertNotIn("[SWAP]", result["mountpoints"])
        self.assertEqual(set(result["mountpoints"]), {"/", "/srv"})
        self.assertEqual(result["total_bytes"], 152 * gib + 339 * gib)
        self.assertEqual(result["used_bytes"], 88 * gib + 139 * gib)
        self.assertEqual(result["available_bytes"], 64 * gib + 200 * gib)

    @mock.patch("nas_monitor.monitor._find_binary", return_value="/usr/bin/lsblk")
    @mock.patch("nas_monitor.monitor._run")
    def test_efi_only_disk_reports_not_mounted(self, mock_run, mock_find):
        # A disk with nothing BUT an EFI partition mounted has no real
        # data filesystem at all - correctly no bar, not a bar showing
        # the boot partition.
        sample = json.dumps(
            {"blockdevices": [{"name": "sda1", "mountpoint": "/boot/efi", "fstype": "vfat", "fssize": 1000000000, "fsavail": 900000000, "fsused": 100000000}]}
        )
        mock_run.return_value = (0, sample, "")
        result = monitor.get_filesystem_usage("/dev/sda")
        self.assertFalse(result["mounted"])

    @mock.patch("nas_monitor.monitor._find_binary", return_value="/usr/bin/lsblk")
    @mock.patch("nas_monitor.monitor._run")
    def test_filesystem_on_a_partition_underneath(self, mock_run, mock_find):
        sample = json.dumps(
            {
                "blockdevices": [
                    {
                        "name": "sda",
                        "mountpoint": None,
                        "fstype": None,
                        "fssize": None,
                        "fsavail": None,
                        "fsused": None,
                        "children": [
                            {"name": "sda1", "mountpoint": "/srv", "fstype": "ext4", "fssize": 2000000000000, "fsavail": 500000000000, "fsused": 1500000000000}
                        ],
                    }
                ]
            }
        )
        mock_run.return_value = (0, sample, "")
        result = monitor.get_filesystem_usage("/dev/sda")
        self.assertTrue(result["mounted"])
        self.assertEqual(result["mountpoints"], ["/srv"])
        self.assertEqual(result["total_bytes"], 2000000000000)

    @mock.patch("nas_monitor.monitor._find_binary", return_value="/usr/bin/lsblk")
    @mock.patch("nas_monitor.monitor._run")
    def test_nothing_mounted(self, mock_run, mock_find):
        sample = json.dumps({"blockdevices": [{"name": "sdb", "mountpoint": None, "fstype": None, "fssize": None, "fsavail": None, "fsused": None}]})
        mock_run.return_value = (0, sample, "")
        result = monitor.get_filesystem_usage("/dev/sdb")
        self.assertFalse(result["mounted"])
        self.assertIsNone(result["total_bytes"])

    @mock.patch("nas_monitor.monitor._find_binary", return_value=None)
    def test_missing_lsblk_reports_not_mounted_not_raises(self, mock_find):
        result = monitor.get_filesystem_usage("/dev/sda")
        self.assertFalse(result["mounted"])

    @mock.patch("nas_monitor.monitor._find_binary", return_value="/usr/bin/lsblk")
    @mock.patch("nas_monitor.monitor._run", return_value=(1, "", "device not found"))
    def test_command_failure_reports_not_mounted_not_raises(self, mock_run, mock_find):
        result = monitor.get_filesystem_usage("/dev/ghost")
        self.assertFalse(result["mounted"])

    @mock.patch("nas_monitor.monitor._find_binary", return_value="/usr/bin/lsblk")
    @mock.patch("nas_monitor.monitor._run", return_value=(0, "not json", ""))
    def test_garbage_output_reports_not_mounted_not_raises(self, mock_run, mock_find):
        result = monitor.get_filesystem_usage("/dev/sda")
        self.assertFalse(result["mounted"])


class TestGetFullStatusVisibility(unittest.TestCase):
    """Raw/unformatted disks (nothing mounted, not part of a RAID array)
    should never appear in get_full_status()'s "disks" list - they show
    in disk_mutate.list_raw_disks() / the raw-disks table instead. A
    disk with a real mounted filesystem, or one that's a RAID member,
    still shows here as before."""

    def _disks(self, name):
        return [{"name": name, "path": f"/dev/{name}"}]

    @mock.patch("nas_monitor.monitor.get_smart_health", return_value={"available": False})
    @mock.patch("nas_monitor.monitor.classify_health", return_value="unknown")
    @mock.patch("nas_monitor.monitor.get_raid_arrays", return_value=[])
    def test_unmounted_non_raid_disk_is_excluded(self, mock_raid, mock_health, mock_smart):
        with mock.patch("nas_monitor.monitor.list_disks", return_value=self._disks("sdz")), \
             mock.patch("nas_monitor.monitor.get_filesystem_usage", return_value={"mounted": False}):
            status = monitor.get_full_status()
        self.assertEqual(status["disks"], [])

    @mock.patch("nas_monitor.monitor.get_smart_health", return_value={"available": False})
    @mock.patch("nas_monitor.monitor.classify_health", return_value="unknown")
    @mock.patch("nas_monitor.monitor.get_raid_arrays", return_value=[])
    def test_mounted_disk_is_included(self, mock_raid, mock_health, mock_smart):
        with mock.patch("nas_monitor.monitor.list_disks", return_value=self._disks("sda")), \
             mock.patch("nas_monitor.monitor.get_filesystem_usage", return_value={"mounted": True}):
            status = monitor.get_full_status()
        self.assertEqual([d["name"] for d in status["disks"]], ["sda"])

    @mock.patch("nas_monitor.monitor.get_smart_health", return_value={"available": False})
    @mock.patch("nas_monitor.monitor.classify_health", return_value="unknown")
    def test_raid_member_is_excluded_even_when_mounted(self, mock_health, mock_smart):
        # His explicit ask: the array's own card already represents
        # this storage - showing each member disk again too, all with
        # identical-looking usage numbers, is confusing rather than
        # informative. This used to be backwards (the "still_included"
        # name this test had before described the very bug being
        # fixed) - a raid member used to bypass the "must be mounted"
        # check and stay visible unconditionally; now it's excluded
        # unconditionally instead, mounted or not.
        raid = [{"path": "/dev/md0", "devices": [{"device": "/dev/sdb1"}]}]
        with mock.patch("nas_monitor.monitor.list_disks", return_value=self._disks("sdb")), \
             mock.patch("nas_monitor.monitor.get_filesystem_usage", return_value={"mounted": True, "mountpoints": ["/srv/dane-raid"]}), \
             mock.patch("nas_monitor.monitor.get_raid_arrays", return_value=raid):
            status = monitor.get_full_status()
        self.assertEqual(status["disks"], [])

    @mock.patch("nas_monitor.monitor.classify_health", return_value="unknown")
    @mock.patch("nas_monitor.monitor.get_raid_arrays", return_value=[])
    def test_one_disk_erroring_does_not_hide_the_others(self, mock_raid, mock_health):
        # Regression test: before this, an exception raised while
        # processing any single disk (lsblk/smartctl choking on one in
        # a weird transitional state) propagated out of
        # get_full_status() entirely - /api/status came back as a 500,
        # and the frontend showed a completely empty Disks & Arrays tab
        # for *every* disk and RAID array, not just the one that was
        # actually having a problem.
        disks = [{"name": "sda", "path": "/dev/sda"}, {"name": "sdb", "path": "/dev/sdb"}]

        def usage_side_effect(path):
            if path == "/dev/sda":
                raise RuntimeError("lsblk exploded")
            return {"mounted": True, "mountpoints": ["/mnt/dane"], "total_bytes": 0, "used_bytes": 0, "available_bytes": 0}

        with mock.patch("nas_monitor.monitor.list_disks", return_value=disks), \
             mock.patch("nas_monitor.monitor.get_filesystem_usage", side_effect=usage_side_effect), \
             mock.patch("nas_monitor.monitor.get_smart_health", return_value={"available": False}):
            status = monitor.get_full_status()

        names = {d["name"] for d in status["disks"]}
        # both still present - the broken one included with an error
        # flagged on it instead of vanishing, the healthy one unaffected
        self.assertEqual(names, {"sda", "sdb"})
        broken = next(d for d in status["disks"] if d["name"] == "sda")
        self.assertEqual(broken["health"], "unknown")
        self.assertIn("lsblk exploded", broken["smart"]["error"])

    @mock.patch("nas_monitor.monitor.get_smart_health", return_value={"available": False})
    @mock.patch("nas_monitor.monitor.classify_health", return_value="unknown")
    @mock.patch("nas_monitor.monitor.get_raid_arrays", return_value=[])
    @mock.patch("nas_monitor.monitor._boot_disk_name", return_value="sda")
    def test_flags_is_boot_disk_correctly(self, mock_boot, mock_raid, mock_health, mock_smart):
        disks = [{"name": "sda", "path": "/dev/sda"}, {"name": "sdb", "path": "/dev/sdb"}]
        with mock.patch("nas_monitor.monitor.list_disks", return_value=disks), \
             mock.patch("nas_monitor.monitor.get_filesystem_usage", return_value={"mounted": True}):
            status = monitor.get_full_status()

        by_name = {d["name"]: d for d in status["disks"]}
        self.assertTrue(by_name["sda"]["is_boot_disk"])
        self.assertFalse(by_name["sdb"]["is_boot_disk"])


if __name__ == "__main__":
    unittest.main()
