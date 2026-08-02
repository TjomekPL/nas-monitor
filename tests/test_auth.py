import os
import sys
import tempfile
import shutil
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import auth  # noqa: E402


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_patch = mock.patch("nas_monitor.auth.state_store.STATE_DIR", self.tmpdir)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestValidatePassword(unittest.TestCase):
    def test_accepts_valid_passwords(self):
        for pw in ["abcdefgh12", "Sup3rSecret!", "1234567890a", "correcthorse9"]:
            self.assertTrue(auth.validate_password(pw)["success"], pw)

    def test_rejects_too_short(self):
        result = auth.validate_password("abc123")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "auth.password_too_short")

    def test_rejects_letters_only(self):
        result = auth.validate_password("abcdefghij")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "auth.password_needs_digit")

    def test_rejects_digits_only(self):
        result = auth.validate_password("1234567890")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "auth.password_needs_letter")

    def test_uppercase_and_special_chars_are_optional_not_required(self):
        # lowercase-only, no special chars, but has letters+digits+length -> valid
        self.assertTrue(auth.validate_password("plainpass1")["success"])
        # also fine WITH uppercase/special - never rejected for having them
        self.assertTrue(auth.validate_password("Plain$Pass1")["success"])


class TestSetCredentials(AuthTestCase):
    def test_creates_credentials_with_hashed_password(self):
        result = auth.set_credentials("admin", "correcthorse9")
        self.assertTrue(result["success"])
        self.assertEqual(result["username"], "admin")
        self.assertTrue(auth.is_configured())
        self.assertEqual(auth.get_username(), "admin")

    def test_rejects_weak_password(self):
        result = auth.set_credentials("admin", "short1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "auth.password_too_short")
        self.assertFalse(auth.is_configured())

    def test_rejects_empty_username(self):
        result = auth.set_credentials("   ", "correcthorse9")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "auth.invalid_username")

    def test_password_is_never_stored_in_plain_text(self):
        auth.set_credentials("admin", "correcthorse9")
        with open(os.path.join(self.tmpdir, auth.CREDENTIALS_FILE)) as f:
            raw = f.read()
        self.assertNotIn("correcthorse9", raw)

    def test_overwriting_credentials_preserves_session_duration_setting(self):
        auth.set_credentials("admin", "correcthorse9")
        auth.set_session_duration_hours(24)
        auth.set_credentials("admin", "anotherpass1")
        self.assertEqual(auth.get_session_duration_hours(), 24)


class TestVerifyCredentials(AuthTestCase):
    def test_correct_credentials(self):
        auth.set_credentials("admin", "correcthorse9")
        self.assertTrue(auth.verify_credentials("admin", "correcthorse9"))

    def test_wrong_password(self):
        auth.set_credentials("admin", "correcthorse9")
        self.assertFalse(auth.verify_credentials("admin", "wrongpassword1"))

    def test_wrong_username(self):
        auth.set_credentials("admin", "correcthorse9")
        self.assertFalse(auth.verify_credentials("someoneelse", "correcthorse9"))

    def test_not_configured_yet(self):
        self.assertFalse(auth.verify_credentials("admin", "correcthorse9"))


class TestChangePassword(AuthTestCase):
    def test_successful_change(self):
        auth.set_credentials("admin", "correcthorse9")
        result = auth.change_password("correcthorse9", "newpassword1")
        self.assertTrue(result["success"])
        self.assertTrue(auth.verify_credentials("admin", "newpassword1"))
        self.assertFalse(auth.verify_credentials("admin", "correcthorse9"))

    def test_rejects_wrong_current_password(self):
        auth.set_credentials("admin", "correcthorse9")
        result = auth.change_password("wrongcurrent1", "newpassword1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "auth.wrong_current_password")
        self.assertTrue(auth.verify_credentials("admin", "correcthorse9"))  # unchanged

    def test_rejects_weak_new_password(self):
        auth.set_credentials("admin", "correcthorse9")
        result = auth.change_password("correcthorse9", "weak")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "auth.password_too_short")

    def test_not_configured_yet(self):
        result = auth.change_password("whatever1", "newpassword1")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "auth.not_configured")


class TestSessionDuration(AuthTestCase):
    def test_default_is_none_until_browser_closes(self):
        auth.set_credentials("admin", "correcthorse9")
        self.assertIsNone(auth.get_session_duration_hours())

    def test_set_and_get(self):
        auth.set_credentials("admin", "correcthorse9")
        result = auth.set_session_duration_hours(24)
        self.assertTrue(result["success"])
        self.assertEqual(auth.get_session_duration_hours(), 24)

    def test_set_back_to_none(self):
        auth.set_credentials("admin", "correcthorse9")
        auth.set_session_duration_hours(24)
        auth.set_session_duration_hours(None)
        self.assertIsNone(auth.get_session_duration_hours())

    def test_rejects_out_of_range(self):
        auth.set_credentials("admin", "correcthorse9")
        for bad in [0, -5, 24 * 31]:
            result = auth.set_session_duration_hours(bad)
            self.assertFalse(result["success"])
            self.assertEqual(result["error_code"], "auth.invalid_session_duration")

    def test_requires_credentials_configured_first(self):
        result = auth.set_session_duration_hours(24)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "auth.not_configured")


class TestAuthEnabled(unittest.TestCase):
    def test_enabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(auth.auth_enabled())

    def test_disabled_via_env_var(self):
        with mock.patch.dict(os.environ, {"AUTH_ENABLED": "0"}):
            self.assertFalse(auth.auth_enabled())
        with mock.patch.dict(os.environ, {"AUTH_ENABLED": "false"}):
            self.assertFalse(auth.auth_enabled())
        with mock.patch.dict(os.environ, {"AUTH_ENABLED": "FALSE"}):
            self.assertFalse(auth.auth_enabled())

    def test_enabled_for_other_values(self):
        with mock.patch.dict(os.environ, {"AUTH_ENABLED": "1"}):
            self.assertTrue(auth.auth_enabled())
        with mock.patch.dict(os.environ, {"AUTH_ENABLED": "yes"}):
            self.assertTrue(auth.auth_enabled())


class TestSecretKey(AuthTestCase):
    def test_generates_and_persists(self):
        key1 = auth.get_or_create_secret_key()
        key2 = auth.get_or_create_secret_key()
        self.assertEqual(key1, key2)
        self.assertGreaterEqual(len(key1), 32)

    def test_different_across_fresh_stores(self):
        key1 = auth.get_or_create_secret_key()
        with tempfile.TemporaryDirectory() as other_dir:
            with mock.patch("nas_monitor.auth.state_store.STATE_DIR", other_dir):
                key2 = auth.get_or_create_secret_key()
        self.assertNotEqual(key1, key2)


if __name__ == "__main__":
    unittest.main()
