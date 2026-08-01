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
    @mock.patch("nas_monitor.monitor.shutil.which", return_value="/usr/sbin/smartctl")
    @mock.patch("nas_monitor.monitor._run")
    def test_parses_ata_disk(self, mock_run, mock_which):
        mock_run.return_value = (0, json.dumps(SAMPLE_SMART_ATA_HEALTHY), "")
        result = monitor.get_smart_health("/dev/sda")
        self.assertTrue(result["available"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["temperature_c"], 34)
        self.assertEqual(result["power_on_hours"], 12000)
        self.assertEqual(monitor.classify_health(result), "ok")

    @mock.patch("nas_monitor.monitor.shutil.which", return_value="/usr/sbin/smartctl")
    @mock.patch("nas_monitor.monitor._run")
    def test_parses_failing_ata_disk(self, mock_run, mock_which):
        mock_run.return_value = (0, json.dumps(SAMPLE_SMART_ATA_FAILING), "")
        result = monitor.get_smart_health("/dev/sda")
        self.assertEqual(monitor.classify_health(result), "critical")  # pending sectors

    @mock.patch("nas_monitor.monitor.shutil.which", return_value="/usr/sbin/smartctl")
    @mock.patch("nas_monitor.monitor._run")
    def test_parses_nvme_disk(self, mock_run, mock_which):
        mock_run.return_value = (0, json.dumps(SAMPLE_SMART_NVME), "")
        result = monitor.get_smart_health("/dev/nvme0n1")
        self.assertEqual(result["temperature_c"], 38)
        self.assertEqual(result["attributes"]["percentage_used"], 4)
        self.assertEqual(monitor.classify_health(result), "ok")

    @mock.patch("nas_monitor.monitor._find_binary", return_value=None)
    def test_missing_smartctl_binary(self, mock_find_binary):
        result = monitor.get_smart_health("/dev/sda")
        self.assertFalse(result["available"])
        self.assertIn("not installed", result["error"])

    @mock.patch("nas_monitor.monitor.shutil.which", return_value="/usr/sbin/smartctl")
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


class TestGetRaidArrays(unittest.TestCase):
    @mock.patch("nas_monitor.monitor.shutil.which", return_value="/sbin/mdadm")
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

    @mock.patch("nas_monitor.monitor._parse_mdstat", return_value={})
    def test_no_arrays_present(self, mock_mdstat):
        self.assertEqual(monitor.get_raid_arrays(), [])


if __name__ == "__main__":
    unittest.main()
