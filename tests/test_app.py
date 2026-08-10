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


class TestApiDiskUnmount(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def test_refuses_without_confirmation_when_shares_are_in_the_way(self):
        disks = [{"name": "sdb", "mount_point": "/srv/Test"}]
        shares = {"available": True, "shares": [{"name": "test-2", "path": "/srv/Test/test-2"}]}
        with mock.patch.object(app_module.disk_mutate, "list_manageable_disks", return_value=disks), \
             mock.patch.object(app_module.smb_shares, "list_shares", return_value=shares), \
             mock.patch.object(app_module.smb_shares, "delete_share") as mock_delete, \
             mock.patch.object(app_module.disk_mutate, "unmount_disk") as mock_unmount:
            res = self.client.post("/api/disks/sdb/unmount", json={})

        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertEqual(data["error_code"], "disks.unmount_blocked_by_shares")
        self.assertEqual(data["error_context"]["shares"], "test-2")
        mock_delete.assert_not_called()
        mock_unmount.assert_not_called()

    def test_deletes_blocking_shares_then_unmounts_when_confirmed(self):
        # His explicit expectation: confirming should DELETE the
        # dependent share(s) (files preserved, same as any normal share
        # delete) and THEN unmount - not just fail a second time.
        disks = [{"name": "sdb", "mount_point": "/srv/Test"}]
        shares = {"available": True, "shares": [{"name": "test-2", "path": "/srv/Test/test-2"}]}
        with mock.patch.object(app_module.disk_mutate, "list_manageable_disks", return_value=disks), \
             mock.patch.object(app_module.smb_shares, "list_shares", return_value=shares), \
             mock.patch.object(app_module.smb_shares, "delete_share", return_value={"success": True}) as mock_delete, \
             mock.patch.object(app_module.disk_mutate, "unmount_disk", return_value={"success": True}) as mock_unmount:
            res = self.client.post("/api/disks/sdb/unmount", json={"delete_blocking_shares": True})

        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["deleted_shares"], ["test-2"])
        mock_delete.assert_called_once_with("test-2", delete_files=False)
        mock_unmount.assert_called_once()

    def test_unmounts_normally_when_no_shares_are_in_the_way(self):
        disks = [{"name": "sdb", "mount_point": "/srv/dane"}]
        shares = {"available": True, "shares": []}
        with mock.patch.object(app_module.disk_mutate, "list_manageable_disks", return_value=disks), \
             mock.patch.object(app_module.smb_shares, "list_shares", return_value=shares), \
             mock.patch.object(app_module.smb_shares, "delete_share") as mock_delete, \
             mock.patch.object(app_module.disk_mutate, "unmount_disk", return_value={"success": True}):
            res = self.client.post("/api/disks/sdb/unmount", json={})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["deleted_shares"], [])
        mock_delete.assert_not_called()


