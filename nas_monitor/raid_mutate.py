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

from nas_monitor import system_tools, errors, monitor, disk_mutate, disk_labels

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

# RAID0 and linear have no redundancy at all - mdadm's own docs are
# explicit about this: "RAID0 or Linear never have missing, spare, or
# failed drives, so there is nothing to monitor." A live member can
# never be hot-removed from either (the kernel refuses with "Device or
# resource busy" - there's no degraded state to fall back to, only
# "every member present" or "the whole array is gone"), and there's
# nothing a Repair/add-a-replacement-disk action could mean either.
# Both buttons are hidden client-side for these levels (see
# dashboard.js), but this is the actual, permanent backend guard - the
# real answer for someone swapping a disk here is deleting the whole
# array (see delete_raid_array) and recreating it with the new set.
NON_REDUNDANT_LEVELS = {"raid0", "linear"}


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
    tool - creating an array is exactly that: destructive).

    A device may also be another, already-existing RAID array's own
    path (e.g. "/dev/md0") - nested RAID (his real scenario: mirroring
    two existing RAID0 arrays into a RAID1 on top) is a genuinely
    supported mdadm pattern, mdadm doesn't care whether a --create
    argument is a physical disk or another md device, and the same
    free-device checks below (no fstype, not mounted, not already a
    member of something else) apply to an array exactly the way they
    apply to a disk."""
    result: dict[str, Any] = {"success": False}

    if level not in MIN_DEVICES_FOR_LEVEL:
        return errors.fail(result, "raid.unsupported_level", level=level)

    min_needed = MIN_DEVICES_FOR_LEVEL[level]
    if len(devices) < min_needed:
        return errors.fail(result, "raid.not_enough_devices", level=level, needed=min_needed, given=len(devices))
    if len(set(devices)) != len(devices):
        return errors.fail(result, "raid.duplicate_device")

    known = {d["path"]: d for d in disk_mutate.list_manageable_disks() + disk_mutate.list_manageable_raid_arrays()}
    for device in devices:
        disk = known.get(device)
        if disk is None:
            return errors.fail(result, "raid.unknown_device", device=device)
        if disk.get("fstype") or disk.get("mounted") or disk.get("is_raid_member"):
            return errors.fail(result, "raid.device_not_free", device=device)
        # Striping (RAID0) over a device that is ITSELF already a
        # non-redundant array (RAID0 or linear) - stripe-of-a-stripe -
        # is mathematically identical to one flat RAID0 across all the
        # underlying disks directly, with zero benefit and one more
        # layer to manage and lose sleep over. His explicit reaction on
        # noticing the picker allowed this. Striping over a REDUNDANT
        # array (RAID0 over two RAID1 mirrors - real "1+0"/"10-style"
        # topology) is a completely different, genuinely useful case
        # and stays allowed - this only rejects the pointless one.
        if level == "0" and disk.get("transport") == "raid" and (disk.get("level") or "").lower() in NON_REDUNDANT_LEVELS:
            return errors.fail(result, "raid.pointless_stripe_of_stripe", device=device)

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


def detach_member(array_name: str, device: str) -> dict[str, Any]:
    """Removes a disk from a RAID array - mdadm requires a member to be
    marked failed before it can be removed, so this does both in
    sequence (his explicit want: a per-member "Detach" action, for
    deliberately pulling a disk rather than waiting for it to fail on
    its own - e.g. before physically swapping it out). The array
    itself keeps running afterward on its remaining members, degraded
    by one - this never touches the array as a whole, only this one
    member's participation in it. --fail is allowed to no-op (a
    disk that's already gone/marked failed has nothing left to fail);
    only --remove actually failing is treated as an error."""
    result: dict[str, Any] = {"success": False}

    arrays = {arr["name"]: arr for arr in monitor.get_raid_arrays()}
    arr = arrays.get(array_name)
    if arr is not None and (arr.get("level") or "").lower() in NON_REDUNDANT_LEVELS:
        return errors.fail(result, "raid.no_redundancy", level=arr.get("level"))

    mdadm_path = system_tools.find_binary("mdadm")
    if mdadm_path is None:
        return errors.tool_missing(result, "mdadm")

    array_path = f"/dev/{array_name}"
    system_tools.run([mdadm_path, "--manage", array_path, "--fail", device], timeout=30)
    code, out, err = system_tools.run([mdadm_path, "--manage", array_path, "--remove", device], timeout=30)
    if code != 0:
        return errors.command_failed(result, err, out, code, "mdadm")

    result["success"] = True
    return result


def add_member(array_name: str, device: str) -> dict[str, Any]:
    """Adds a disk to an existing array - the repair/replace flow (his
    real scenario: an array missing a disk, wanting to add a new one
    back in). mdadm automatically starts rebuilding onto it if the
    array is currently degraded; if the array's already at full
    strength, it's added as a spare instead. The device must be
    genuinely free - the same checks create_raid_array uses, since
    adding a disk that still has real data on it would destroy that
    data without any of that function's up-front confirmation."""
    result: dict[str, Any] = {"success": False}

    arrays = {arr["name"]: arr for arr in monitor.get_raid_arrays()}
    arr = arrays.get(array_name)
    if arr is not None and (arr.get("level") or "").lower() in NON_REDUNDANT_LEVELS:
        return errors.fail(result, "raid.no_redundancy", level=arr.get("level"))

    known = {d["path"]: d for d in disk_mutate.list_manageable_disks()}
    disk = known.get(device)
    if disk is None:
        return errors.fail(result, "raid.unknown_device", device=device)
    if disk.get("fstype") or disk.get("mounted") or disk.get("is_raid_member"):
        return errors.fail(result, "raid.device_not_free", device=device)

    mdadm_path = system_tools.find_binary("mdadm")
    if mdadm_path is None:
        return errors.tool_missing(result, "mdadm")

    array_path = f"/dev/{array_name}"
    code, out, err = system_tools.run([mdadm_path, "--manage", array_path, "--add", device], timeout=30)
    if code != 0:
        return errors.command_failed(result, err, out, code, "mdadm")

    result["success"] = True
    return result


