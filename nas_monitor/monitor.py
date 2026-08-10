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
    """IEC binary units (KiB/MiB/GiB/TiB - factor of 1024), not decimal SI
    units (KB/MB/GB - factor of 1000). The arithmetic here was always
    base-1024; the old KB/MB/GB labels were simply the wrong symbol for
    what was actually being computed, not a deliberate choice."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


def get_filesystem_usage(device_path: str) -> dict[str, Any]:
    """Combined usage across every "real" mounted filesystem under this
    device - via lsblk's FS* columns (not a separate `df` call), which
    works exactly the same way whether device_path is a raw disk
    (/dev/sda) or a RAID array (/dev/md0): one shared data source for
    the usage bar wherever it's shown.

    A single physical disk very often has multiple partitions - an EFI
    boot partition, swap, an OS root filesystem, maybe a separate data
    partition - and taking just the first mounted one found could as
    easily land on a nearly-empty ~1 GiB boot partition as on the actual
    data (confirmed against a real report: a disk with an EFI/btrfs
    root/swap/ext4-data layout showed "9 MiB of 974 MiB" - the EFI
    partition - instead of anything resembling the real ~490 GiB of
    actual filesystems). EFI/boot (vfat) and swap are excluded outright
    as never being "data" in a meaningful sense; everything else mounted
    under this device is summed into one combined total/used/available,
    on the reasoning that all of it together is "how full is this disk".

    mounted=False (nothing else populated) covers both "nothing here
    qualifies" and "lsblk/the device isn't available" - the caller
    doesn't need to distinguish those, there's just no usage bar to show
    either way."""
    result: dict[str, Any] = {
        "mounted": False,
        "mountpoints": [],
        "total_bytes": None,
        "used_bytes": None,
        "available_bytes": None,
    }

    lsblk_path = _find_binary("lsblk")
    if lsblk_path is None:
        return result

    code, out, err = _run(
        [lsblk_path, "-b", "-J", "-o", "NAME,MOUNTPOINT,FSTYPE,FSSIZE,FSAVAIL,FSUSED", device_path]
    )
    if code != 0 or not out.strip():
        return result

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return result

    _excluded_fstypes = {"swap", "vfat"}

    def _collect_real_filesystems(devices, found):
        for dev in devices:
            mountpoint = dev.get("mountpoint")
            fstype = (dev.get("fstype") or "").lower()
            if mountpoint and mountpoint != "[SWAP]" and dev.get("fssize") and fstype not in _excluded_fstypes:
                found.append(dev)
            _collect_real_filesystems(dev.get("children") or [], found)
        return found

    matches = _collect_real_filesystems(data.get("blockdevices", []), [])
    if not matches:
        return result

    total = sum(int(m["fssize"]) for m in matches)
    avail = sum(int(m.get("fsavail") or 0) for m in matches)
    if all(m.get("fsused") is not None for m in matches):
        used = sum(int(m["fsused"]) for m in matches)
    else:
        used = max(total - avail, 0)

    result["mounted"] = True
    result["mountpoints"] = [m.get("mountpoint") for m in matches]
    result["total_bytes"] = total
    result["used_bytes"] = used
    result["available_bytes"] = avail
    return result


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
            if key == "temperature" and isinstance(raw, int):
                # Attribute 194's raw value isn't always just the
                # temperature - some drives (this shows up a lot on
                # USB-SATA bridge chips) pack current/min/max history
                # into the same 48-bit field, e.g. 0x2100210021 for a
                # drive sitting at 33°C with a 33/33 min/max history.
                # Only the lowest byte is ever the *current* reading;
                # taking the full raw integer as-is produced nonsense
                # like "141736083489°C" on a real USB drive. Real-world
                # temperatures always fit in a single byte, so masking
                # to the low 8 bits recovers the current value in both
                # the packed and unpacked (plain single-byte) cases.
                raw = raw & 0xFF
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


_MD_LEVEL_NAMES = {"linear", "raid0", "raid1", "raid4", "raid5", "raid6", "raid10", "multipath", "faulty"}
_MDSTAT_MEMBER_RE = re.compile(r"^([a-zA-Z0-9]+)\[(\d+)\](\(([SF])\))?$")


def _parse_mdstat_members(members_raw: str) -> list[dict[str, Any]]:
    """Parses mdstat's own raw member string ("sdb1[1] sdc1[2](F)
    sde1[4](S)") into the same {"device", "role"} shape mdadm --export
    normally provides - the fallback used when mdadm itself isn't
    installed (see get_raid_arrays). [N] is the device's RAID slot
    number (a genuinely active member); a trailing (F) marks it
    faulty, (S) marks it a spare - neither actually contributing to
    the array's redundancy right now, matching how a mdadm-export
    "faulty spare" role is already treated as not-working elsewhere in
    this module."""
    members = []
    for token in members_raw.split():
        m = _MDSTAT_MEMBER_RE.match(token)
        if not m:
            continue
        name, slot, _, flag = m.groups()
        role = "faulty" if flag == "F" else ("spare" if flag == "S" else slot)
        members.append({"device": f"/dev/{name}", "role": role})
    return members


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
        m = re.match(r"^(md\d+)\s*:\s*(active|inactive)\s*(\(read-only\)\s*)?(.*)$", line)
        if m:
            current = m.group(1)
            # An inactive array with too few members to even start
            # (e.g. "md0 : inactive sda1[0](S)") has no level token at
            # all - the device list starts immediately after
            # active/inactive. Only treat the first word as the level
            # if it's actually one of mdadm's known personality names;
            # otherwise it's the first device, and swallowing it as a
            # fake "level" (the previous behavior) both gave a wrong
            # level and silently dropped that device from members_raw.
            rest = m.group(4).strip()
            tokens = rest.split(None, 1)
            if tokens and tokens[0] in _MD_LEVEL_NAMES:
                level = tokens[0]
                members_raw = tokens[1] if len(tokens) > 1 else ""
            else:
                level = "unknown"
                members_raw = rest
            arrays[current] = {
                "active": m.group(2) == "active",
                "level": level,
                "members_raw": members_raw,
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
            # mdadm itself is what normally supplies the structured
            # device list (below) - without it, /proc/mdstat's own raw
            # member string ("sdb1[1] sdc1[2](F)") is the only thing
            # available, so it's parsed as a fallback rather than
            # leaving devices empty and unable to answer "which disks
            # does this contain" at all just because mdadm isn't on
            # this system.
            entry["error"] = "mdadm not installed"
            entry["devices"] = _parse_mdstat_members(info["members_raw"])
            entry["working_devices"] = sum(1 for d in entry["devices"] if d.get("role") and d["role"].isdigit())
            entry["failed_devices"] = len(entry["devices"]) - entry["working_devices"]
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

        # Real mdadm --detail --export keys each member by its device
        # NAME, not a positional index - MD_DEVICE_dev_sdb_DEV /
        # MD_DEVICE_dev_sdb_ROLE, one pair per member, in whatever
        # order mdadm happens to emit them (not sorted, not
        # necessarily role order). The previous version of this parser
        # assumed a sequential MD_DEVICE_dev0_DEV/dev1_DEV.../devN_DEV
        # numbering that real mdadm never actually produces - which
        # meant `while f"MD_DEVICE_dev{i}_DEV" in kv"` never matched
        # anything past i=0, silently leaving `devices` empty on every
        # real system. That, in turn, made working_devices=0 look like
        # every member was missing (device_shortfall) - a healthy,
        # fully-populated RAID0 showed as "warning", its member disks
        # weren't recognized as members anywhere else in the app
        # (wrong buttons in the management table, not excluded from
        # the Podsumowanie disk cards), and its card showed "(0
        # disks)" even though MD_DEVICES itself (a plain top-level key,
        # unaffected by this) correctly said 5. Scanning for the
        # MD_DEVICE_<id>_DEV / MD_DEVICE_<id>_ROLE pattern generically
        # - whatever <id> actually is - fixes all of those at once.
        device_ids = []
        seen_ids = set()
        for k in kv:
            m = re.match(r"^MD_DEVICE_(.+)_DEV$", k)
            if m and m.group(1) not in seen_ids:
                seen_ids.add(m.group(1))
                device_ids.append(m.group(1))
        devices = []
        for dev_id in device_ids:
            dev_path = kv.get(f"MD_DEVICE_{dev_id}_DEV")
            role = kv.get(f"MD_DEVICE_{dev_id}_ROLE")
            if dev_path:
                devices.append({"device": dev_path, "role": role})
        entry["devices"] = devices
        # A genuinely present, working member has both a real device
        # path AND a numeric role (an active RAID slot) - a "spare" or
        # "faulty" role, or an empty/missing device path, means that
        # slot isn't actually contributing to array redundancy right
        # now, whatever the array's own summary state says about it.
        entry["working_devices"] = sum(
            1 for d in devices if d.get("device") and (d.get("role") or "").isdigit()
        )
        entry["failed_devices"] = len(devices) - entry["working_devices"]
        try:
            entry["expected_devices"] = int(entry["num_devices"]) if entry["num_devices"] else None
        except ValueError:
            entry["expected_devices"] = None

        state = (entry["array_state"] or "").lower()
        # Real report: an array missing a disk (3 of 4 connected) still
        # showed healthy - a single string-match against MD_ARRAY_STATE
        # (mdadm's --export summary field) isn't reliable enough on its
        # own to catch every case a degraded array can present as, so
        # this also independently compares how many members are
        # actually working against how many the array expects,
        # regardless of what the state string itself says.
        device_shortfall = (
            entry["expected_devices"] is not None
            and entry["working_devices"] < entry["expected_devices"]
        )
        is_degraded = "degraded" in state or "failed" in state or device_shortfall
        if is_degraded and not entry["active"]:
            # Genuinely non-functional - too many members missing for
            # the array to operate at all (mdstat itself reports it
            # inactive), not just running without full redundancy.
            entry["health"] = "critical"
        elif is_degraded:
            # His explicit correction to the first version of this fix:
            # a degraded-but-still-active array (e.g. one disk out of a
            # RAID5) is still working, reads/writes still succeed, it's
            # just running without its normal redundancy - that's a
            # "come deal with this soon" state, not "this is broken
            # right now", so it gets the same warning tier as anything
            # else in this app that's degraded-but-functional (a high
            # disk temperature, say), not the same red as something
            # actually failed.
            entry["health"] = "warning"
        elif entry["progress_percent"] is not None:
            entry["health"] = "warning"
        elif state in ("clean", "active"):
            entry["health"] = "ok"
        elif not state and entry["active"]:
            # RAID0/linear genuinely never emit MD_ARRAY_STATE at all -
            # confirmed against real mdadm output, not a guess: per
            # mdadm's own docs, that field only carries meaningful
            # information for the redundant levels (1/4/5/6/10), since
            # RAID0/linear can't be "degraded" (missing a member fails
            # the whole array, not a partial state to report). Without
            # this, a perfectly healthy RAID0 - not degraded, no
            # shortfall, mdstat itself says active - fell through every
            # branch above with nothing left to match and stayed at the
            # unhelpful "unknown" default.
            entry["health"] = "ok"

        arrays.append(entry)

    return arrays


# --------------------------------------------------------------------------
# Top-level snapshot used by the web app
# --------------------------------------------------------------------------

def _disk_name_from_device(device_or_partition: str) -> str:
    """"/dev/sda1" -> "sda", "/dev/nvme0n1p1" -> "nvme0n1", "/dev/mmcblk0p1"
    -> "mmcblk0" (SD cards - relevant on a Raspberry Pi), "/dev/md0" ->
    "md0" (RAID arrays - same fix as disk_mutate._disk_name's, kept
    consistent here too even though this copy is currently only ever
    called on array MEMBER paths, not an array's own path). Deliberately
    duplicated from disk_mutate._disk_name rather than imported - this
    module stays read-only and dependency-free of the mutation module
    on principle, and the parsing itself is a handful of lines."""
    base = device_or_partition.rsplit("/", 1)[-1]
    if re.match(r"^(nvme\d+n\d+|mmcblk\d+|md\d+)$", base):
        return base
    m = re.match(r"^(nvme\d+n\d+|mmcblk\d+|md\d+)p\d+$", base)
    if m:
        return m.group(1)
    m = re.match(r"^([a-zA-Z]+)\d+$", base)
    if m:
        return m.group(1)
    return base


def _boot_disk_name() -> str | None:
    """Whole-disk name (e.g. "sda") backing the root filesystem -
    resolved via findmnt + lsblk's PKNAME. Duplicated from
    disk_mutate._boot_disk_name for the same reason as
    _disk_name_from_device above - used here only to flag
    is_boot_disk on the disk cards, so the frontend can hide actions
    (like Unmount) that could never apply to the running system's own
    disk, rather than showing a button that would just come back as a
    rejection."""
    findmnt_path = _find_binary("findmnt")
    lsblk_path = _find_binary("lsblk")
    if findmnt_path is None or lsblk_path is None:
        return None

    code, out, _ = _run([findmnt_path, "-no", "SOURCE", "/"])
    if code != 0 or not out.strip():
        return None
    source = out.strip().splitlines()[0]

    code, out, _ = _run([lsblk_path, "-no", "PKNAME", source])
    parent = out.strip().splitlines()[0] if code == 0 and out.strip() else ""
    return parent if parent else _disk_name_from_device(source)


def get_full_status() -> dict[str, Any]:
    disks = list_disks()
    raid = get_raid_arrays()
    for arr in raid:
        try:
            arr["usage"] = get_filesystem_usage(arr["path"])
        except Exception as exc:
            # One array's usage lookup failing must never blank out
            # every other array and disk in the response - see the
            # matching comment on the disk loop below for why this
            # matters more than it looks like it should.
            arr["usage"] = {"mounted": False, "mountpoints": [], "total_bytes": None, "used_bytes": None, "available_bytes": None}
            arr["error"] = arr.get("error") or f"usage lookup failed: {exc}"

    raid_member_names = {
        _disk_name_from_device(dev["device"])
        for arr in raid
        for dev in arr.get("devices", [])
        if dev.get("device")
    }
    boot_disk = _boot_disk_name()

    # A disk shows here only once it means something: it has a real,
    # mounted filesystem, or it's part of a RAID array (the array's own
    # card already represents it - see nas-monitor's Arrays section).
    # Anything else - blank, unrecognized filesystem, unmounted - is a
    # "raw disk" and lives in the separate raw-disks table instead
    # (disk_mutate.list_raw_disks), where formatting/wiping actually
    # happens. Skipping smartctl for those here too - no point paying
    # for it on a disk this view won't show.
    #
    # Each disk is handled inside its own try/except - lsblk/smartctl
    # dealing with a disk in a genuinely weird transitional state (mid
    # format, just unplugged, etc.) failing in some way this code
    # didn't anticipate must never take the whole endpoint down with
    # it. Before this, one bad disk meant get_full_status() raised,
    # /api/status came back as a 500, and the frontend showed nothing
    # at all for RAID *and* every other, perfectly fine disk too - a
    # single misbehaving disk hid the entire tab.
    visible_disks = []
    for disk in disks:
        if disk["name"] in raid_member_names:
            # The array's own card already represents this disk's
            # storage - showing it again here too was a real report
            # (his explicit ask): several individually-identical-
            # looking cards for one array's members is confusing, not
            # informative. This was previously backwards - the
            # condition below used to KEEP a raid member visible even
            # while unmounted (bypassing the "must be mounted" check
            # specifically for members), the opposite of what this
            # function's own comment already said should happen.
            continue
        try:
            usage = get_filesystem_usage(disk["path"])
            if not usage["mounted"]:
                continue
            disk["usage"] = usage
            disk["is_boot_disk"] = disk["name"] == boot_disk
            smart = get_smart_health(disk["path"])
            disk["smart"] = smart
            disk["health"] = classify_health(smart)
            visible_disks.append(disk)
        except Exception as exc:
            disk["usage"] = {"mounted": False, "mountpoints": [], "total_bytes": None, "used_bytes": None, "available_bytes": None}
            disk["is_boot_disk"] = disk["name"] == boot_disk
            disk["smart"] = {"available": False, "error": str(exc)}
            disk["health"] = "unknown"
            visible_disks.append(disk)

    return {
        "disks": visible_disks,
        "raid": raid,
    }
