import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nas_monitor import network  # noqa: E402


IP_ADDR_SAMPLE = """
[
  {"ifname": "lo", "flags": ["LOOPBACK", "UP"], "operstate": "UNKNOWN", "address": "00:00:00:00:00:00",
   "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
  {"ifname": "eth0", "flags": ["BROADCAST", "UP"], "operstate": "UP", "address": "aa:bb:cc:dd:ee:ff",
   "addr_info": [{"family": "inet", "local": "192.168.0.50", "prefixlen": 24}]},
  {"ifname": "eth1", "flags": ["BROADCAST"], "operstate": "DOWN", "address": "11:22:33:44:55:66",
   "addr_info": []}
]
"""

IP_ROUTE_SAMPLE = """[{"dst": "default", "gateway": "192.168.0.1", "dev": "eth0", "flags": []}]"""


class TestPrefixlenToNetmask(unittest.TestCase):
    def test_common_prefixes(self):
        self.assertEqual(network._prefixlen_to_netmask(24), "255.255.255.0")
        self.assertEqual(network._prefixlen_to_netmask(16), "255.255.0.0")
        self.assertEqual(network._prefixlen_to_netmask(8), "255.0.0.0")
        self.assertEqual(network._prefixlen_to_netmask(32), "255.255.255.255")
        self.assertEqual(network._prefixlen_to_netmask(0), "0.0.0.0")


class TestClassifyInterfaceType(unittest.TestCase):
    def _fake_sysfs(self, devices):
        """devices: {iface: None | {"wireless": bool, "bus": str}}
        None means no /device link at all (purely virtual interface)."""

        def fake_islink(path):
            parts = path.split("/")
            iface = parts[4] if len(parts) > 4 else None
            return iface in devices and devices[iface] is not None and path.endswith("/device")

        def fake_isdir(path):
            parts = path.split("/")
            if len(parts) < 5:
                return False
            iface = parts[4]
            if iface not in devices or devices[iface] is None:
                return False
            if path.endswith("/wireless"):
                return devices[iface].get("wireless", False)
            if path.endswith("/phy80211"):
                return False
            return False

        def fake_readlink(path):
            parts = path.split("/")
            iface = parts[4] if len(parts) > 4 else None
            if iface in devices and devices[iface] is not None and path.endswith("/device/subsystem"):
                return f"../../../../bus/{devices[iface]['bus']}"
            raise OSError("no such link")

        return fake_islink, fake_isdir, fake_readlink

    def test_real_world_mixed_setup_from_screenshot(self):
        # eno1 = onboard ethernet, enx... = USB ethernet, wlp2s0 = internal
        # WiFi, tailscale0 = purely virtual (no /device at all) - exactly
        # the interface set reported in production
        devices = {
            "eno1": {"wireless": False, "bus": "pci"},
            "enx00e04c680121": {"wireless": False, "bus": "usb"},
            "wlp2s0": {"wireless": True, "bus": "pci"},
            "tailscale0": None,
        }
        fake_islink, fake_isdir, fake_readlink = self._fake_sysfs(devices)
        with mock.patch("nas_monitor.network.os.path.islink", side_effect=fake_islink), \
             mock.patch("nas_monitor.network.os.path.isdir", side_effect=fake_isdir), \
             mock.patch("nas_monitor.network.os.readlink", side_effect=fake_readlink):
            self.assertEqual(network._classify_interface_type("eno1"), {"kind": "ethernet", "bus": "builtin"})
            self.assertEqual(network._classify_interface_type("enx00e04c680121"), {"kind": "ethernet", "bus": "usb"})
            self.assertEqual(network._classify_interface_type("wlp2s0"), {"kind": "wifi", "bus": "builtin"})
            self.assertEqual(network._classify_interface_type("tailscale0"), {"kind": "virtual", "bus": None})

    def test_usb_wifi_dongle(self):
        devices = {"wlx1234": {"wireless": True, "bus": "usb"}}
        fake_islink, fake_isdir, fake_readlink = self._fake_sysfs(devices)
        with mock.patch("nas_monitor.network.os.path.islink", side_effect=fake_islink), \
             mock.patch("nas_monitor.network.os.path.isdir", side_effect=fake_isdir), \
             mock.patch("nas_monitor.network.os.readlink", side_effect=fake_readlink):
            self.assertEqual(network._classify_interface_type("wlx1234"), {"kind": "wifi", "bus": "usb"})


