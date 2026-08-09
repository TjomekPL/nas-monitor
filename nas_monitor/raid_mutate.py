"""
nas_monitor.raid_mutate
--------------------------
Mutating RAID array operations - the write counterpart to
monitor.get_raid_arrays() (read-only detection/health). Deliberately
narrow for now: only CREATING a new array from disks that are
genuinely free. Formatting the resulting array and mounting it reuses
disk_mutate's own format_disk()/mount_disk() unchanged - an mdadm array
device (/dev/mdX) is just another block device to lsblk/mkfs/mount, so
there's no reason to duplicate that machinery here.

Returns as soon as mdadm accepts the command - the actual initial sync
runs in the background at the kernel level and is already tracked the
same way an ordinary resync is (monitor.get_raid_arrays()'s
progress_percent/progress_action, polled by the existing UI). No
separate async job system needed for this: mdadm --create itself only
takes a few seconds, it's the sync afterward that's long, and that part
was already being watched before this feature existed.
"""

from __future__ import annotations

from typing import Any

from nas_monitor import system_tools, errors, monitor, disk_mutate

# Minimum device count each level needs to be created at all. mdadm
# itself enforces this, but with inconsistent, sometimes-cryptic error
# text per level ("level 6 needs at least 4 devices" vs a generic
# failure depending on version) - validating here first gives one
# clear, uniform error instead, before ever invoking mdadm.
#
# RAID3 deliberately absent - not supported by mdadm at all (confirmed
# via research earlier in this project, see disk_mutate/monitor notes).
MIN_DEVICES_FOR_LEVEL = {
    "0": 2,
    "1": 2,
    "4": 3,
    "5": 3,
    "6": 4,
    "10": 4,
}


def _next_array_name() -> str:
    """First /dev/mdN not already in use, starting from md0."""
    existing = {arr["name"] for arr in monitor.get_raid_arrays()}
    i = 0
    while f"md{i}" in existing:
        i += 1
    return f"md{i}"


def create_raid_array(devices: list[str], level: str) -> dict[str, Any]:
    """devices are whole-disk paths (e.g. "/dev/sdb") - unlike
    format_disk(), which always partitions a single disk, a RAID
    member conventionally uses the whole device directly (a common,
    supported mdadm pattern, and simpler than a partition-per-member
    step that adds nothing here). Every device must be a genuinely
    free, manageable disk - not the boot disk, not already a RAID
    member, not carrying a filesystem already (that needs an explicit
    Wipe first, same as any other destructive disk operation in this
    tool - creating an array is exactly that: destructive)."""
    result: dict[str, Any] = {"success": False}

    if level not in MIN_DEVICES_FOR_LEVEL:
        return errors.fail(result, "raid.unsupported_level", level=level)

    min_needed = MIN_DEVICES_FOR_LEVEL[level]
    if len(devices) < min_needed:
        return errors.fail(result, "raid.not_enough_devices", level=level, needed=min_needed, given=len(devices))
    if len(set(devices)) != len(devices):
        return errors.fail(result, "raid.duplicate_device")

    known = {d["path"]: d for d in disk_mutate.list_manageable_disks()}
    for device in devices:
        disk = known.get(device)
        if disk is None:
            return errors.fail(result, "raid.unknown_device", device=device)
        if disk.get("fstype") or disk.get("mounted") or disk.get("is_raid_member"):
            return errors.fail(result, "raid.device_not_free", device=device)

    mdadm_path = system_tools.find_binary("mdadm")
    if mdadm_path is None:
        return errors.tool_missing(result, "mdadm")

    array_name = _next_array_name()
    cmd = [
        mdadm_path, "--create", f"/dev/{array_name}",
        f"--level={level}", f"--raid-devices={len(devices)}",
        "--metadata=1.2", "--run",
    ] + devices
    code, out, err = system_tools.run(cmd, timeout=60)
    if code != 0:
        return errors.command_failed(result, err, out, code, "mdadm")

    result["success"] = True
    result["name"] = array_name
    result["path"] = f"/dev/{array_name}"
    return result
