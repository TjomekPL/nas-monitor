import os
import sys
import time
import tempfile
import shutil
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import network_mutate  # noqa: E402


class TestIsValidIpv4(unittest.TestCase):
    def test_accepts_normal_addresses(self):
        for ip in ["192.168.0.1", "10.0.0.1", "255.255.255.255", "0.0.0.0", "1.1.1.1"]:
            self.assertTrue(network_mutate.is_valid_ipv4(ip), ip)

    def test_rejects_double_dot(self):
        self.assertFalse(network_mutate.is_valid_ipv4("1921..68.0.1"))

    def test_rejects_missing_dot(self):
        self.assertFalse(network_mutate.is_valid_ipv4("1921680.0.1"))

    def test_rejects_octet_over_255(self):
        self.assertFalse(network_mutate.is_valid_ipv4("999.99.99.9"))

    def test_rejects_empty_and_garbage(self):
        for ip in ["", "not-an-ip", "192.168.0", "192.168.0.1.5", None]:
            self.assertFalse(network_mutate.is_valid_ipv4(ip))


class TestIsValidPrefixlen(unittest.TestCase):
    def test_accepts_valid_range(self):
        for p in [0, 8, 24, 32, "24"]:
            self.assertTrue(network_mutate.is_valid_prefixlen(p))

    def test_rejects_out_of_range(self):
        for p in [-1, 33, 999]:
            self.assertFalse(network_mutate.is_valid_prefixlen(p))

    def test_rejects_non_numeric(self):
        for p in ["abc", None, "24.5"]:
            self.assertFalse(network_mutate.is_valid_prefixlen(p))


class TestGatewayInSubnet(unittest.TestCase):
    def test_gateway_in_same_subnet(self):
        self.assertTrue(network_mutate.gateway_in_subnet("192.168.0.10", 24, "192.168.0.1"))

    def test_gateway_outside_subnet(self):
        self.assertFalse(network_mutate.gateway_in_subnet("192.168.0.10", 24, "10.0.0.1"))

    def test_gateway_outside_narrower_subnet(self):
        # /28 = 192.168.0.0-15, gateway .1 is inside, .20 is not
        self.assertTrue(network_mutate.gateway_in_subnet("192.168.0.10", 28, "192.168.0.1"))
        self.assertFalse(network_mutate.gateway_in_subnet("192.168.0.10", 28, "192.168.0.20"))

    def test_malformed_input_is_false_not_raised(self):
        self.assertFalse(network_mutate.gateway_in_subnet("garbage", 24, "192.168.0.1"))


class NetworkMutateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_patch = mock.patch("nas_monitor.network_mutate.state_store.STATE_DIR", self.tmpdir)
        self.state_patch.start()
        self.backend_patch = mock.patch("nas_monitor.network_mutate.network.detect_backend", return_value="networkmanager")
        self.backend_patch.start()

    def tearDown(self):
        self.backend_patch.stop()
        self.state_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        network_mutate._active_timer = None


def _fake_nmcli_run(commands_log):
    """Build a system_tools.run fake that logs every call and returns
    canned success output for the nmcli subcommands request_ip_change
    actually issues."""

    def fake_run(cmd, *args, **kwargs):
        commands_log.append(cmd)
        if cmd[1:4] == ["-t", "-f", "DEVICE,CONNECTION"]:
            return 0, "eno1:Wired connection 1\n", ""
        if "show" in cmd and "connection" in cmd:
            return 0, (
                "ipv4.method:auto\n"
                "ipv4.addresses:192.168.0.10/24\n"
                "ipv4.gateway:192.168.0.1\n"
                "ipv4.dns:1.1.1.1\n"
            ), ""
        if "modify" in cmd or "up" in cmd:
            return 0, "", ""
        return 0, "", ""

    return fake_run


