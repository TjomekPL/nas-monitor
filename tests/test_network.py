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


class TestListInterfaces(unittest.TestCase):
    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/sbin/ip")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_excludes_loopback_includes_others(self, mock_run, mock_find):
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
    def test_converts_prefixlen_and_attaches_gateway(self, mock_run, mock_find):
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

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value=None)
    def test_missing_ip_binary(self, mock_find):
        result = network.list_interfaces()
        self.assertFalse(result["available"])
        self.assertIn("not installed", result["error"])

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/sbin/ip")
    @mock.patch("nas_monitor.network.system_tools.run", return_value=(0, "not json", ""))
    def test_garbage_output_does_not_crash(self, mock_run, mock_find):
        result = network.list_interfaces()
        self.assertFalse(result["available"])
        self.assertIsNotNone(result["error"])


class TestDetectBackend(unittest.TestCase):
    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/bin/systemctl")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_detects_networkmanager(self, mock_run, mock_find):
        mock_run.return_value = (0, "active\n", "")
        self.assertEqual(network.detect_backend(), "networkmanager")

    @mock.patch("nas_monitor.network.system_tools.find_binary", return_value="/usr/bin/systemctl")
    @mock.patch("nas_monitor.network.system_tools.run")
    def test_detects_systemd_networkd(self, mock_run, mock_find):
        # NetworkManager inactive, systemd-networkd active
        mock_run.side_effect = [(3, "inactive\n", ""), (0, "active\n", "")]
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


class TestGetDnsServers(unittest.TestCase):
    @mock.patch("nas_monitor.network.os.path.isfile", return_value=True)
    def test_parses_nameserver_lines(self, mock_isfile):
        content = "nameserver 8.8.8.8\nnameserver 1.1.1.1\nsearch example.com\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=content)):
            self.assertEqual(network.get_dns_servers(), ["8.8.8.8", "1.1.1.1"])

    @mock.patch("nas_monitor.network.os.path.isfile", return_value=False)
    def test_missing_resolv_conf(self, mock_isfile):
        self.assertEqual(network.get_dns_servers(), [])


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
