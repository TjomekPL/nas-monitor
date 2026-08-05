"""
nas_monitor.network_mutate
-----------------------------
Changing IP/gateway/DNS - the one mutation in this whole tool that can
disconnect the admin from the dashboard itself if it goes wrong. Every
change is therefore snapshot-then-apply-then-auto-revert: unless
explicitly confirmed within REVERT_GRACE_SECONDS, the previous config is
restored automatically. NetworkManager only (via nmcli) - see
network.detect_backend(); systemd-networkd/ifupdown are out of scope for
now (each would need a completely different apply/restore mechanism).

Why this needs state_store (not an in-memory variable): nas-monitor runs
under gunicorn with multiple worker PROCESSES (see nas-monitor.service),
so the request that applies a change and the request that later confirms
it can land on two different workers with separate memory. The pending
change - including which token is valid and what to roll back to - is
persisted to disk so any worker can read/confirm it. The 30s timer
itself still lives in-process (threading.Timer, started by whichever
worker handled the apply request) - what makes this safe despite that is
that the timer's callback ALWAYS re-reads the persisted state before
acting, rather than trusting its own in-memory closure. If confirm()
already marked the change confirmed (from either worker), the timer sees
that on disk and does nothing. If the whole service restarts mid-window
(losing the timer entirely), check_and_recover_on_startup() re-arms or
immediately reverts based on how much of the grace period is left -
called once at app startup.
"""

from __future__ import annotations

import ipaddress
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from nas_monitor import system_tools, state_store, errors, network

PENDING_CHANGE_FILE = "network-pending-change.json"
REVERT_GRACE_SECONDS = 30

_active_timer: threading.Timer | None = None


def is_valid_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except (ValueError, TypeError):
        return False


def is_valid_prefixlen(value: Any) -> bool:
    try:
        return 0 <= int(value) <= 32
    except (TypeError, ValueError):
        return False


def gateway_in_subnet(ip: str, prefixlen: int, gateway: str) -> bool:
    try:
        subnet = ipaddress.ip_interface(f"{ip}/{prefixlen}").network
        return ipaddress.IPv4Address(gateway) in subnet
    except (ValueError, TypeError):
        return False


def _connection_for_interface(iface: str) -> str | None:
    nmcli_path = system_tools.find_binary("nmcli")
    if nmcli_path is None:
        return None
    code, out, _ = system_tools.run([nmcli_path, "-t", "-f", "DEVICE,CONNECTION", "device", "status"])
    if code != 0:
        return None
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[0] == iface and parts[1]:
            return parts[1]
    return None


_SNAPSHOT_FIELDS = ["ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns"]


def _snapshot_ipv4(connection: str) -> dict[str, str] | None:
    """Current values for exactly the fields we're about to change, in
    nmcli's own `key:value` syntax - which means they can be fed straight
    back into `connection modify` unmodified to restore this connection
    to precisely its current state."""
    nmcli_path = system_tools.find_binary("nmcli")
    if nmcli_path is None:
        return None
    code, out, _ = system_tools.run([nmcli_path, "-t", "-f", ",".join(_SNAPSHOT_FIELDS), "connection", "show", connection])
    if code != 0:
        return None
    values: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            if key in _SNAPSHOT_FIELDS:
                values[key] = value
    return values if len(values) == len(_SNAPSHOT_FIELDS) else None


