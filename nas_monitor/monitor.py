"""
nas_monitor.monitor
--------------------
Read-only helpers for disk enumeration, S.M.A.R.T. health, and mdadm/RAID
array status. Every public function is defensive: missing binaries,
permission errors, and unsupported devices are reported as a status field
in the returned dict instead of raising, so the web dashboard always has
something sane to render.

Nothing here writes to disk, mounts anything, or touches mdadm in a way
that changes state. This module is intentionally read-only.
"""

from __future__ import annotations

import json
import re
from typing import Any

from nas_monitor import system_tools


def _run(cmd: list[str]) -> tuple[int, str, str]:
    return system_tools.run(cmd)


def _find_binary(name: str) -> str | None:
    return system_tools.find_binary(name)


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


# --------------------------------------------------------------------------
# Block device enumeration
# --------------------------------------------------------------------------

def list_disks() -> list[dict[str, Any]]:
    """Enumerate physical disks via lsblk (whole disks only, no partitions)."""
    lsblk_path = _find_binary("lsblk")
    if lsblk_path is None:
        return []

    code, out, err = _run(
        [lsblk_path, "-d", "-b", "-J", "-o", "NAME,SIZE,MODEL,SERIAL,ROTA,TYPE,TRAN"]
    )
    if code != 0 or not out.strip():
        return []

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []

    disks = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        disks.append(
            {
                "name": dev.get("name"),
                "path": f"/dev/{dev.get('name')}",
                "size": _human_size(int(dev.get("size") or 0)),
                "model": (dev.get("model") or "").strip() or "unknown",
                "serial": (dev.get("serial") or "").strip() or "unknown",
                "rotational": dev.get("rota") in (True, "1", 1),
                "transport": dev.get("tran") or "unknown",
            }
        )
    return disks


# --------------------------------------------------------------------------
# S.M.A.R.T. health
# --------------------------------------------------------------------------

# attribute IDs we surface for SATA/SAS (ATA) disks - these are the ones
# that actually predict failure, not the full 30+ attribute dump
_KEY_ATA_ATTRS = {
    5: "reallocated_sectors",
    187: "reported_uncorrect",
    188: "command_timeout",
    197: "pending_sectors",
    198: "offline_uncorrectable",
    194: "temperature",
    9: "power_on_hours",
}


def get_smart_health(device_path: str) -> dict[str, Any]:
    """Return a normalized SMART health summary for one device.

    Works for both ATA/SATA/SAS disks (via -A -H -j) and NVMe (which reports
    health through a different JSON shape - handled separately below).
    """
    result: dict[str, Any] = {
        "device": device_path,
        "available": False,
        "passed": None,
        "temperature_c": None,
        "power_on_hours": None,
        "attributes": {},
        "error": None,
    }

    smartctl_path = _find_binary("smartctl")
    if smartctl_path is None:
        result["error"] = "smartctl not installed"
        return result

    code, out, err = _run([smartctl_path, "-a", "-j", device_path])
    # smartctl uses bit-flags in its exit code; bit 0/1 are usage errors,
    # higher bits can still be set on a device that's perfectly readable
    # (e.g. bit 6 = "SMART status not ok"), so don't treat code != 0 as fatal
    if not out.strip():
        result["error"] = err.strip() or f"no output (exit {code})"
        return result

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        result["error"] = "could not parse smartctl JSON output"
        return result

    result["available"] = True

    smart_status = data.get("smart_status", {})
    if "passed" in smart_status:
        result["passed"] = bool(smart_status["passed"])

    # --- NVMe path ---
    nvme_log = data.get("nvme_smart_health_information_log")
    if nvme_log:
        result["temperature_c"] = nvme_log.get("temperature")
        result["power_on_hours"] = nvme_log.get("power_on_hours")
        result["attributes"] = {
            "media_errors": nvme_log.get("media_errors"),
            "percentage_used": nvme_log.get("percentage_used"),
            "critical_warning": nvme_log.get("critical_warning"),
            "unsafe_shutdowns": nvme_log.get("unsafe_shutdowns"),
        }
        return result

    # --- ATA/SATA/SAS path ---
    table = (data.get("ata_smart_attributes") or {}).get("table", [])
    attrs = {}
    for entry in table:
        attr_id = entry.get("id")
        if attr_id in _KEY_ATA_ATTRS:
            key = _KEY_ATA_ATTRS[attr_id]
            raw = entry.get("raw", {}).get("value")
            attrs[key] = raw
    result["attributes"] = attrs
    result["temperature_c"] = attrs.get("temperature")
    result["power_on_hours"] = attrs.get("power_on_hours")

    # some drives report temperature via a dedicated top-level field instead
    if result["temperature_c"] is None:
        temp = data.get("temperature", {}).get("current")
        if temp is not None:
            result["temperature_c"] = temp

    return result


