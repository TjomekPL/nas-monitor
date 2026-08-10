import os
import re
import sys
import tempfile
import shutil
import logging
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
        auth.set_session_duration_minutes(24 * 60)
        auth.set_credentials("admin", "anotherpass1")
        self.assertEqual(auth.get_session_duration_minutes(), 24 * 60)


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
        self.assertIsNone(auth.get_session_duration_minutes())

    def test_set_and_get(self):
        auth.set_credentials("admin", "correcthorse9")
        result = auth.set_session_duration_minutes(24 * 60)
        self.assertTrue(result["success"])
        self.assertEqual(auth.get_session_duration_minutes(), 24 * 60)

    def test_short_durations_supported(self):
        auth.set_credentials("admin", "correcthorse9")
        for minutes in [5, 15, 30, 60]:
            result = auth.set_session_duration_minutes(minutes)
            self.assertTrue(result["success"], minutes)
            self.assertEqual(auth.get_session_duration_minutes(), minutes)

    def test_set_back_to_none(self):
        auth.set_credentials("admin", "correcthorse9")
        auth.set_session_duration_minutes(24 * 60)
        auth.set_session_duration_minutes(None)
        self.assertIsNone(auth.get_session_duration_minutes())

    def test_rejects_out_of_range(self):
        auth.set_credentials("admin", "correcthorse9")
        for bad in [0, -5, 4, 30 * 24 * 60 + 1]:
            result = auth.set_session_duration_minutes(bad)
            self.assertFalse(result["success"])
            self.assertEqual(result["error_code"], "auth.invalid_session_duration")

    def test_requires_credentials_configured_first(self):
        result = auth.set_session_duration_minutes(24 * 60)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "auth.not_configured")


class TestUiScale(AuthTestCase):
    def test_default_is_110(self):
        auth.set_credentials("admin", "correcthorse9")
        self.assertEqual(auth.get_ui_scale(), 110)

    def test_default_is_110_even_before_credentials_exist(self):
        self.assertEqual(auth.get_ui_scale(), 110)

    def test_set_and_get(self):
        auth.set_credentials("admin", "correcthorse9")
        result = auth.set_ui_scale(90)
        self.assertTrue(result["success"])
        self.assertEqual(auth.get_ui_scale(), 90)

    def test_set_130(self):
        auth.set_credentials("admin", "correcthorse9")
        auth.set_ui_scale(130)
        self.assertEqual(auth.get_ui_scale(), 130)

    def test_rejects_values_outside_the_three_presets(self):
        auth.set_credentials("admin", "correcthorse9")
        for bad in [0, 100, 120, 150, -90]:
            result = auth.set_ui_scale(bad)
            self.assertFalse(result["success"])
            self.assertEqual(result["error_code"], "auth.invalid_ui_scale")

    def test_rejects_non_numeric(self):
        auth.set_credentials("admin", "correcthorse9")
        result = auth.set_ui_scale("huge")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "auth.invalid_ui_scale")

    def test_requires_credentials_configured_first(self):
        result = auth.set_ui_scale(90)
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

    def test_concurrent_workers_on_a_fresh_install_agree_on_one_key(self):
        # Reproduces the real report: two gunicorn worker processes
        # starting near-simultaneously on a brand new install, before
        # the key file exists yet. Every "worker" here calls the
        # function with NO key file present (simulating the race
        # window), and they must all end up agreeing on exactly one
        # key - not each keeping whatever they individually generated.
        import concurrent.futures

        def worker():
            return auth.get_or_create_secret_key()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: worker(), range(8)))

        self.assertEqual(len(set(results)), 1, "all workers must agree on the same key")

        # And it matches what's actually persisted on disk - no worker
        # is silently running with a key that doesn't match the file.
        on_disk = auth.get_or_create_secret_key()
        self.assertEqual(on_disk, results[0])


