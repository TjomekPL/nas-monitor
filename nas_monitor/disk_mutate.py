"""
nas_monitor.disk_mutate
--------------------------
Destructive disk operations: format and wipe. Nothing in
nas_monitor.monitor touches disk contents - this is where that line
gets crossed, so it carries the same caution as network_mutate.py's
IP/gateway changes, with one important difference: there's no possible
auto-rollback here. A wiped disk stays wiped. Every guard below exists
to make sure an operation can only ever land on a disk that's genuinely
just sitting there unused - re-checked at call time, not trusted from
whatever the browser's disk list happened to say a few seconds ago.

Formatting always creates a single GPT partition spanning the whole
disk, then formats that partition - never the raw disk device directly.
That's what every other Linux tool (and Windows/macOS, if the disk is
ever moved to another machine) expects to find, and it keeps a
formatted disk and a future RAID member (also a partition, not a raw
disk) consistent with each other.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from nas_monitor import system_tools, errors, monitor

SUPPORTED_FILESYSTEMS = {"ext4", "btrfs", "xfs", "exfat"}

_MKFS_BINARY = {
    "ext4": "mkfs.ext4",
    "btrfs": "mkfs.btrfs",
    "xfs": "mkfs.xfs",
    "exfat": "mkfs.exfat",  # exfatprogs
}


def _disk_name(device_or_partition: str) -> str:
    """"/dev/sda1" -> "sda", "/dev/nvme0n1p1" -> "nvme0n1",
    "/dev/mmcblk0p1" -> "mmcblk0" (SD cards - relevant on a Raspberry
    Pi), "/dev/sda" -> "sda" unchanged. nvme/mmcblk whole-disk names end
    in a digit themselves ("nvme0n1", "mmcblk0"), so that pattern is
    checked first - otherwise the generic letters+digits fallback below
    would wrongly treat their own trailing digit as a partition number
    to strip (mmcblk0 -> mmcblk, silently pointing at a device that
    doesn't exist)."""
    base = device_or_partition.rsplit("/", 1)[-1]
    if re.match(r"^(nvme\d+n\d+|mmcblk\d+)$", base):
        return base
    m = re.match(r"^(nvme\d+n\d+|mmcblk\d+)p\d+$", base)
    if m:
        return m.group(1)
    m = re.match(r"^([a-zA-Z]+)\d+$", base)
    if m:
        return m.group(1)
    return base


def _partition_path(device: str) -> str:
    """The single partition format_disk() creates: "/dev/sda" ->
    "/dev/sda1", "/dev/nvme0n1" -> "/dev/nvme0n1p1", "/dev/mmcblk0" ->
    "/dev/mmcblk0p1"."""
    base = device.rsplit("/", 1)[-1]
    if re.match(r"^(nvme\d+n\d+|mmcblk\d+)$", base):
        return f"{device}p1"
    return f"{device}1"


def _boot_disk_name() -> str | None:
    """Whole-disk name (e.g. "sda") backing the root filesystem -
    resolved via findmnt + lsblk's PKNAME (parent kernel device name),
    so it's correct whether "/" sits directly on a disk or (the normal
    case) on a partition of one."""
    findmnt_path = system_tools.find_binary("findmnt")
    lsblk_path = system_tools.find_binary("lsblk")
    if findmnt_path is None or lsblk_path is None:
        return None

    code, out, _ = system_tools.run([findmnt_path, "-no", "SOURCE", "/"])
    if code != 0 or not out.strip():
        return None
    source = out.strip().splitlines()[0]

    code, out, _ = system_tools.run([lsblk_path, "-no", "PKNAME", source])
    parent = out.strip().splitlines()[0] if code == 0 and out.strip() else ""
    if parent:
        return parent
    return _disk_name(source)


def _mounted_disk_names() -> set[str]:
    """Whole-disk names with at least one currently-mounted partition,
    or mounted directly with no partition table."""
    lsblk_path = system_tools.find_binary("lsblk")
    if lsblk_path is None:
        return set()
    code, out, _ = system_tools.run([lsblk_path, "-J", "-o", "NAME,MOUNTPOINT,TYPE"])
    if code != 0 or not out.strip():
        return set()
    try:
        import json
        data = json.loads(out)
    except ValueError:
        return set()

    mounted: set[str] = set()

    def walk(devices, current_disk):
        for dev in devices:
            disk = dev.get("name") if dev.get("type") == "disk" else current_disk
            if dev.get("mountpoint") and disk:
                mounted.add(disk)
            walk(dev.get("children") or [], disk)

    walk(data.get("blockdevices", []), None)
    return mounted


def _raid_member_disk_names() -> set[str]:
    members: set[str] = set()
    for array in monitor.get_raid_arrays():
        for dev in array.get("devices", []):
            path = dev.get("device")
            if path:
                members.add(_disk_name(path))
    return members


def list_raw_disks() -> list[dict[str, Any]]:
    """Whole disks that are not the boot disk, not part of any RAID
    array, and have nothing currently mounted on them - disks a human
    could safely format or wipe right now without checking anything
    else first. Everything else (the system disk, mounted disks, RAID
    members) stays out of this list entirely and is shown elsewhere."""
    boot_disk = _boot_disk_name()
    mounted = _mounted_disk_names()
    raid_members = _raid_member_disk_names()

    return [
        disk
        for disk in monitor.list_disks()
        if disk["name"] != boot_disk and disk["name"] not in mounted and disk["name"] not in raid_members
    ]


def check_disk_safe_to_modify(device: str) -> dict[str, Any]:
    """The guard shared by format_disk/wipe_disk. Exposed on its own too
    - the frontend calls this right before showing a destructive
    confirmation dialog, so a disk that got mounted or RAID-joined in
    the seconds since the table last refreshed is caught before the
    confirmation even appears, not after the person has already typed
    the disk name in to confirm."""
    result: dict[str, Any] = {"safe": False}
    name = _disk_name(device)

    known = {d["name"] for d in monitor.list_disks()}
    if name not in known:
        return errors.fail(result, "disks.not_found", device=device)
    if name == _boot_disk_name():
        return errors.fail(result, "disks.is_boot_disk", device=device)
    if name in _mounted_disk_names():
        return errors.fail(result, "disks.is_mounted", device=device)
    if name in _raid_member_disk_names():
        return errors.fail(result, "disks.is_raid_member", device=device)

    result["safe"] = True
    return result


def wipe_disk(device: str) -> dict[str, Any]:
    """Erases the partition table and any filesystem signatures -
    leaves the disk genuinely blank: no filesystem, ready to be
    formatted or used as a fresh RAID member. Irreversible."""
    result: dict[str, Any] = {"device": device, "success": False}
    safety = check_disk_safe_to_modify(device)
    if not safety["safe"]:
        return errors.propagate(result, safety)

    wipefs_path = system_tools.find_binary("wipefs")
    if wipefs_path is None:
        return errors.tool_missing(result, "wipefs")

    code, out, err = system_tools.run([wipefs_path, "-a", device], timeout=30)
    if code != 0:
        return errors.command_failed(result, err, out, code, "wipefs")

    result["success"] = True
    return result


def format_disk(device: str, filesystem: str) -> dict[str, Any]:
    """Wipe -> new GPT label -> one partition spanning the disk ->
    mkfs on that partition. See the module docstring for why this never
    formats the raw disk device directly."""
    result: dict[str, Any] = {"device": device, "filesystem": filesystem, "success": False}

    if filesystem not in SUPPORTED_FILESYSTEMS:
        return errors.fail(result, "disks.unsupported_filesystem", filesystem=filesystem)

    safety = check_disk_safe_to_modify(device)
    if not safety["safe"]:
        return errors.propagate(result, safety)

    mkfs_binary = _MKFS_BINARY[filesystem]
    wipefs_path = system_tools.find_binary("wipefs")
    parted_path = system_tools.find_binary("parted")
    mkfs_path = system_tools.find_binary(mkfs_binary)
    if wipefs_path is None:
        return errors.tool_missing(result, "wipefs")
    if parted_path is None:
        return errors.tool_missing(result, "parted")
    if mkfs_path is None:
        return errors.tool_missing(result, mkfs_binary)

    code, out, err = system_tools.run([wipefs_path, "-a", device], timeout=30)
    if code != 0:
        return errors.command_failed(result, err, out, code, "wipefs")

    code, out, err = system_tools.run(
        [parted_path, "-s", device, "mklabel", "gpt", "mkpart", "primary", "0%", "100%"], timeout=30
    )
    if code != 0:
        return errors.command_failed(result, err, out, code, "parted")

    partition = _partition_path(device)
    # the kernel needs a moment to create the new partition's device
    # node after parted writes the table - poll briefly rather than
    # assume it's instantaneous.
    for _ in range(20):
        if os.path.exists(partition):
            break
        time.sleep(0.25)
    else:
        return errors.fail(result, "disks.partition_not_ready", partition=partition)

    code, out, err = system_tools.run([mkfs_path, partition], timeout=180)
    if code != 0:
        return errors.command_failed(result, err, out, code, mkfs_binary)

    result["success"] = True
    result["partition"] = partition
    return result
