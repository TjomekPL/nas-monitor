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
MOUNT_BASE = "/mnt"
FSTAB_PATH = "/etc/fstab"

# Conservative allowlist a label must satisfy before it's ever handed
# to mkfs's own -L flag or used to build a mount-point directory name -
# not because mkfs/mkdir would mishandle other characters, but because
# this same string becomes a path component (/mnt/<label>) and an
# /etc/fstab field, and there's no reason to accept anything that could
# be awkward in either place (spaces, slashes, quotes) when a plain
# alnum/dash/underscore name covers every real use case.
_VALID_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

_MKFS_BINARY = {
    "ext4": "mkfs.ext4",
    "btrfs": "mkfs.btrfs",
    "xfs": "mkfs.xfs",
    "exfat": "mkfs.exfat",  # exfatprogs
}

# Every mkfs tool refuses to run over a signature it recognizes unless
# told otherwise - each one spells that differently. wipefs already
# clears the disk and the partition before this runs (see format_disk),
# but that alone hasn't proven reliable enough in practice: it can miss
# some signatures (a real report showed mkfs.xfs still refusing over a
# leftover "partition table (dos)" signature after both wipefs passes
# already ran clean). Passing each tool's own force flag on top is the
# actually-robust fix - the tool's own detection logic is what would
# reject the operation, so overriding it directly is more reliable than
# trying to out-guess every signature format wipefs might not catch.
_MKFS_FORCE_ARGS = {
    "ext4": ["-F"],
    "btrfs": ["-f"],
    "xfs": ["-f"],
    "exfat": [],  # mkfs.exfat (exfatprogs) has no such safety check to override
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


def _disk_fstype(device: str) -> str | None:
    """Filesystem currently on this disk (or its first partition, if
    any), even though nothing is mounted - so the raw-disks table can
    show what's actually there rather than a disk always looking blank
    until you dig into it. None if genuinely blank/unrecognized."""
    lsblk_path = system_tools.find_binary("lsblk")
    if lsblk_path is None:
        return None
    code, out, _ = system_tools.run([lsblk_path, "-J", "-o", "NAME,FSTYPE", device])
    if code != 0 or not out.strip():
        return None
    try:
        import json
        data = json.loads(out)
    except ValueError:
        return None

    def walk(devices):
        for dev in devices:
            fstype = dev.get("fstype")
            if fstype:
                return fstype
            found = walk(dev.get("children") or [])
            if found:
                return found
        return None

    return walk(data.get("blockdevices", []))


def list_raw_disks() -> list[dict[str, Any]]:
    """Whole disks that are not the boot disk, not part of any RAID
    array, and have nothing currently mounted on them - disks a human
    could safely format or wipe right now without checking anything
    else first. Everything else (the system disk, mounted disks, RAID
    members) stays out of this list entirely and is shown elsewhere."""
    boot_disk = _boot_disk_name()
    mounted = _mounted_disk_names()
    raid_members = _raid_member_disk_names()

    raw = []
    for disk in monitor.list_disks():
        if disk["name"] == boot_disk or disk["name"] in mounted or disk["name"] in raid_members:
            continue
        disk = dict(disk)
        disk["fstype"] = _disk_fstype(disk["path"])
        raw.append(disk)
    return raw


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


def format_disk(device: str, filesystem: str, label: str = "", auto_mount: bool = True) -> dict[str, Any]:
    """Wipe -> new GPT label -> one partition spanning the disk ->
    mkfs on that partition -> (by default) mount it and make that
    persistent across reboots. See the module docstring for why this
    never formats the raw disk device directly.

    auto_mount defaults on: wiping a disk is preparation for a future
    RAID array (no mount wanted, mdadm needs the bare partition), but
    formatting is preparation for standalone use - a formatted disk you
    still have to SSH in and mount by hand isn't actually usable yet,
    so this finishes the job. Mount-point naming and fstab persistence
    are still a nice-to-have on top of the filesystem itself, though:
    if either step fails, that failure comes back as a warning on an
    otherwise-successful result, not a hard failure - the disk is
    correctly formatted either way, and can always be mounted by hand
    afterward."""
    result: dict[str, Any] = {"device": device, "filesystem": filesystem, "success": False}

    if filesystem not in SUPPORTED_FILESYSTEMS:
        return errors.fail(result, "disks.unsupported_filesystem", filesystem=filesystem)

    label = label.strip()
    if label and not _VALID_LABEL_RE.match(label):
        return errors.fail(result, "disks.invalid_label", label=label)

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

    # Second wipefs, now on the partition itself. The one above only
    # ever reaches signatures that live in the whole *disk's* own
    # address space (its GPT/MBR header). If the new partition lands at
    # the same offset an old one used to - very likely, since both are
    # created the same way ("0% to 100%") - whatever filesystem used to
    # live there is still physically sitting in that data region, on
    # its own separate device node (/dev/sda1, not /dev/sda), which the
    # first wipefs never touched.
    code, out, err = system_tools.run([wipefs_path, "-a", partition], timeout=30)
    if code != 0:
        return errors.command_failed(result, err, out, code, "wipefs")

    mkfs_args = [mkfs_path, *_MKFS_FORCE_ARGS[filesystem]]
    if label:
        mkfs_args += ["-L", label]
    mkfs_args.append(partition)
    code, out, err = system_tools.run(mkfs_args, timeout=180)
    if code != 0:
        return errors.command_failed(result, err, out, code, mkfs_binary)

    result["success"] = True
    result["partition"] = partition

    if auto_mount:
        mount_result = _mount_and_persist(partition, filesystem, label or _disk_name(device))
        result["mount_point"] = mount_result.get("mount_point")
        if not mount_result["success"]:
            errors.warn(result, mount_result["error_code"], **mount_result.get("error_context", {}))

    return result


def _get_uuid(partition: str) -> str | None:
    blkid_path = system_tools.find_binary("blkid")
    if blkid_path is None:
        return None
    code, out, _ = system_tools.run([blkid_path, "-s", "UUID", "-o", "value", partition], timeout=10)
    uuid = out.strip()
    return uuid if code == 0 and uuid else None


def _mount_and_persist(partition: str, filesystem: str, mount_name: str) -> dict[str, Any]:
    """Creates /mnt/<mount_name>, adds a UUID-keyed /etc/fstab entry
    (nofail - a disk that's missing at boot, e.g. an unplugged USB
    drive, must never hang the rest of the system coming up), and
    mounts it immediately rather than waiting for the next reboot.
    UUID rather than the device path (/dev/sda1): device names aren't
    guaranteed stable across reboots, especially for USB/hot-plugged
    drives - the UUID always points at the right filesystem regardless
    of what the kernel happens to name it this time.

    Every failure path here uses its own disks.* code rather than the
    generic errors.tool_missing/io_failed/command_failed helpers (which
    all produce system.* codes) - format_disk() surfaces whichever of
    these fires as a *warning* on an otherwise-successful format, and a
    system.* code would double as both this and some unrelated future
    warning that happens to hit the same generic helper, showing this
    disk-specific wording in a context that has nothing to do with
    mounting."""
    result: dict[str, Any] = {"success": False, "mount_point": None}

    uuid = _get_uuid(partition)
    if not uuid:
        return errors.fail(result, "disks.uuid_not_found", partition=partition)

    mount_point = os.path.join(MOUNT_BASE, mount_name)
    try:
        os.makedirs(mount_point, exist_ok=True)
    except OSError as exc:
        return errors.fail(result, "disks.mount_point_failed", path=mount_point, detail=str(exc))

    try:
        with open(FSTAB_PATH, "r") as fh:
            fstab_content = fh.read()
    except OSError as exc:
        return errors.fail(result, "disks.fstab_failed", path=FSTAB_PATH, detail=str(exc))

    if uuid not in fstab_content:
        entry = f"UUID={uuid}  {mount_point}  {filesystem}  defaults,nofail  0  2\n"
        try:
            with open(FSTAB_PATH, "a") as fh:
                fh.write(entry)
        except OSError as exc:
            return errors.fail(result, "disks.fstab_failed", path=FSTAB_PATH, detail=str(exc))

    mount_path = system_tools.find_binary("mount")
    if mount_path is None:
        return errors.fail(result, "disks.mount_tool_missing")

    code, out, err = system_tools.run([mount_path, mount_point], timeout=30)
    if code != 0:
        detail = (err or "").strip() or (out or "").strip() or f"exit code {code}"
        return errors.fail(result, "disks.mount_failed", detail=detail)

    result["success"] = True
    result["mount_point"] = mount_point
    return result
