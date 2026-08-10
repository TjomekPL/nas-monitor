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

from nas_monitor import system_tools, errors, monitor, disk_labels

SUPPORTED_FILESYSTEMS = {"ext4", "btrfs", "xfs", "exfat"}
MOUNT_BASE = "/srv"
FSTAB_PATH = "/etc/fstab"

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
    Pi), "/dev/md0" -> "md0" (RAID arrays - real bug this caught: the
    generic fallback below stripped its own number the exact same way
    a partition number would be, "md0" -> "md", pointing at a device
    that doesn't exist and silently breaking serial lookup / mount
    naming for every array), "/dev/sda" -> "sda" unchanged. nvme/mmcblk
    /md whole-disk names end in a digit themselves, so that pattern is
    checked first - otherwise the generic letters+digits fallback
    would wrongly treat their own trailing digit as a partition number
    to strip."""
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


def _disk_serial(device: str) -> str:
    """Raw serial number for this device, or "" if unavailable/the
    "unknown" placeholder monitor.list_disks() itself falls back to
    for a drive that doesn't report one. Shared by _default_mount_name
    (the actual mount-path source of truth, always /srv/<serial> - see
    format_disk/mount_disk) and label-saving (disk_labels is keyed by
    this same serial, so a label always finds its way back to the
    right physical disk regardless of what /dev/sdX it's on today).

    A RAID array has no serial of its own - its /dev/mdN name is
    already the stable identifier (unlike /dev/sdX, it doesn't shift
    across reboots/reconnects), so that name is used here in its
    place. Without this, saving a label for an array would silently
    go nowhere: _disk_name resolves it correctly, but it was never in
    monitor.list_disks() (physical disks only) to begin with."""
    name = _disk_name(device)
    known = {d["name"]: d for d in monitor.list_disks()}
    serial = (known.get(name) or {}).get("serial") or ""
    if serial.lower() == "unknown":
        serial = ""
    if serial:
        return serial
    if name in {arr["name"] for arr in monitor.get_raid_arrays()}:
        return name
    return ""


def _default_mount_name(device: str) -> str:
    """The mount-point name: the disk's serial number, sanitized to a
    filesystem-safe charset - not the kernel device name (sda, sdb...),
    which is exactly the kind of identifier that can point at a
    completely different physical disk after a reboot or a USB
    reconnect (his point: naming a *persistent* mount path after
    something that isn't itself persistent is backwards), and not a
    user-chosen label either (his later refinement, v0.14.4 - a label
    is meant to be renamed freely, which a path other things depend on
    can't safely be). Falls back to the kernel device name only if no
    usable serial is available at all."""
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "", _disk_serial(device))
    return sanitized if sanitized else _disk_name(device)


def _partition_path(device: str) -> str:
    """The single partition format_disk() creates: "/dev/sda" ->
    "/dev/sda1", "/dev/nvme0n1" -> "/dev/nvme0n1p1", "/dev/mmcblk0" ->
    "/dev/mmcblk0p1", "/dev/md0" -> "/dev/md0p1" (RAID arrays - same
    "needs a p separator" rule as nvme/mmcblk, missed the first time
    around when arrays were added: a real report showed formatting an
    array's ext4 failing with "Partition /dev/md01 didn't appear after
    being created", because md0+"1" reads as device "md01", not
    partition 1 of md0)."""
    base = device.rsplit("/", 1)[-1]
    if re.match(r"^(nvme\d+n\d+|mmcblk\d+|md\d+)$", base):
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


def _raid_member_to_array_map() -> dict[str, str]:
    """Maps each RAID member disk's name to the array it belongs to -
    e.g. {"sdb": "md0", "sdc": "md0"}. Used to group member disks under
    their array in the management table, and to know which array a
    "detach" action on a given disk should actually operate on."""
    mapping: dict[str, str] = {}
    for array in monitor.get_raid_arrays():
        for dev in array.get("devices", []):
            path = dev.get("device")
            if path:
                mapping[_disk_name(path)] = array["name"]
    return mapping


def _disk_state(device: str) -> dict[str, Any]:
    """One lsblk call per disk covering fstype, mountpoint, and the
    actual device-node name carrying them (whichever partition, or the
    whole disk itself) - used by list_manageable_disks so each row
    only needs a single lsblk invocation instead of two, and by
    mount_disk to know exactly which node to hand to `mount` (a disk
    can have a filesystem directly on the whole device with no
    partition table at all - "superfloppy" style - so this can't just
    always assume _partition_path(device))."""
    lsblk_path = system_tools.find_binary("lsblk")
    if lsblk_path is None:
        return {"fstype": None, "mountpoint": None, "device_node": None}
    code, out, _ = system_tools.run([lsblk_path, "-J", "-o", "NAME,FSTYPE,MOUNTPOINT", device], timeout=10)
    if code != 0 or not out.strip():
        return {"fstype": None, "mountpoint": None, "device_node": None}
    try:
        import json
        data = json.loads(out)
    except ValueError:
        return {"fstype": None, "mountpoint": None, "device_node": None}

    def walk(devices):
        for dev in devices:
            fstype = dev.get("fstype")
            mountpoint = dev.get("mountpoint")
            if fstype or mountpoint:
                return {"fstype": fstype, "mountpoint": mountpoint, "device_node": dev.get("name")}
            found = walk(dev.get("children") or [])
            if found:
                return found
        return None

    return walk(data.get("blockdevices", [])) or {"fstype": None, "mountpoint": None, "device_node": None}


_SYSTEM_MOUNTPOINTS = {"/", "/boot", "/boot/efi"}


def _is_system_partition(mountpoint: str | None) -> bool:
    """True for anything that's clearly part of the running OS, not a
    NAS data disk - a fixed set of well-known paths plus swap (lsblk
    reports an active swap partition's MOUNTPOINT as the literal string
    "[SWAP]", not a real path). Checked per-disk independent of whole-
    disk boot-disk detection: a real report showed a *non-boot* test
    disk that happened to carry a leftover /boot/efi partition (from
    earlier, unrelated testing) still showing up in the manageable-
    disks table - _boot_disk_name() correctly excludes the actual boot
    disk as a whole, but says nothing about some other disk carrying a
    stray system partition. This is the second, independent guard for
    that case."""
    return mountpoint in _SYSTEM_MOUNTPOINTS or mountpoint == "[SWAP]"


def list_manageable_raid_arrays() -> list[dict[str, Any]]:
    """RAID arrays in the exact same shape list_manageable_disks()
    returns disks in (name, path, size, model, serial, transport,
    fstype, mount_point, mounted, is_raid_member, label) - so the
    frontend can simply concatenate the two lists and render/manage
    them in one table with the existing Format/Mount/Unmount logic
    completely unchanged. An mdadm array device is just another block
    device to lsblk/mkfs/mount; there's no reason those actions need a
    separate code path just because the underlying device is an array
    instead of a physical disk.

    Arrays that failed detection (mdadm not installed, mdadm --detail
    itself erroring) are skipped - nothing here can safely be offered
    to format/mount without knowing its real state first."""
    lsblk_path = system_tools.find_binary("lsblk")
    raid_member_map = _raid_member_to_array_map()
    arrays = []
    for arr in monitor.get_raid_arrays():
        if arr.get("error"):
            continue
        size = "?"
        if lsblk_path is not None:
            code, out, _ = system_tools.run([lsblk_path, "-d", "-b", "-J", "-o", "SIZE", arr["path"]], timeout=10)
            if code == 0 and out.strip():
                try:
                    import json as _json
                    blockdevices = _json.loads(out).get("blockdevices", [])
                    if blockdevices:
                        size = monitor._human_size(int(blockdevices[0].get("size") or 0))
                except (ValueError, KeyError):
                    pass
        try:
            state = _disk_state(arr["path"])
        except Exception:
            state = {"fstype": None, "mountpoint": None}
        # devices comes straight from monitor.get_raid_arrays() (mdadm
        # --detail --export, or the /proc/mdstat fallback) - working_devices
        # falls back to it if the device list itself came back empty for
        # some reason (e.g. a transient mdadm hiccup right after
        # --create), so the count shown here doesn't silently regress to
        # "(0 disks)" when the array card elsewhere still knows the real
        # number.
        member_count = len(arr.get("devices") or []) or arr.get("working_devices") or 0
        # arr["level"] is already a full string ("raid0", "raid5", ...)
        # from mdadm/mdstat, not a bare number - a real display bug from
        # prepending "RAID" again on top of it ("RAIDraid0").
        level_label = (arr.get("level") or "?").upper()
        arrays.append({
            "name": arr["name"],
            "path": arr["path"],
            "size": size,
            "model": f"{level_label} ({member_count} {'disk' if member_count == 1 else 'disks'})",
            "level": arr.get("level"),
            "serial": arr["name"],  # arrays have no serial of their own - the /dev/mdN name is the stable identifier instead
            "transport": "raid",
            "fstype": state["fstype"],
            "mount_point": state["mountpoint"],
            "mounted": bool(state["mountpoint"]),
            # An array can itself be a member of a PARENT array - nested
            # RAID (e.g. two RAID0s mirrored into a RAID1 on top) is a
            # real, supported mdadm pattern, and _raid_member_to_array_map
            # already generically walks every array's device-path list
            # regardless of whether a given path is a physical disk or
            # another /dev/mdN - so this reuses that same detection
            # rather than a separate array-only check.
            "is_raid_member": arr["name"] in raid_member_map,
            "raid_array": raid_member_map.get(arr["name"]),
            "label": disk_labels.get_label(arr["name"]),
            # Needed by raid_mutate.create_raid_array (and the frontend
            # picker) to decide whether this array is actually eligible
            # to be USED as a component of a new, bigger array - real
            # report: a RAID6 still mid-initial-sync (not degraded, just
            # not finished settling yet) showed up as pickable for
            # nesting, which mdadm building on top of an actively-busy
            # array is not something any real system does or a sane
            # admin would want, degraded or not.
            "is_degraded": bool(arr.get("is_degraded")),
            "is_syncing": arr.get("progress_percent") is not None,
        })
    return arrays


def list_mounted_raid_arrays() -> list[dict[str, Any]]:
    """RAID arrays currently mounted under MOUNT_BASE, with enough state
    (fstype, mount_point) to let an array's existing mount point be
    used as a share location - the same way an individual disk's
    mount point already can be. Deliberately narrow: this is NOT array
    creation/management (still deferred, needs its own async design
    for the long initial sync) - just recognizing an array that's
    already assembled and mounted, so a real gap doesn't block him
    (a real report: an existing, working RAID array wasn't offered as
    a share location at all, only plain disks were). Reuses
    _disk_state - lsblk reports fstype/mountpoint for /dev/mdX exactly
    the same way it does for a plain disk, no special-casing needed."""
    arrays = []
    for arr in monitor.get_raid_arrays():
        if arr.get("error"):
            continue
        try:
            state = _disk_state(arr["path"])
        except Exception:
            continue
        mount_point = state.get("mountpoint") or ""
        if mount_point.startswith(MOUNT_BASE + os.sep):
            arrays.append({
                "name": arr["name"],
                "mount_point": mount_point,
                "fstype": state.get("fstype"),
            })
    return arrays


def list_manageable_disks() -> list[dict[str, Any]]:
    """Every disk except the boot disk and anything else carrying a
    system partition (see _is_system_partition) - the boot disk never
    appears here at all (see the Summary tab for it instead), and
    nothing here is meant to support managing a disk that arrived with
    an existing, foreign multi-partition layout (matches how TrueNAS
    and OpenMediaVault both treat this - a "bring your own partitioned
    disk" workflow is out of scope, not just unbuilt yet; the answer
    for a disk like that is to format it clean, already supported).
    Everything else shows here regardless of state (raw,
    formatted-and-mounted, RAID member) so this table is always the
    one place to manage a disk, not just the ones that happen to be
    empty right now. Each entry carries enough state (fstype,
    mount_point, is_raid_member) for the frontend to decide which
    actions - Format/Wipe (only when genuinely free), Unmount (any
    mounted disk - the boot-disk exclusion above is what actually
    keeps this safe, not where something happens to be mounted), or
    none at all (RAID members - that's the future Arrays section's
    job) - make sense for that row."""
    boot_disk = _boot_disk_name()
    raid_member_map = _raid_member_to_array_map()

    manageable = []
    for disk in monitor.list_disks():
        if disk["name"] == boot_disk:
            continue
        disk = dict(disk)
        # A single disk's state lookup failing (lsblk choking on a
        # disk mid-format, just unplugged, or otherwise in a state this
        # code didn't anticipate) must never take the rest of this list
        # down with it - same reasoning as monitor.get_full_status()'s
        # per-disk try/except, which exists for exactly this failure
        # mode after it once hid the entire Disks & Arrays tab.
        try:
            state = _disk_state(disk["path"])
        except Exception:
            state = {"fstype": None, "mountpoint": None}
        if _is_system_partition(state["mountpoint"]):
            continue
        disk["fstype"] = state["fstype"]
        disk["mount_point"] = state["mountpoint"]
        disk["mounted"] = bool(state["mountpoint"])
        disk["is_raid_member"] = disk["name"] in raid_member_map
        disk["raid_array"] = raid_member_map.get(disk["name"])
        disk["label"] = disk_labels.get_label(disk.get("serial", ""))
        manageable.append(disk)
    return manageable


def check_disk_safe_to_modify(device: str) -> dict[str, Any]:
    """The guard shared by format_disk/wipe_disk. Exposed on its own too
    - the frontend calls this right before showing a destructive
    confirmation dialog, so a disk that got mounted or RAID-joined in
    the seconds since the table last refreshed is caught before the
    confirmation even appears, not after the person has already typed
    the disk name in to confirm."""
    result: dict[str, Any] = {"safe": False}
    name = _disk_name(device)

    # RAID arrays are just as manageable here as physical disks (see
    # list_manageable_raid_arrays) - without this, formatting/wiping an
    # array would always fail with "not found", since
    # monitor.list_disks() only ever enumerates physical disks.
    known = {d["name"] for d in monitor.list_disks()} | {arr["name"] for arr in monitor.get_raid_arrays()}
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


def mount_disk(device: str, label: str = "") -> dict[str, Any]:
    """Mounts a disk's EXISTING filesystem - the non-destructive
    counterpart to format_disk(): that one always wipes and creates a
    fresh filesystem, this one never touches the disk's contents at
    all, just makes an already-formatted-but-currently-unmounted disk
    available (real report: a disk with a real ext4 filesystem showed
    "Free" in the table, offering only Format/Wipe - both destructive -
    with no way to just mount what was already there). Reuses the same
    UUID-keyed /etc/fstab + immediate-mount machinery format_disk()'s
    auto-mount uses (_mount_and_persist), so a disk mounted this way
    behaves identically to one mounted right after formatting.

    The mount point is always /srv/<serial number> (_default_mount_name)
    - never a name the label supplies. His explicit design call: a
    mount path is something else (fstab, share locations) comes to
    depend on, so it has to stay put; a serial never changes for a
    given physical disk and isn't something anyone would want to
    rename, unlike a label. label, if given, is purely cosmetic - saved
    via disk_labels for display next to the device name, never used to
    build any path."""
    result: dict[str, Any] = {"device": device, "success": False}

    label = label.strip()
    if len(label) > disk_labels.MAX_LABEL_LENGTH:
        return errors.fail(result, "disks.label_too_long", max_length=disk_labels.MAX_LABEL_LENGTH)

    safety = check_disk_safe_to_modify(device)
    if not safety["safe"]:
        return errors.propagate(result, safety)

    try:
        state = _disk_state(device)
    except Exception as exc:
        return errors.fail(result, "disks.state_lookup_failed", detail=str(exc))

    if not state["fstype"] or not state["device_node"]:
        return errors.fail(result, "disks.no_filesystem", device=device)

    target = f"/dev/{state['device_node']}"
    mount_result = _mount_and_persist(target, state["fstype"], _default_mount_name(device))
    if not mount_result["success"]:
        return errors.propagate(result, mount_result)

    if label:
        disk_labels.set_label(_disk_serial(device), label)

    result["success"] = True
    result["mount_point"] = mount_result["mount_point"]
    return result


def unmount_disk(device: str) -> dict[str, Any]:
    """Unmounts whatever this disk is currently mounted at (see
    _current_mount_point - not restricted to MOUNT_BASE, since a
    legitimately-mounted-elsewhere disk, e.g. via a desktop session's
    own automounter, still needs a way back into the raw-disks table
    for format/wipe), and removes any matching /etc/fstab entry so it
    doesn't try to come back at next boot.

    The actual safety net is the boot-disk and RAID-member checks
    below, not where the disk happens to be mounted - refuses the boot
    disk outright even if it also has a spare partition mounted
    somewhere _current_mount_point() would otherwise find: this is a
    hard rule of its own, so the button for it can never even appear
    next to a disk carrying the running system."""
    result: dict[str, Any] = {"device": device, "success": False}
    name = _disk_name(device)

    known = {d["name"] for d in monitor.list_disks()} | {arr["name"] for arr in monitor.get_raid_arrays()}
    if name not in known:
        return errors.fail(result, "disks.not_found", device=device)
    if name == _boot_disk_name():
        return errors.fail(result, "disks.is_boot_disk", device=device)
    if name in _raid_member_disk_names():
        return errors.fail(result, "disks.is_raid_member", device=device)

    mount_point = _current_mount_point(device)
    if not mount_point:
        return errors.fail(result, "disks.not_mounted", device=device)

    umount_path = system_tools.find_binary("umount")
    if umount_path is None:
        return errors.tool_missing(result, "umount")

    code, out, err = system_tools.run([umount_path, mount_point], timeout=30)
    if code != 0:
        return errors.command_failed(result, err, out, code, "umount")

    _remove_fstab_entry(mount_point)

    result["success"] = True
    return result


def _current_mount_point(device: str) -> str | None:
    """Wherever this device is currently mounted, if anywhere - not
    restricted to MOUNT_BASE. Originally this only recognized mounts
    under MOUNT_BASE (i.e. only what format_disk()'s auto-mount could
    have set up), but that left a disk mounted some other way (a real
    report: a desktop session's own automounter putting a USB drive at
    /media/<user>/<label>) with no Unmount option at all - already
    mounted, so Format/Wipe don't apply either, and not under
    MOUNT_BASE, so this always returned None. The boot disk exclusion
    in unmount_disk (checked independently, not by mount location) is
    the actual safety net here, not which path something happens to be
    mounted at - once a disk is confirmed non-boot and non-RAID-member,
    wherever it's mounted is fair game to unmount."""
    lsblk_path = system_tools.find_binary("lsblk")
    if lsblk_path is None:
        return None
    code, out, _ = system_tools.run([lsblk_path, "-J", "-o", "NAME,MOUNTPOINT", device], timeout=10)
    if code != 0 or not out.strip():
        return None
    try:
        import json
        data = json.loads(out)
    except ValueError:
        return None

    def walk(devices):
        for dev in devices:
            mp = dev.get("mountpoint")
            if mp:
                return mp
            found = walk(dev.get("children") or [])
            if found:
                return found
        return None

    return walk(data.get("blockdevices", []))


def _remove_fstab_entry(mount_point: str) -> None:
    """Best-effort - if this fails, the stale entry just sits there
    (harmless: `nofail` means it won't hang boot, it'll just silently
    not mount since the filesystem beneath it may since have changed).
    Not surfaced as a warning: unmount already succeeded, which is the
    part that actually matters to whoever clicked the button."""
    try:
        with open(FSTAB_PATH, "r") as fh:
            lines = fh.readlines()
    except OSError:
        return
    kept = [line for line in lines if f" {mount_point} " not in line and not line.rstrip("\n").endswith(f" {mount_point}")]
    if kept == lines:
        return
    try:
        with open(FSTAB_PATH, "w") as fh:
            fh.writelines(kept)
    except OSError:
        pass


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
    afterward.

    label, if given, is purely cosmetic (see mount_disk's docstring for
    why it's decoupled from the mount path itself) - saved via
    disk_labels once the format succeeds, never used to name anything
    on disk."""
    result: dict[str, Any] = {"device": device, "filesystem": filesystem, "success": False}

    if filesystem not in SUPPORTED_FILESYSTEMS:
        return errors.fail(result, "disks.unsupported_filesystem", filesystem=filesystem)

    label = label.strip()
    if len(label) > disk_labels.MAX_LABEL_LENGTH:
        return errors.fail(result, "disks.label_too_long", max_length=disk_labels.MAX_LABEL_LENGTH)

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

    # Real report: the very next wipefs on the new partition intermittently
    # failed with ENOENT ("no such file or directory") even though the
    # os.path.exists() poll below had already returned True - on a
    # /dev/mdN array specifically, whose partitions go through the
    # kernel's "blkext" extended-block-device path rather than the more
    # immediate partition registration a plain SCSI/NVMe disk gets, the
    # dentry can appear in /dev microseconds before the block device is
    # actually openable for I/O. `partprobe` explicitly asks the kernel
    # to re-read the table and emit the uevents parted's own write
    # doesn't reliably trigger on its own for md devices; `udevadm
    # settle` then waits for udev to finish processing that queue before
    # anything here tries to open the result. Both best-effort - parted
    # already succeeded, a missing/failing partprobe or a settle timeout
    # isn't itself a reason to give up, just a nudge before falling back
    # to the poll loop that was already here.
    partprobe_path = system_tools.find_binary("partprobe")
    if partprobe_path:
        system_tools.run([partprobe_path, device], timeout=15)
    udevadm_path = system_tools.find_binary("udevadm")
    if udevadm_path:
        system_tools.run([udevadm_path, "settle", "--timeout=5"], timeout=10)

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
    #
    # A few retries here too, on top of partprobe/udevadm settle above:
    # his real report showed the node passing the os.path.exists() poll
    # and STILL failing this wipefs with ENOENT moments later on an
    # md-device partition - existing in the directory listing and being
    # genuinely openable aren't quite the same guarantee for a device
    # that just appeared. Only retries actual "not ready yet"-shaped
    # failures; a wipefs failure for a different reason surfaces on the
    # first attempt as before.
    for attempt in range(3):
        code, out, err = system_tools.run([wipefs_path, "-a", partition], timeout=30)
        if code == 0:
            break
        if attempt < 2 and "no such file" in err.lower():
            time.sleep(0.5)
            continue
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

    if label:
        disk_labels.set_label(_disk_serial(device), label)

    if auto_mount:
        mount_result = _mount_and_persist(partition, filesystem, _default_mount_name(device))
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
    """Creates MOUNT_BASE/<mount_name> (/srv/<mount_name>), adds a
    UUID-keyed /etc/fstab entry
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