class TestFailedLoginFileLog(unittest.TestCase):
    """auth.log_failed_login_attempt - the fail2ban-facing log, entirely
    separate from the JSON-backed lockout tested above."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.tmpdir, "auth.log")
        self.dir_patch = mock.patch("nas_monitor.auth.AUTH_LOG_DIR", self.tmpdir)
        self.file_patch = mock.patch("nas_monitor.auth.AUTH_LOG_FILE", self.log_file)
        self.logger_patch = mock.patch("nas_monitor.auth._auth_file_logger", None)
        self.dir_patch.start()
        self.file_patch.start()
        self.logger_patch.start()

    def tearDown(self):
        self.dir_patch.stop()
        self.file_patch.stop()
        self.logger_patch.stop()
        # drop any handler the test opened, or Windows/some filesystems
        # would refuse to let tearDown remove a file still held open
        logger = logging.getLogger("nas_monitor.auth_file_log")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_writes_a_line_fail2bans_filter_can_parse(self):
        auth.log_failed_login_attempt("admin", "192.168.1.55")
        with open(self.log_file) as f:
            content = f.read()
        pattern = r"^\S+ \S+ Failed login attempt for user 'admin' from (?P<host>\S+)\s*$"
        match = re.search(pattern, content, re.MULTILINE)
        self.assertIsNotNone(match, content)
        self.assertEqual(match.group("host"), "192.168.1.55")

    def test_handles_missing_remote_addr_without_raising(self):
        auth.log_failed_login_attempt("admin", None)
        with open(self.log_file) as f:
            self.assertIn("from unknown", f.read())

    def test_never_raises_when_log_dir_cannot_be_created(self):
        with mock.patch("nas_monitor.auth.AUTH_LOG_DIR", "/this/path/cannot/exist/on/purpose"), \
             mock.patch("nas_monitor.auth.AUTH_LOG_FILE", "/this/path/cannot/exist/on/purpose/auth.log"), \
             mock.patch("nas_monitor.auth._auth_file_logger", None):
            auth.log_failed_login_attempt("admin", "1.2.3.4")  # must not raise


class TestLoginRateLimiting(AuthTestCase):
    def test_not_locked_out_with_no_history(self):
        locked, remaining = auth.is_locked_out("admin")
        self.assertFalse(locked)
        self.assertEqual(remaining, 0)

    def test_not_locked_out_below_threshold(self):
        for _ in range(auth.MAX_LOGIN_ATTEMPTS - 1):
            auth.record_failed_login("admin")
        locked, _ = auth.is_locked_out("admin")
        self.assertFalse(locked)

    def test_locks_out_at_threshold(self):
        for _ in range(auth.MAX_LOGIN_ATTEMPTS):
            auth.record_failed_login("admin")
        locked, remaining = auth.is_locked_out("admin")
        self.assertTrue(locked)
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, auth.LOGIN_LOCKOUT_SECONDS)

    def test_lockout_is_per_username_not_global(self):
        for _ in range(auth.MAX_LOGIN_ATTEMPTS):
            auth.record_failed_login("admin")
        locked, _ = auth.is_locked_out("someone-else")
        self.assertFalse(locked, "a lockout on one username must not affect another")

    def test_successful_login_clears_history(self):
        for _ in range(auth.MAX_LOGIN_ATTEMPTS - 1):
            auth.record_failed_login("admin")
        auth.clear_login_attempts("admin")
        for _ in range(auth.MAX_LOGIN_ATTEMPTS - 1):
            auth.record_failed_login("admin")
        # needed the full threshold again after clearing - the earlier
        # near-miss attempts must not carry over
        locked, _ = auth.is_locked_out("admin")
        self.assertFalse(locked)

    def test_lockout_expires_after_lockout_window(self):
        with mock.patch("nas_monitor.auth.time.time") as mock_time:
            mock_time.return_value = 1000.0
            for _ in range(auth.MAX_LOGIN_ATTEMPTS):
                auth.record_failed_login("admin")
            locked, _ = auth.is_locked_out("admin")
            self.assertTrue(locked)

            mock_time.return_value = 1000.0 + auth.LOGIN_LOCKOUT_SECONDS + 1
            locked, remaining = auth.is_locked_out("admin")
            self.assertFalse(locked)
            self.assertEqual(remaining, 0)

    def test_old_failures_outside_window_do_not_accumulate(self):
        with mock.patch("nas_monitor.auth.time.time") as mock_time:
            mock_time.return_value = 1000.0
            for _ in range(auth.MAX_LOGIN_ATTEMPTS - 1):
                auth.record_failed_login("admin")

            # a single stale failure long after the window shouldn't add
            # to the earlier near-miss count
            mock_time.return_value = 1000.0 + auth.LOGIN_ATTEMPT_WINDOW_SECONDS + 60
            auth.record_failed_login("admin")
            locked, _ = auth.is_locked_out("admin")
            self.assertFalse(locked)


if __name__ == "__main__":
    unittest.main()
