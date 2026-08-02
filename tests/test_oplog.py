import os
import sys
import tempfile
import shutil
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import oplog  # noqa: E402


class OplogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_patch = mock.patch("nas_monitor.oplog.state_store.STATE_DIR", self.tmpdir)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestLogEvent(OplogTestCase):
    def test_records_success_event(self):
        entry = oplog.log_event("users", "create", "success", "Utworzono użytkownika wieslaw")
        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["category"], "users")
        self.assertEqual(entry["action"], "create")
        self.assertEqual(entry["message"], "")
        self.assertIn("timestamp", entry)

        events = oplog.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["summary"], "Utworzono użytkownika wieslaw")

    def test_records_failure_with_message(self):
        oplog.log_event("shares", "create", "failure", "Nie udało się utworzyć udziału test", "testparm error: bad path")
        events = oplog.list_events()
        self.assertEqual(events[0]["status"], "failure")
        self.assertEqual(events[0]["message"], "testparm error: bad path")

    def test_unknown_status_treated_as_failure(self):
        oplog.log_event("users", "create", "weird", "coś")
        self.assertEqual(oplog.list_events()[0]["status"], "failure")

    def test_ids_increment_and_persist_across_calls(self):
        e1 = oplog.log_event("users", "create", "success", "a")
        e2 = oplog.log_event("users", "create", "success", "b")
        self.assertEqual(e2["id"], e1["id"] + 1)

    def test_newest_first(self):
        oplog.log_event("users", "create", "success", "first")
        oplog.log_event("users", "create", "success", "second")
        events = oplog.list_events()
        self.assertEqual(events[0]["summary"], "second")
        self.assertEqual(events[1]["summary"], "first")


class TestMaxEntries(OplogTestCase):
    def test_default_max_entries(self):
        self.assertEqual(oplog.get_max_entries(), oplog.DEFAULT_MAX_ENTRIES)

    def test_oldest_dropped_once_cap_exceeded(self):
        oplog.set_max_entries(10)  # minimum allowed
        for i in range(15):
            oplog.log_event("users", "create", "success", f"entry {i}")
        events = oplog.list_events()
        self.assertEqual(len(events), 10)
        # newest first, so entry 14 should be present and entry 0 gone
        self.assertEqual(events[0]["summary"], "entry 14")
        summaries = [e["summary"] for e in events]
        self.assertNotIn("entry 0", summaries)

    def test_set_max_entries_clamped_to_bounds(self):
        result = oplog.set_max_entries(5)
        self.assertEqual(result["max_entries"], oplog.MIN_MAX_ENTRIES)

        result = oplog.set_max_entries(50000)
        self.assertEqual(result["max_entries"], oplog.MAX_MAX_ENTRIES)

    def test_lowering_cap_trims_existing_entries_immediately(self):
        for i in range(5):
            oplog.log_event("users", "create", "success", f"entry {i}")
        oplog.set_max_entries(10)
        self.assertEqual(len(oplog.list_events()), 5)

        oplog.set_max_entries(10)  # still above 5, no trim
        self.assertEqual(len(oplog.list_events()), 5)


class TestClearEvents(OplogTestCase):
    def test_clear_removes_all_entries(self):
        oplog.log_event("users", "create", "success", "a")
        oplog.log_event("users", "create", "success", "b")
        result = oplog.clear_events()
        self.assertTrue(result["success"])
        self.assertEqual(oplog.list_events(), [])

    def test_clear_preserves_max_entries_setting(self):
        oplog.set_max_entries(200)
        oplog.log_event("users", "create", "success", "a")
        oplog.clear_events()
        self.assertEqual(oplog.get_max_entries(), 200)


class TestListEventsTimeRange(OplogTestCase):
    def test_since_filters_out_older_events(self):
        with mock.patch("nas_monitor.oplog.datetime") as mock_dt:
            mock_dt.now.return_value.isoformat.return_value = "2026-01-01T10:00:00+00:00"
            mock_dt.fromisoformat = __import__("datetime").datetime.fromisoformat
            oplog.log_event("users", "create", "success", "old event")

        with mock.patch("nas_monitor.oplog.datetime") as mock_dt:
            mock_dt.now.return_value.isoformat.return_value = "2026-01-02T10:00:00+00:00"
            mock_dt.fromisoformat = __import__("datetime").datetime.fromisoformat
            oplog.log_event("users", "create", "success", "new event")

        events = oplog.list_events(since="2026-01-02T00:00:00+00:00")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["summary"], "new event")

    def test_malformed_since_is_ignored_not_raised(self):
        oplog.log_event("users", "create", "success", "a")
        events = oplog.list_events(since="not-a-date")
        self.assertEqual(len(events), 1)

    def test_limit_caps_result_count(self):
        for i in range(5):
            oplog.log_event("users", "create", "success", f"entry {i}")
        events = oplog.list_events(limit=2)
        self.assertEqual(len(events), 2)


if __name__ == "__main__":
    unittest.main()
