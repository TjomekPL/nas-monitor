from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import disk_labels  # noqa: E402


class DiskLabelsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.patcher = mock.patch("nas_monitor.state_store.STATE_DIR", self.tmpdir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestGetLabel(DiskLabelsTestCase):
    def test_empty_when_nothing_saved(self):
        self.assertEqual(disk_labels.get_label("G0Z056222"), "")

    def test_returns_saved_label(self):
        disk_labels.set_label("G0Z056222", "dane")
        self.assertEqual(disk_labels.get_label("G0Z056222"), "dane")

    def test_empty_for_empty_serial(self):
        self.assertEqual(disk_labels.get_label(""), "")


class TestSetLabel(DiskLabelsTestCase):
    def test_overwrites_previous_label_for_same_serial(self):
        disk_labels.set_label("G0Z056222", "stara-nazwa")
        disk_labels.set_label("G0Z056222", "nowa-nazwa")
        self.assertEqual(disk_labels.get_label("G0Z056222"), "nowa-nazwa")

    def test_empty_label_removes_it(self):
        disk_labels.set_label("G0Z056222", "dane")
        disk_labels.set_label("G0Z056222", "")
        self.assertEqual(disk_labels.get_label("G0Z056222"), "")

    def test_labels_are_independent_per_serial(self):
        disk_labels.set_label("SERIAL-A", "dysk-a")
        disk_labels.set_label("SERIAL-B", "dysk-b")
        self.assertEqual(disk_labels.get_label("SERIAL-A"), "dysk-a")
        self.assertEqual(disk_labels.get_label("SERIAL-B"), "dysk-b")

    def test_truncates_overly_long_labels(self):
        long_label = "x" * 200
        disk_labels.set_label("G0Z056222", long_label)
        self.assertEqual(len(disk_labels.get_label("G0Z056222")), disk_labels.MAX_LABEL_LENGTH)

    def test_no_op_for_empty_serial(self):
        result = disk_labels.set_label("", "dane")
        self.assertTrue(result["success"])
        self.assertEqual(disk_labels.get_all_labels(), {})

    def test_returns_success_result(self):
        result = disk_labels.set_label("G0Z056222", "dane")
        self.assertTrue(result["success"])


class TestGetAllLabels(DiskLabelsTestCase):
    def test_empty_dict_when_nothing_saved(self):
        self.assertEqual(disk_labels.get_all_labels(), {})

    def test_returns_everything_saved(self):
        disk_labels.set_label("SERIAL-A", "dysk-a")
        disk_labels.set_label("SERIAL-B", "dysk-b")
        self.assertEqual(disk_labels.get_all_labels(), {"SERIAL-A": "dysk-a", "SERIAL-B": "dysk-b"})


if __name__ == "__main__":
    unittest.main()
