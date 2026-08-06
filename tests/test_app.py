from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import app as app_module  # noqa: E402


class TestSharesBlockingUnmount(unittest.TestCase):
    def test_no_shares_at_all(self):
        with mock.patch.object(app_module.smb_shares, "list_shares", return_value={"available": True, "shares": []}):
            self.assertEqual(app_module._shares_blocking_unmount("/mnt/dane"), [])

    def test_share_exactly_at_the_mount_point(self):
        shares = {"available": True, "shares": [{"name": "cokolwiek", "path": "/mnt/dane"}]}
        with mock.patch.object(app_module.smb_shares, "list_shares", return_value=shares):
            self.assertEqual(app_module._shares_blocking_unmount("/mnt/dane"), ["cokolwiek"])

    def test_share_nested_under_the_mount_point(self):
        shares = {"available": True, "shares": [{"name": "test-2", "path": "/mnt/Test/test-2"}]}
        with mock.patch.object(app_module.smb_shares, "list_shares", return_value=shares):
            self.assertEqual(app_module._shares_blocking_unmount("/mnt/Test"), ["test-2"])

    def test_unrelated_share_is_not_blocking(self):
        shares = {"available": True, "shares": [{"name": "inny", "path": "/srv/inny"}]}
        with mock.patch.object(app_module.smb_shares, "list_shares", return_value=shares):
            self.assertEqual(app_module._shares_blocking_unmount("/mnt/dane"), [])

    def test_does_not_false_positive_on_a_similarly_prefixed_sibling_path(self):
        # /mnt/dane2 must not be treated as "under /mnt/dane" just
        # because the string starts the same way
        shares = {"available": True, "shares": [{"name": "sibling", "path": "/mnt/dane2/x"}]}
        with mock.patch.object(app_module.smb_shares, "list_shares", return_value=shares):
            self.assertEqual(app_module._shares_blocking_unmount("/mnt/dane"), [])

    def test_multiple_blocking_shares_all_listed(self):
        shares = {"available": True, "shares": [
            {"name": "test-1", "path": "/mnt/Test/test-1"},
            {"name": "test-2", "path": "/mnt/Test/test-2"},
            {"name": "inny", "path": "/srv/inny"},
        ]}
        with mock.patch.object(app_module.smb_shares, "list_shares", return_value=shares):
            self.assertEqual(app_module._shares_blocking_unmount("/mnt/Test"), ["test-1", "test-2"])


if __name__ == "__main__":
    unittest.main()
