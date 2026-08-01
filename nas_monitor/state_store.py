"""
nas_monitor.state_store
-------------------------
A tiny local JSON key-file store, for the small amount of state this tool
needs to remember that ISN'T derivable by reading real system state
(unlike disks, users, shares - see monitor.py/users.py/smb_shares.py,
which are all read fresh from the system every time, on purpose).

Right now that's just "which remote hosts has a given SSH key been
deployed to" (ssh_keys.py) - the planned operations log will use this
same mechanism.
"""

from __future__ import annotations

import json
import os
from typing import Any

STATE_DIR = "/etc/nas-monitor"


def load(filename: str, default: Any = None) -> Any:
    """Read a JSON file from STATE_DIR. Missing or corrupt -> default
    (never raises - a corrupt state file should degrade gracefully, not
    crash the dashboard)."""
    path = os.path.join(STATE_DIR, filename)
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def save(filename: str, data: Any) -> dict[str, Any]:
    """Write a JSON file to STATE_DIR, creating the directory if needed."""
    result = {"success": False, "error": None}
    path = os.path.join(STATE_DIR, filename)
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_path, path)  # atomic - never leaves a half-written file
    except OSError as exc:
        result["error"] = str(exc)
        return result
    result["success"] = True
    return result
