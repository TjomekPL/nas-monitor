"""
nas_monitor.network
---------------------
Network configuration - detection first (this module), mutation with a
mandatory auto-rollback safety net to follow separately. A bad network
change is the one mistake on this whole tool that can't be fixed by
trying again from the dashboard - the dashboard itself becomes
unreachable - so detection has to be rock solid and mutation has to
default to reverting itself.

Different Debian installs manage networking differently (NetworkManager
on a desktop like a KDE Plasma box, systemd-networkd or plain ifupdown on
a headless server/Pi) - this module detects which one is actually active
rather than assuming, the same way the rest of this project reads real
system state instead of guessing.
"""

from __future__ import annotations

import json
import os
from typing import Any

from nas_monitor import system_tools

RESOLV_CONF = "/etc/resolv.conf"
HOSTNAME_FILE = "/etc/hostname"


def get_hostname() -> str:
    hostnamectl_path = system_tools.find_binary("hostnamectl")
    if hostnamectl_path:
        code, out, err = system_tools.run([hostnamectl_path, "--static"])
        if code == 0 and out.strip():
            return out.strip()
    if os.path.isfile(HOSTNAME_FILE):
        try:
            with open(HOSTNAME_FILE, "r") as fh:
                content = fh.read().strip()
                if content:
                    return content
        except OSError:
            pass
    return os.uname().nodename


def detect_backend() -> str:
    """Which system actually manages network config here - matters for
    how a future "apply new settings" step would need to work, since
    each backend is configured completely differently."""
    systemctl_path = system_tools.find_binary("systemctl")
    if systemctl_path:
        code, out, _ = system_tools.run([systemctl_path, "is-active", "NetworkManager"])
        if code == 0 and out.strip() == "active":
            return "networkmanager"
        code, out, _ = system_tools.run([systemctl_path, "is-active", "systemd-networkd"])
        if code == 0 and out.strip() == "active":
            return "systemd-networkd"

    # ifupdown has no daemon to check - a non-trivial /etc/network/interfaces
    # (beyond just the default loopback stanza) is the closest real signal
    if os.path.isfile("/etc/network/interfaces"):
        try:
            with open("/etc/network/interfaces", "r") as fh:
                content = fh.read()
            non_lo_ifaces = [
                line for line in content.splitlines()
                if line.strip().startswith("iface") and "lo" not in line.split()[1:2]
            ]
            if non_lo_ifaces:
                return "ifupdown"
        except OSError:
            pass

    return "unknown"


def _prefixlen_to_netmask(prefixlen: int) -> str:
    mask = (0xFFFFFFFF << (32 - prefixlen)) & 0xFFFFFFFF
    return ".".join(str((mask >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def list_interfaces() -> dict[str, Any]:
    """Every network interface with its current state, straight from the
    kernel via `ip -j` - true regardless of which backend (if any) is
    managing it. Loopback is excluded, it's never something to configure."""
    result: dict[str, Any] = {"available": False, "interfaces": [], "error": None}

    ip_path = system_tools.find_binary("ip")
    if ip_path is None:
        result["error"] = "ip (iproute2) not installed"
        return result

    code, out, err = system_tools.run([ip_path, "-j", "addr", "show"])
    if code != 0 or not out.strip():
        result["error"] = err.strip() or f"ip exited {code}"
        return result

    try:
        raw_interfaces = json.loads(out)
    except json.JSONDecodeError:
        result["error"] = "could not parse ip addr output"
        return result

    gateway_by_dev = _default_gateways()

    interfaces = []
    for iface in raw_interfaces:
        name = iface.get("ifname", "")
        if name == "lo" or "LOOPBACK" in (iface.get("flags") or []):
            continue

        ipv4_addrs = []
        for addr in iface.get("addr_info", []):
            if addr.get("family") != "inet":
                continue
            prefixlen = addr.get("prefixlen", 32)
            ipv4_addrs.append(
                {
                    "address": addr.get("local"),
                    "prefixlen": prefixlen,
                    "netmask": _prefixlen_to_netmask(prefixlen),
                }
            )

        interfaces.append(
            {
                "name": name,
                "mac": iface.get("address"),
                "state": iface.get("operstate", "UNKNOWN").lower(),
                "addresses": ipv4_addrs,
                "gateway": gateway_by_dev.get(name),
            }
        )

    result["available"] = True
    result["interfaces"] = sorted(interfaces, key=lambda i: i["name"])
    return result


def _default_gateways() -> dict[str, str]:
    """{interface_name: gateway_ip} for every default route."""
    ip_path = system_tools.find_binary("ip")
    if ip_path is None:
        return {}
    code, out, err = system_tools.run([ip_path, "-j", "route", "show", "default"])
    if code != 0 or not out.strip():
        return {}
    try:
        routes = json.loads(out)
    except json.JSONDecodeError:
        return {}
    return {r["dev"]: r["gateway"] for r in routes if "dev" in r and "gateway" in r}


def get_dns_servers() -> list[str]:
    """Parse /etc/resolv.conf - works whether it's hand-written, written by
    a DHCP client, or a symlink to systemd-resolved's generated file."""
    if not os.path.isfile(RESOLV_CONF):
        return []
    servers = []
    try:
        with open(RESOLV_CONF, "r") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2:
                        servers.append(parts[1])
    except OSError:
        pass
    return servers


def get_status() -> dict[str, Any]:
    """Everything the Sieć tab needs in one call."""
    interfaces_result = list_interfaces()
    return {
        "hostname": get_hostname(),
        "backend": detect_backend(),
        "dns_servers": get_dns_servers(),
        "interfaces": interfaces_result.get("interfaces", []),
        "error": interfaces_result.get("error"),
    }
