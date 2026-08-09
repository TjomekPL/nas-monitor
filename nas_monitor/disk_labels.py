"""
nas_monitor.disk_labels
--------------------------
Purely cosmetic, user-chosen names for disks, keyed by serial number -
deliberately decoupled from where a disk actually mounts (always
/srv/<serial>, see disk_mutate._default_mount_name) so that renaming a
disk's label later never has to move real files, rewrite /etc/fstab,
or break an existing share's path. Shown next to the device name in
the disk-management table and the share-creation location picker -
never used to build a path anywhere.

His reasoning for the split (previously the label itself WAS the
mount-point name, v0.13.6/v0.13.7): a path is something other things
(fstab, share locations) come to depend on, so it needs to stay put
once chosen: a serial number never changes for a given physical disk
and was never something anyone would want to rename. A label is
exactly the opposite - meant to be renamed freely - so it can't safely
be the same string as the path.
"""

from __future__ import annotations

from typing import Any

from nas_monitor import state_store

LABELS_FILE = "disk-labels.json"

# Cosmetic only - not a path component anymore, so this is just a sane
# length/content limit for display purposes, not the strict
# filesystem-safe charset a directory name would need.
MAX_LABEL_LENGTH = 64


def get_label(serial: str) -> str:
    if not serial:
        return ""
    data = state_store.load(LABELS_FILE, default={})
    return data.get(serial, "") if isinstance(data, dict) else ""


def get_all_labels() -> dict[str, str]:
    data = state_store.load(LABELS_FILE, default={})
    return dict(data) if isinstance(data, dict) else {}


def set_label(serial: str, label: str) -> dict[str, Any]:
    """label="" removes any existing label for this serial (back to
    showing nothing but the raw device name)."""
    result: dict[str, Any] = {"serial": serial, "success": False}
    if not serial:
        result["success"] = True  # nothing to key a label to - a silent no-op, not an error
        return result

    label = label.strip()[:MAX_LABEL_LENGTH]
    data = state_store.load(LABELS_FILE, default={})
    if not isinstance(data, dict):
        data = {}

    if label:
        data[serial] = label
    else:
        data.pop(serial, None)

    save_result = state_store.save(LABELS_FILE, data)
    result["success"] = save_result["success"]
    if not save_result["success"]:
        result["error_code"] = "system.io_failed"
        result["error_context"] = {"detail": str(save_result.get("error") or "")}
    return result