def _set_ipv4(connection: str, values: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {"success": False}
    nmcli_path = system_tools.find_binary("nmcli")
    if nmcli_path is None:
        return errors.tool_missing(result, "nmcli")

    cmd = [nmcli_path, "connection", "modify", connection]
    for key in _SNAPSHOT_FIELDS:
        cmd += [key, values.get(key, "")]
    code, out, err = system_tools.run(cmd)
    if code != 0:
        return errors.command_failed(result, err, out, code, "nmcli")

    code, out, err = system_tools.run([nmcli_path, "connection", "up", connection])
    if code != 0:
        return errors.command_failed(result, err, out, code, "nmcli")

    result["success"] = True
    return result


def get_pending_change() -> dict[str, Any] | None:
    data = state_store.load(PENDING_CHANGE_FILE, default=None)
    return data or None  # treat {} the same as "cleared"


def _clear_pending_change() -> None:
    state_store.save(PENDING_CHANGE_FILE, {})


def _revert_if_still_pending(token: str) -> None:
    """Timer callback - fires ~REVERT_GRACE_SECONDS after an unconfirmed
    apply, in whichever worker process started it. Always re-reads the
    persisted state first; see module docstring for why that's what
    makes this safe under multiple gunicorn workers."""
    pending = get_pending_change()
    if not pending or pending.get("token") != token or pending.get("confirmed"):
        return  # already confirmed (by this worker or another), or cleared
    _set_ipv4(pending["connection"], pending["snapshot"])
    _clear_pending_change()


def request_ip_change(
    interface: str, ip: str, prefixlen: int, gateway: str, dns: list[str]
) -> dict[str, Any]:
    global _active_timer
    result: dict[str, Any] = {"success": False}

    if network.detect_backend() != "networkmanager":
        return errors.fail(result, "network.backend_unsupported")
    if get_pending_change():
        return errors.fail(result, "network.change_already_pending")
    if not is_valid_ipv4(ip):
        return errors.fail(result, "network.invalid_ip")
    if not is_valid_prefixlen(prefixlen):
        return errors.fail(result, "network.invalid_prefix")
    if not is_valid_ipv4(gateway):
        return errors.fail(result, "network.invalid_gateway")
    dns = [d.strip() for d in dns if d.strip()]
    for d in dns:
        if not is_valid_ipv4(d):
            return errors.fail(result, "network.invalid_dns", value=d)
    if not gateway_in_subnet(ip, prefixlen, gateway):
        return errors.fail(result, "network.gateway_outside_subnet")

    connection = _connection_for_interface(interface)
    if connection is None:
        return errors.fail(result, "network.connection_not_found", interface=interface)

    snapshot = _snapshot_ipv4(connection)
    if snapshot is None:
        return errors.fail(result, "network.snapshot_failed")

    token = uuid.uuid4().hex
    state_store.save(
        PENDING_CHANGE_FILE,
        {
            "token": token,
            "interface": interface,
            "connection": connection,
            "snapshot": snapshot,
            "confirmed": False,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )

    new_values = {
        "ipv4.method": "manual",
        "ipv4.addresses": f"{ip}/{prefixlen}",
        "ipv4.gateway": gateway,
        "ipv4.dns": ",".join(dns),
    }
    apply_result = _set_ipv4(connection, new_values)
    if not apply_result["success"]:
        _clear_pending_change()
        return errors.propagate(result, apply_result, interface=interface)

    _active_timer = threading.Timer(REVERT_GRACE_SECONDS, _revert_if_still_pending, args=[token])
    _active_timer.daemon = True
    _active_timer.start()

    result["success"] = True
    result["token"] = token
    result["expires_in"] = REVERT_GRACE_SECONDS
    result["new_host"] = f"{ip}:8420"
    return result


def confirm_change(token: str) -> dict[str, Any]:
    result: dict[str, Any] = {"success": False}
    pending = get_pending_change()
    if not pending or pending.get("token") != token:
        return errors.fail(result, "network.change_not_found")
    interface = pending.get("interface", "")
    _clear_pending_change()
    result["success"] = True
    result["interface"] = interface
    return result


def check_and_recover_on_startup() -> None:
    """Called once when the app starts. If the service was restarted (or
    a worker recycled) while a change was still pending and unconfirmed,
    the in-memory threading.Timer that would have reverted it is gone -
    this re-arms a fresh timer for whatever's left of the grace period,
    or reverts immediately if that period has already elapsed."""
    global _active_timer
    pending = get_pending_change()
    if not pending or pending.get("confirmed"):
        return
    try:
        created = datetime.fromisoformat(pending["created_at"])
    except (KeyError, ValueError):
        _revert_if_still_pending(pending.get("token", ""))
        return
    elapsed = (datetime.now(timezone.utc) - created).total_seconds()
    remaining = REVERT_GRACE_SECONDS - elapsed
    if remaining <= 0:
        _revert_if_still_pending(pending["token"])
    else:
        _active_timer = threading.Timer(remaining, _revert_if_still_pending, args=[pending["token"]])
        _active_timer.daemon = True
        _active_timer.start()