class TestApiRaidDelete(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def test_cleans_up_a_blocking_share_even_when_the_array_is_already_unmounted(self):
        # Real report: a share survived indefinitely across repeated
        # delete-and-recreate cycles, still browsable afterward - the
        # cascade used to only look for blocking shares when the array
        # was mounted AT THE MOMENT of deletion. Unmount-then-delete (a
        # completely normal sequence) skipped share cleanup entirely,
        # even though the share still pointed at this array's
        # deterministic mount path and a later array reusing the same
        # name would mount right back on top of it.
        arrays = [{"name": "md0", "mount_point": None, "mounted": False}]
        shares = {"available": True, "shares": [{"name": "test", "path": "/srv/md0/test"}]}
        with mock.patch.object(app_module.disk_mutate, "list_manageable_raid_arrays", return_value=arrays), \
             mock.patch.object(app_module.smb_shares, "list_shares", return_value=shares), \
             mock.patch.object(app_module.smb_shares, "delete_share", return_value={"success": True}) as mock_delete, \
             mock.patch.object(app_module.disk_mutate, "unmount_disk") as mock_unmount, \
             mock.patch.object(app_module.raid_mutate, "delete_raid_array", return_value={"success": True, "members": []}):
            res = self.client.post("/api/raid/md0/delete", json={"delete_blocking_shares": True})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["deleted_shares"], ["test"])
        mock_delete.assert_called_once_with("test", delete_files=False)
        mock_unmount.assert_not_called()  # already unmounted - nothing to unmount

    def test_refuses_without_confirmation_when_a_blocking_share_exists_even_if_unmounted(self):
        arrays = [{"name": "md0", "mount_point": None, "mounted": False}]
        shares = {"available": True, "shares": [{"name": "test", "path": "/srv/md0/test"}]}
        with mock.patch.object(app_module.disk_mutate, "list_manageable_raid_arrays", return_value=arrays), \
             mock.patch.object(app_module.smb_shares, "list_shares", return_value=shares), \
             mock.patch.object(app_module.smb_shares, "delete_share") as mock_delete, \
             mock.patch.object(app_module.raid_mutate, "delete_raid_array") as mock_delete_array:
            res = self.client.post("/api/raid/md0/delete", json={})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "disks.unmount_blocked_by_shares")
        mock_delete.assert_not_called()
        mock_delete_array.assert_not_called()

    def test_delete_success_no_shares_still_unmounts_a_mounted_array(self):
        arrays = [{"name": "md0", "mount_point": "/srv/md0", "mounted": True}]
        with mock.patch.object(app_module.disk_mutate, "list_manageable_raid_arrays", return_value=arrays), \
             mock.patch.object(app_module.smb_shares, "list_shares", return_value={"available": True, "shares": []}), \
             mock.patch.object(app_module.disk_mutate, "unmount_disk", return_value={"success": True}) as mock_unmount, \
             mock.patch.object(app_module.raid_mutate, "delete_raid_array", return_value={"success": True, "members": ["/dev/sdb"]}):
            res = self.client.post("/api/raid/md0/delete", json={})
        self.assertEqual(res.status_code, 200)
        mock_unmount.assert_called_once_with("/dev/md0")


class TestApiRaidRoutes(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def test_create_success(self):
        with mock.patch.object(app_module.raid_mutate, "create_raid_array",
                                return_value={"success": True, "name": "md0", "path": "/dev/md0"}) as mock_create:
            res = self.client.post("/api/raid/create", json={"devices": ["/dev/sdb", "/dev/sdc"], "level": "1"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"success": True, "name": "md0", "path": "/dev/md0"})
        mock_create.assert_called_once_with(["/dev/sdb", "/dev/sdc"], "1")

    def test_create_failure_returns_400_with_error_code(self):
        with mock.patch.object(app_module.raid_mutate, "create_raid_array",
                                return_value={"success": False, "error_code": "raid.not_enough_devices", "error_context": {}}):
            res = self.client.post("/api/raid/create", json={"devices": ["/dev/sdb"], "level": "5"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "raid.not_enough_devices")

    def test_detach_success(self):
        with mock.patch.object(app_module.raid_mutate, "detach_member", return_value={"success": True}) as mock_detach:
            res = self.client.post("/api/raid/md0/detach", json={"device": "/dev/sdb"})
        self.assertEqual(res.status_code, 200)
        mock_detach.assert_called_once_with("md0", "/dev/sdb")

    def test_add_success(self):
        with mock.patch.object(app_module.raid_mutate, "add_member", return_value={"success": True}) as mock_add:
            res = self.client.post("/api/raid/md0/add", json={"device": "/dev/sdb"})
        self.assertEqual(res.status_code, 200)
        mock_add.assert_called_once_with("md0", "/dev/sdb")

    def test_add_failure_returns_400(self):
        with mock.patch.object(app_module.raid_mutate, "add_member",
                                return_value={"success": False, "error_code": "raid.device_not_free", "error_context": {"device": "/dev/sdb"}}):
            res = self.client.post("/api/raid/md0/add", json={"device": "/dev/sdb"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "raid.device_not_free")


if __name__ == "__main__":
    unittest.main()