def delete_raid_array(array_name: str) -> dict[str, Any]:
    """Fully tears the array down: mdadm --stop, then --zero-superblock
    on every member so the kernel/mdadm genuinely stop recognizing them
    as part of anything - not just "stopped" while still carrying RAID
    metadata that would make them reappear as an (inactive) array on
    the next boot or --assemble --scan. After this, every former member
    is a plain, free disk again, ready to be formatted or put in a
    different array. Deliberately requires the array to already be
    unmounted first - same layering as the rest of this module: the
    share cascade + actual unmount is app.py's job (it already owns
    that for plain disks too), this function's only concern is the
    mdadm/disk teardown itself once nothing depends on the array's
    filesystem anymore."""
    result: dict[str, Any] = {"success": False}

    arrays = {arr["name"]: arr for arr in monitor.get_raid_arrays()}
    arr = arrays.get(array_name)
    if arr is None:
        return errors.fail(result, "raid.unknown_array", array=array_name)

    array_path = f"/dev/{array_name}"
    manageable = {a["name"]: a for a in disk_mutate.list_manageable_raid_arrays()}
    if manageable.get(array_name, {}).get("mounted"):
        return errors.fail(result, "raid.still_mounted", array=array_name)

    mdadm_path = system_tools.find_binary("mdadm")
    if mdadm_path is None:
        return errors.tool_missing(result, "mdadm")

    member_devices = [dev["device"] for dev in (arr.get("devices") or []) if dev.get("device")]

    code, out, err = system_tools.run([mdadm_path, "--stop", array_path], timeout=30)
    if code != 0:
        return errors.command_failed(result, err, out, code, "mdadm")

    # The array itself is already stopped and gone at this point - the
    # part that actually matters - so a single member's zero-superblock
    # failing must never make the whole operation look like it failed
    # when it substantively already succeeded. Surfaced as a warning
    # instead: that one disk may still show up as an (inactive) RAID
    # member until it's wiped by hand.
    warnings = []
    for device in member_devices:
        zcode, zout, zerr = system_tools.run([mdadm_path, "--zero-superblock", device], timeout=30)
        if zcode != 0:
            warnings.append({"code": "raid.zero_superblock_failed", "context": {"device": device, "detail": (zerr or zout or "").strip()}})

    disk_labels.set_label(array_name, "")

    result["success"] = True
    result["members"] = member_devices
    if warnings:
        result["warnings"] = warnings
    return result
