import io
import os
import sys
import tempfile
import shutil
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import setup_admin  # noqa: E402


class SetupAdminTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_patch = mock.patch("nas_monitor.auth.state_store.STATE_DIR", self.tmpdir)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_with_stdin(self, text):
        with mock.patch("sys.stdin", io.StringIO(text)):
            return setup_admin.main()


class TestSetupAdmin(SetupAdminTestCase):
    def test_successful_setup(self):
        code = self.run_with_stdin("admin\ncorrecthorse9\n")
        self.assertEqual(code, 0)
        from nas_monitor import auth
        self.assertTrue(auth.is_configured())
        self.assertTrue(auth.verify_credentials("admin", "correcthorse9"))

    def test_rejects_weak_password(self):
        code = self.run_with_stdin("admin\nweak\n")
        self.assertEqual(code, 1)
        from nas_monitor import auth
        self.assertFalse(auth.is_configured())

    def test_rejects_incomplete_stdin(self):
        code = self.run_with_stdin("onlyusername\n")
        self.assertEqual(code, 1)

    def test_custom_username(self):
        code = self.run_with_stdin("wieslaw\ncorrecthorse9\n")
        self.assertEqual(code, 0)
        from nas_monitor import auth
        self.assertEqual(auth.get_username(), "wieslaw")


if __name__ == "__main__":
    unittest.main()