@mock.patch("nas_monitor.network._interface_dns_servers", return_value=[])
@mock.patch("nas_monitor.network._classify_interface_type", return_value={"kind": "ethernet", "bus": None})
class TestListInterfaces(unittest.TestCase):
    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/sbin/ip")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_excludes_loopback_includes_others(self, mock_run, mock_find, mock_classify, mock_dns):
        mock_run.side_effect = [
            (0, IP_ADDR_SAMPLE, ""),
            (0, IP_ROUTE_SAMPLE, ""),
        ]
        result = network.list_interfaces()
        self.assertTrue(result["available"])
        names = [i["name"] for i in result["interfaces"]]
        self.assertNotIn("lo", names)
        self.assertEqual(names, ["eth0", "eth1"])

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/sbin/ip")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_converts_prefixlen_and_attaches_gateway(self, mock_run, mock_find, mock_classify, mock_dns):
        mock_run.side_effect = [
            (0, IP_ADDR_SAMPLE, ""),
            (0, IP_ROUTE_SAMPLE, ""),
        ]
        result = network.list_interfaces()
        eth0 = next(i for i in result["interfaces"] if i["name"] == "eth0")
        self.assertEqual(eth0["addresses"][0]["netmask"], "255.255.255.0")
        self.assertEqual(eth0["gateway"], "192.168.0.1")
        eth1 = next(i for i in result["interfaces"] if i["name"] == "eth1")
        self.assertIsNone(eth1["gateway"])  # no default route via eth1
        self.assertEqual(eth1["addresses"], [])

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/sbin/ip")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_tailscale_style_unknown_state_with_ip_counts_as_effectively_up(self, mock_run, mock_find, mock_classify, mock_dns):
        # Real-world case: a Tailscale interface reports operstate
        # "UNKNOWN" from the kernel even while genuinely connected -
        # that's normal for tunnel-type devices, not a sign it's down.
        sample = """
        [{"ifname": "tailscale0", "flags": ["POINTOPOINT", "UP"], "operstate": "UNKNOWN",
          "address": "", "addr_info": [{"family": "inet", "local": "100.77.203.121", "prefixlen": 32}]}]
        """
        mock_run.side_effect = [(0, sample, ""), (0, "[]", "")]
        result = network.list_interfaces()
        ts = result["interfaces"][0]
        self.assertEqual(ts["state"], "unknown")  # raw state stays honest/visible
        self.assertTrue(ts["effective_up"])  # but treated as up for the status dot

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/sbin/ip")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_unknown_state_with_no_address_is_not_effectively_up(self, mock_run, mock_find, mock_classify, mock_dns):
        # unknown state AND no address at all - genuinely nothing to suggest it's active
        sample = """
        [{"ifname": "dummy0", "flags": [], "operstate": "UNKNOWN", "address": "", "addr_info": []}]
        """
        mock_run.side_effect = [(0, sample, ""), (0, "[]", "")]
        result = network.list_interfaces()
        self.assertFalse(result["interfaces"][0]["effective_up"])

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/sbin/ip")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_explicit_down_state_is_never_effectively_up_even_with_stale_address(self, mock_run, mock_find, mock_classify, mock_dns):
        sample = """
        [{"ifname": "eno1", "flags": [], "operstate": "DOWN", "address": "",
          "addr_info": [{"family": "inet", "local": "10.0.0.5", "prefixlen": 24}]}]
        """
        mock_run.side_effect = [(0, sample, ""), (0, "[]", "")]
        result = network.list_interfaces()
        self.assertFalse(result["interfaces"][0]["effective_up"])

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value=None)
    def test_missing_ip_binary(self, mock_find, mock_classify, mock_dns):
        result = network.list_interfaces()
        self.assertFalse(result["available"])
        self.assertEqual(result["error_code"], "system.tool_missing")

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/sbin/ip")
    @mock.patch("nas_monitor.network.system_tools.run", return_value=(0, "not json", ""))
    def test_garbage_output_does_not_crash(self, mock_run, mock_find, mock_classify, mock_dns):
        result = network.list_interfaces()
        self.assertFalse(result["available"])
        self.assertIsNotNone(result.get("error_code"))