class TestRequestIpChange(NetworkMutateTestCase):
    @mock.patch("nas_monitor.network_mutate.system_tools.find_binary", return_value="/usr/bin/nmcli")
    def test_rejects_invalid_ip(self, mock_find):
        result = network_mutate.request_ip_change("eno1", "999.99.99.9", 24, "192.168.0.1", [])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "network.invalid_ip")

    @mock.patch("nas_monitor.network_mutate.system_tools.find_binary", return_value="/usr/bin/nmcli")
    def test_rejects_gateway_outside_subnet(self, mock_find):
        result = network_mutate.request_ip_change("eno1", "192.168.0.10", 24, "10.0.0.1", [])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "network.gateway_outside_subnet")

    @mock.patch("nas_monitor.network_mutate.network.detect_backend", return_value="systemd-networkd")
    def test_rejects_unsupported_backend(self, mock_backend):
        result = network_mutate.request_ip_change("eno1", "192.168.0.10", 24, "192.168.0.1", [])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "network.backend_unsupported")

    @mock.patch("nas_monitor.network_mutate.system_tools.find_binary", return_value="/usr/bin/nmcli")
    def test_successful_change_persists_pending_state_and_schedules_timer(self, mock_find):
        log = []
        with mock.patch("nas_monitor.network_mutate.system_tools.run", side_effect=_fake_nmcli_run(log)):
            result = network_mutate.request_ip_change("eno1", "192.168.0.20", 24, "192.168.0.1", ["1.1.1.1"])
        self.assertTrue(result["success"])
        self.assertIn("token", result)
        self.assertEqual(result["new_host"], "192.168.0.20:8420")

        pending = network_mutate.get_pending_change()
        self.assertIsNotNone(pending)
        self.assertEqual(pending["token"], result["token"])
        self.assertFalse(pending["confirmed"])
        self.assertEqual(pending["snapshot"]["ipv4.addresses"], "192.168.0.10/24")

        self.assertIsNotNone(network_mutate._active_timer)
        network_mutate._active_timer.cancel()

    @mock.patch("nas_monitor.network_mutate.system_tools.find_binary", return_value="/usr/bin/nmcli")
    def test_rejects_when_a_change_is_already_pending(self, mock_find):
        log = []
        with mock.patch("nas_monitor.network_mutate.system_tools.run", side_effect=_fake_nmcli_run(log)):
            network_mutate.request_ip_change("eno1", "192.168.0.20", 24, "192.168.0.1", [])
            network_mutate._active_timer.cancel()
            result = network_mutate.request_ip_change("eno1", "192.168.0.30", 24, "192.168.0.1", [])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "network.change_already_pending")

    @mock.patch("nas_monitor.network_mutate.system_tools.find_binary", return_value="/usr/bin/nmcli")
    def test_apply_failure_clears_pending_state(self, mock_find):
        def failing_run(cmd, *args, **kwargs):
            if cmd[1:4] == ["-t", "-f", "DEVICE,CONNECTION"]:
                return 0, "eno1:Wired connection 1\n", ""
            if "show" in cmd:
                return 0, "ipv4.method:auto\nipv4.addresses:192.168.0.10/24\nipv4.gateway:192.168.0.1\nipv4.dns:1.1.1.1\n", ""
            if "modify" in cmd:
                return 1, "", "connection modify failed"
            return 0, "", ""

        with mock.patch("nas_monitor.network_mutate.system_tools.run", side_effect=failing_run):
            result = network_mutate.request_ip_change("eno1", "192.168.0.20", 24, "192.168.0.1", [])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")
        self.assertIsNone(network_mutate.get_pending_change())


class TestConfirmChange(NetworkMutateTestCase):
    @mock.patch("nas_monitor.network_mutate.system_tools.find_binary", return_value="/usr/bin/nmcli")
    def test_confirm_marks_pending_change_confirmed(self, mock_find):
        log = []
        with mock.patch("nas_monitor.network_mutate.system_tools.run", side_effect=_fake_nmcli_run(log)):
            applied = network_mutate.request_ip_change("eno1", "192.168.0.20", 24, "192.168.0.1", [])
            network_mutate._active_timer.cancel()

            result = network_mutate.confirm_change(applied["token"])
            self.assertTrue(result["success"])
            self.assertIsNone(network_mutate.get_pending_change())

    def test_confirm_rejects_unknown_token(self):
        result = network_mutate.confirm_change("does-not-exist")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "network.change_not_found")


