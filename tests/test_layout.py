from __future__ import annotations

import os
import sys
import tempfile
import shutil
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import layout  # noqa: E402


class LayoutTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.patcher = mock.patch("nas_monitor.state_store.STATE_DIR", self.tmpdir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestGetOrder(LayoutTestCase):
    def test_empty_when_nothing_saved(self):
        self.assertEqual(layout.get_order("disks"), [])

    def test_returns_saved_order(self):
        layout.set_order("disks", ["nvme0n1", "sdb", "sdc"])
        self.assertEqual(layout.get_order("disks"), ["nvme0n1", "sdb", "sdc"])

    def test_sections_are_independent(self):
        layout.set_order("disks", ["sda", "sdb"])
        self.assertEqual(layout.get_order("network"), [])
        layout.set_order("network", ["eth0", "wlan0"])
        self.assertEqual(layout.get_order("disks"), ["sda", "sdb"])
        self.assertEqual(layout.get_order("network"), ["eth0", "wlan0"])


class TestSetOrder(LayoutTestCase):
    def test_overwrites_previous_order_for_same_section(self):
        layout.set_order("disks", ["sda", "sdb"])
        layout.set_order("disks", ["sdb", "sda"])
        self.assertEqual(layout.get_order("disks"), ["sdb", "sda"])

    def test_coerces_items_to_strings(self):
        layout.set_order("disks", [1, 2, 3])
        self.assertEqual(layout.get_order("disks"), ["1", "2", "3"])

    def test_returns_success_result(self):
        result = layout.set_order("disks", ["sda"])
        self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