def classify_health(smart: dict[str, Any]) -> str:
    """Turn a get_smart_health() result into ok / warning / critical / unknown."""
    if not smart.get("available"):
        return "unknown"
    if smart.get("passed") is False:
        return "critical"

    attrs = smart.get("attributes") or {}
    realloc = attrs.get("reallocated_sectors") or 0
    pending = attrs.get("pending_sectors") or 0
    offline_unc = attrs.get("offline_uncorrectable") or 0
    media_errors = attrs.get("media_errors") or 0
    pct_used = attrs.get("percentage_used") or 0

    if pending or offline_unc or media_errors:
        return "critical"
    if realloc or pct_used >= 90:
        return "warning"
    return "ok"


# --------------------------------------------------------------------------
# RAID / mdadm status
# --------------------------------------------------------------------------

_MDSTAT_RESYNC_RE = re.compile(r"(resync|recovery|reshape|check)\s*=\s*([\d.]+)%")


def _parse_mdstat(mdstat_path: str = "/proc/mdstat") -> dict[str, dict[str, Any]]:
    """Parse /proc/mdstat for array names and per-array progress info."""
    try:
        with open(mdstat_path, "r") as fh:
            content = fh.read()
    except OSError:
        return {}

    arrays: dict[str, dict[str, Any]] = {}
    current = None
    for line in content.splitlines():
        m = re.match(r"^(md\d+)\s*:\s*(active|inactive)\s*(\(read-only\)\s*)?(\S+)?\s*(.*)$", line)
        if m:
            current = m.group(1)
            arrays[current] = {
                "active": m.group(2) == "active",
                "level": m.group(4) or "unknown",
                "members_raw": m.group(5).strip(),
                "progress_percent": None,
                "progress_action": None,
            }
            continue
        if current and (match := _MDSTAT_RESYNC_RE.search(line)):
            arrays[current]["progress_action"] = match.group(1)
            arrays[current]["progress_percent"] = float(match.group(2))
    return arrays


def get_raid_arrays() -> list[dict[str, Any]]:
    """List all mdadm arrays with state, level, member disks, and health."""
    mdstat = _parse_mdstat()
    if not mdstat:
        return []

    arrays = []
    mdadm_path = _find_binary("mdadm")

    for name, info in mdstat.items():
        entry: dict[str, Any] = {
            "name": name,
            "path": f"/dev/{name}",
            "level": info["level"],
            "active": info["active"],
            "progress_percent": info["progress_percent"],
            "progress_action": info["progress_action"],
            "array_state": None,
            "num_devices": None,
            "working_devices": None,
            "failed_devices": None,
            "devices": [],
            "health": "unknown",
            "error": None,
        }

        if mdadm_path is None:
            entry["error"] = "mdadm not installed"
            arrays.append(entry)
            continue

        code, out, err = _run([mdadm_path, "--detail", "--export", f"/dev/{name}"])
        if code != 0 or not out.strip():
            entry["error"] = err.strip() or f"mdadm exited {code}"
            arrays.append(entry)
            continue

        kv = {}
        for line in out.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k] = v

        entry["level"] = kv.get("MD_LEVEL", entry["level"])
        entry["array_state"] = kv.get("MD_ARRAY_STATE")
        entry["num_devices"] = kv.get("MD_DEVICES")
        entry["metadata"] = kv.get("MD_METADATA")

        devices = []
        i = 0
        while f"MD_DEVICE_dev{i}_DEV" in kv or f"MD_DEVICE_DEV_{i}" in kv:
            dev_path = kv.get(f"MD_DEVICE_dev{i}_DEV") or kv.get(f"MD_DEVICE_DEV_{i}")
            role = kv.get(f"MD_DEVICE_dev{i}_ROLE")
            devices.append({"device": dev_path, "role": role})
            i += 1
        entry["devices"] = devices

        state = (entry["array_state"] or "").lower()
        if "degraded" in state or "failed" in state:
            entry["health"] = "critical"
        elif entry["progress_percent"] is not None:
            entry["health"] = "warning"
        elif state in ("clean", "active"):
            entry["health"] = "ok"

        arrays.append(entry)

    return arrays


# --------------------------------------------------------------------------
# Top-level snapshot used by the web app
# --------------------------------------------------------------------------

def get_full_status() -> dict[str, Any]:
    disks = list_disks()
    for disk in disks:
        smart = get_smart_health(disk["path"])
        disk["smart"] = smart
        disk["health"] = classify_health(smart)

    raid = get_raid_arrays()

    return {
        "disks": disks,
        "raid": raid,
    }