class TestRevertIfStillPending(NetworkMutateTestCase):
    @mock.patch("nas_monitor.network_mutate.system_tools.find_binary", return_value="/usr/bin/nmcli")
    def test_reverts_when_still_unconfirmed(self, mock_find):
        log = []
        with mock.patch("nas_monitor.network_mutate.system_tools.run", side_effect=_fake_nmcli_run(log)):
            applied = network_mutate.request_ip_change("eno1", "192.168.0.20", 24, "192.168.0.1", [])
            network_mutate._active_timer.cancel()
            log.clear()

            network_mutate._revert_if_still_pending(applied["token"])

        # the revert should have re-applied the snapshotted values
        modify_calls = [c for c in log if "modify" in c]
        self.assertTrue(any("192.168.0.10/24" in c for c in modify_calls[0]) for modify_calls in [modify_calls])
        self.assertIsNone(network_mutate.get_pending_change())

    @mock.patch("nas_monitor.network_mutate.system_tools.find_binary", return_value="/usr/bin/nmcli")
    def test_does_not_revert_once_confirmed(self, mock_find):
        log = []
        with mock.patch("nas_monitor.network_mutate.system_tools.run", side_effect=_fake_nmcli_run(log)):
            applied = network_mutate.request_ip_change("eno1", "192.168.0.20", 24, "192.168.0.1", [])
            network_mutate._active_timer.cancel()
            network_mutate.confirm_change(applied["token"])
            log.clear()

            network_mutate._revert_if_still_pending(applied["token"])

        self.assertEqual(log, [])  # no nmcli calls at all - nothing to revert

    def test_noop_when_nothing_pending(self):
        network_mutate._revert_if_still_pending("whatever")  # must not raise


class TestStartupRecovery(NetworkMutateTestCase):
    def test_noop_when_nothing_pending(self):
        network_mutate.check_and_recover_on_startup()  # must not raise
        self.assertIsNone(network_mutate._active_timer)

    @mock.patch("nas_monitor.network_mutate.system_tools.find_binary", return_value="/usr/bin/nmcli")
    def test_reverts_immediately_if_grace_period_already_elapsed(self, mock_find):
        from datetime import datetime, timezone, timedelta

        log = []
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=999)).isoformat(timespec="seconds")
        network_mutate.state_store.save(
            network_mutate.PENDING_CHANGE_FILE,
            {
                "token": "abc",
                "interface": "eno1",
                "connection": "Wired connection 1",
                "snapshot": {"ipv4.method": "auto", "ipv4.addresses": "192.168.0.10/24", "ipv4.gateway": "192.168.0.1", "ipv4.dns": "1.1.1.1"},
                "confirmed": False,
                "created_at": old_time,
            },
        )
        with mock.patch("nas_monitor.network_mutate.system_tools.run", side_effect=_fake_nmcli_run(log)):
            network_mutate.check_and_recover_on_startup()

        self.assertIsNone(network_mutate.get_pending_change())
        self.assertTrue(any("modify" in c for c in log))

    @mock.patch("nas_monitor.network_mutate.system_tools.find_binary", return_value="/usr/bin/nmcli")
    def test_rearms_timer_if_grace_period_still_remaining(self, mock_find):
        from datetime import datetime, timezone

        recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
        network_mutate.state_store.save(
            network_mutate.PENDING_CHANGE_FILE,
            {
                "token": "abc",
                "interface": "eno1",
                "connection": "Wired connection 1",
                "snapshot": {"ipv4.method": "auto", "ipv4.addresses": "192.168.0.10/24", "ipv4.gateway": "192.168.0.1", "ipv4.dns": "1.1.1.1"},
                "confirmed": False,
                "created_at": recent,
            },
        )
        network_mutate.check_and_recover_on_startup()
        self.assertIsNotNone(network_mutate._active_timer)
        network_mutate._active_timer.cancel()
        # still pending, not reverted yet
        self.assertIsNotNone(network_mutate.get_pending_change())


if __name__ == "__main__":
    unittest.main()