class TestDetectBackend(unittest.TestCase):
    @mock.patch("nas_monitor.network.system_tools.find_binary")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_detects_networkmanager_when_it_genuinely_manages_a_device(self, mock_run, mock_find):
        mock_find.side_effect = lambda name: f"/usr/bin/{name}" if name == "nmcli" else None
        mock_run.return_value = (0, "lo:unmanaged:loopback\nens18:connected:ethernet\n", "")
        self.assertEqual(network.detect_backend(), "networkmanager")

    @mock.patch("nas_monitor.network.system_tools.find_binary")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_networkmanager_running_but_not_managing_anything_falls_through(self, mock_run, mock_find):
        # Real report: NetworkManager's own service can be perfectly
        # active while genuinely managing nothing - Debian ships it with
        # ifupdown-defined interfaces deliberately left "unmanaged" by
        # default. Merely checking "is the service active" (the old
        # logic here) said "networkmanager" anyway, which then broke a
        # later step expecting a real NM connection profile to exist.
        mock_find.side_effect = lambda name: "/usr/bin/nmcli" if name == "nmcli" else None
        mock_run.return_value = (0, "lo:unmanaged:loopback\nens18:unmanaged:ethernet\n", "")
        self.assertNotEqual(network.detect_backend(), "networkmanager")

    @mock.patch("nas_monitor.network.system_tools.find_binary")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_detects_systemd_networkd_when_a_link_is_actually_configured(self, mock_run, mock_find):
        mock_find.side_effect = lambda name: "/usr/bin/networkctl" if name == "networkctl" else None
        mock_run.return_value = (0, "1 lo     loopback carrier     unmanaged\n2 ens18  ether    routable    configured\n", "")
        self.assertEqual(network.detect_backend(), "systemd-networkd")

    @mock.patch("nas_monitor.network.os.path.isfile", return_value=False)
    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value=None)
    def test_unknown_when_nothing_detected(self, mock_find, mock_isfile):
        self.assertEqual(network.detect_backend(), "unknown")

    @mock.patch("builtins.open", new_callable=mock.mock_open, read_data="auto lo\niface lo inet loopback\n\nauto eth0\niface eth0 inet dhcp\n")
    @mock.patch("nas_monitor.network.os.path.isfile", return_value=True)
    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value=None)
    def test_detects_ifupdown_from_real_interface_stanza(self, mock_find, mock_isfile, mock_open):
        self.assertEqual(network.detect_backend(), "ifupdown")

    @mock.patch("builtins.open", new_callable=mock.mock_open, read_data="auto lo\niface lo inet loopback\n")
    @mock.patch("nas_monitor.network.os.path.isfile", return_value=True)
    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value=None)
    def test_loopback_only_interfaces_file_is_not_ifupdown(self, mock_find, mock_isfile, mock_open):
        # a bare default /etc/network/interfaces (just loopback) doesn't mean
        # ifupdown is actually managing anything real
        self.assertEqual(network.detect_backend(), "unknown")


class TestInterfaceDnsServers(unittest.TestCase):
    """Per-connection DNS via nmcli - deliberately NOT /etc/resolv.conf,
    which reflects one merged system-wide resolver (whichever interface/
    VPN currently has top priority), not what's actually configured on a
    given connection. See network.py's docstring for the real-world case
    this was built for (Tailscale's MagicDNS masking a wired connection's
    real DNS servers)."""

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/bin/nmcli")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_parses_indexed_dns_fields(self, mock_run, mock_find):
        mock_run.return_value = (0, "IP4.DNS[1]:192.168.0.5\nIP4.DNS[2]:192.168.0.6\n", "")
        self.assertEqual(network._interface_dns_servers("eno1"), ["192.168.0.5", "192.168.0.6"])

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/bin/nmcli")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_no_dns_configured(self, mock_run, mock_find):
        mock_run.return_value = (0, "", "")
        self.assertEqual(network._interface_dns_servers("eno1"), [])

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value=None)
    def test_missing_nmcli_returns_empty_not_raises(self, mock_find):
        self.assertEqual(network._interface_dns_servers("eno1"), [])

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/bin/nmcli")
    @mock.patch("nas_monitor.network.system_tools.run", return_value=(1, "", "no such device"))
    def test_command_failure_returns_empty_not_raises(self, mock_run, mock_find):
        self.assertEqual(network._interface_dns_servers("eno1"), [])


class TestGetHostname(unittest.TestCase):
    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/bin/hostnamectl")
    @mock.patch("nas_monitor.network.system_tools.run", return_value=(0, "HP-G8\n", ""))
    def test_uses_hostnamectl_when_available(self, mock_run, mock_find):
        self.assertEqual(network.get_hostname(), "HP-G8")

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value=None)
    @mock.patch("nas_monitor.network.os.path.isfile", return_value=True)
    def test_falls_back_to_hostname_file(self, mock_isfile, mock_find):
        with mock.patch("builtins.open", mock.mock_open(read_data="fallback-host\n")):
            self.assertEqual(network.get_hostname(), "fallback-host")


if __name__ == "__main__":
    unittest.main()