class TestSetHostname(unittest.TestCase):
    @mock.patch.object(network_mutate.system_tools, "find_binary")
    @mock.patch.object(network_mutate.system_tools, "run")
    def test_sets_hostname_and_restarts_avahi_and_samba(self, mock_run, mock_find):
        mock_find.side_effect = lambda name: f"/usr/bin/{name}"
        mock_run.return_value = (0, "", "")
        result = network_mutate.set_hostname("nas-mon")
        self.assertTrue(result["success"])
        self.assertEqual(result["hostname"], "nas-mon")
        mock_run.assert_any_call(["/usr/bin/hostnamectl", "set-hostname", "nas-mon"], timeout=15)
        mock_run.assert_any_call(["/usr/bin/systemctl", "restart", "avahi-daemon"], timeout=15)
        # Real report: a Windows machine kept showing the box under its
        # old name in network browsing well after avahi/.local had
        # already picked up the new one - smbd/nmbd cache the hostname
        # at their own startup and were never being restarted at all.
        mock_run.assert_any_call(["/usr/bin/systemctl", "restart", "smbd"], timeout=15)
        mock_run.assert_any_call(["/usr/bin/systemctl", "restart", "nmbd"], timeout=15)

    def test_rejects_empty_hostname(self):
        result = network_mutate.set_hostname("")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "network.invalid_hostname")

    def test_rejects_hostname_with_invalid_characters(self):
        for bad in ["nas monitor", "nas_monitor", "nas.monitor", "nas!", "-nas", "nas-"]:
            result = network_mutate.set_hostname(bad)
            self.assertFalse(result["success"], bad)
            self.assertEqual(result["error_code"], "network.invalid_hostname", bad)

    def test_rejects_hostname_over_63_chars(self):
        result = network_mutate.set_hostname("a" * 64)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "network.invalid_hostname")

    @mock.patch.object(network_mutate.system_tools, "find_binary", return_value=None)
    def test_reports_missing_hostnamectl(self, mock_find):
        result = network_mutate.set_hostname("nas-mon")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.tool_missing")

    @mock.patch.object(network_mutate.system_tools, "find_binary")
    @mock.patch.object(network_mutate.system_tools, "run")
    def test_surfaces_hostnamectl_failure(self, mock_run, mock_find):
        mock_find.side_effect = lambda name: f"/usr/bin/{name}"
        mock_run.return_value = (1, "", "hostnamectl: Failed to set hostname")
        result = network_mutate.set_hostname("nas-mon")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "system.command_failed")

    @mock.patch.object(network_mutate.system_tools, "find_binary")
    @mock.patch.object(network_mutate.system_tools, "run")
    def test_a_failed_avahi_restart_is_a_warning_not_a_failure(self, mock_run, mock_find):
        # The hostname itself is already changed at that point - the
        # part that actually matters - so avahi failing to restart
        # must not make the whole rename look like it failed.
        mock_find.side_effect = lambda name: f"/usr/bin/{name}"
        mock_run.side_effect = [(0, "", ""), (1, "", "Unit avahi-daemon.service not found."), (0, "", ""), (0, "", "")]
        result = network_mutate.set_hostname("nas-mon")
        self.assertTrue(result["success"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["warnings"][0]["code"], "network.avahi_restart_failed")

    @mock.patch.object(network_mutate.system_tools, "find_binary")
    @mock.patch.object(network_mutate.system_tools, "run")
    def test_a_failed_samba_restart_is_a_warning_not_a_failure(self, mock_run, mock_find):
        # nmbd specifically isn't always installed/enabled on every
        # setup (some minimal Samba configs skip it) - its restart
        # failing is just as much a non-fatal warning as avahi's.
        mock_find.side_effect = lambda name: f"/usr/bin/{name}"
        mock_run.side_effect = [(0, "", ""), (0, "", ""), (0, "", ""), (1, "", "Unit nmbd.service not found.")]
        result = network_mutate.set_hostname("nas-mon")
        self.assertTrue(result["success"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["warnings"][0]["code"], "network.samba_restart_failed")
        self.assertEqual(result["warnings"][0]["context"]["service"], "nmbd")
