"""
nas_monitor.system_update
-----------------------------
OS package updates via apt - the system-level counterpart to
update_manager.py's self-update (that one updates nas-monitor itself
via git; this one updates everything else on the box via apt). Same
overall shape on purpose: a cheap "check" that's safe to call often,
and an "apply" that hands off to a detached background process rather
than holding an HTTP request open for however long `apt-get upgrade`
takes.

Deliberately `apt-get upgrade`, never `dist-upgrade`/`full-upgrade`:
upgrade only ever installs newer versions of packages already present,
never removes one to satisfy a dependency change - the safer default
for something running unattended, at the cost of occasionally leaving
a package pinned at its current version until a human runs a
dist-upgrade by hand. Nothing here does that automatically.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from nas_monitor import system_tools

LOG_DIR = "/var/log/nas-monitor"
LOG_PATH = f"{LOG_DIR}/system-update.log"
REBOOT_REQUIRED_PATH = "/var/run/reboot-required"
DONE_MARKER = "=== nas-monitor system update finished ==="
UPDATE_TIMEOUT = 120  # apt-get update - network call, package-index size varies
UPGRADABLE_TIMEOUT = 30  # local query against the index just refreshed above


def check_for_updates() -> dict[str, Any]:
    """Refreshes the package index, then lists what's upgradable. Every
    failure mode returns available: True/False plus an error_code
    instead of raising - a stale/corrupt apt state on his box is a real
    possibility this needs to survive, not crash the whole account
    dialog over."""
    apt_get_path = system_tools.find_binary("apt-get")
    if apt_get_path is None:
        return {"available": False, "error_code": "system_update.apt_missing"}

    update_code, _, update_err = system_tools.run(
        ["/bin/sh", "-c", f"DEBIAN_FRONTEND=noninteractive {apt_get_path} update -qq"], timeout=UPDATE_TIMEOUT
    )
    if update_code != 0:
        return {
            "available": True,
            "error_code": "system_update.refresh_failed",
            "error_context": {"detail": update_err.strip()[:300]},
        }

    apt_path = system_tools.find_binary("apt") or apt_get_path
    list_code, out, list_err = system_tools.run([apt_path, "list", "--upgradable"], timeout=UPGRADABLE_TIMEOUT)
    if list_code != 0:
        return {
            "available": True,
            "error_code": "system_update.list_failed",
            "error_context": {"detail": list_err.strip()[:300]},
        }

    packages = []
    for line in out.splitlines():
        line = line.strip()
        # "apt list" always opens with a "Listing..." progress line on
        # stdout, never a real package entry - and every genuine entry
        # has the "name/suite version arch [...]" shape, i.e. a slash.
        if not line or line.startswith("Listing") or "/" not in line:
            continue
        packages.append(line.split("/", 1)[0])

    return {
        "available": True,
        "count": len(packages),
        "packages": packages,
        "update_available": len(packages) > 0,
        "reboot_required": os.path.isfile(REBOOT_REQUIRED_PATH),
    }


def apply_updates() -> dict[str, Any]:
    """Kicks off apt-get update + upgrade -y in a detached background
    process and returns immediately - identical reasoning to
    update_manager.apply_update(): this can take anywhere from
    instant to several minutes depending on what's downloading, and an
    HTTP request has no business staying open for that. Progress is
    polled separately via get_progress(), which tails the same log
    file this writes DONE_MARKER to when finished."""
    apt_get_path = system_tools.find_binary("apt-get")
    if apt_get_path is None:
        return {"success": False, "error_code": "system_update.apt_missing"}

    cmd = (
        f"mkdir -p {LOG_DIR} && : > {LOG_PATH} && "
        f"( DEBIAN_FRONTEND=noninteractive {apt_get_path} update -qq && "
        f"DEBIAN_FRONTEND=noninteractive {apt_get_path} upgrade -y; "
        f"echo '{DONE_MARKER}' ) >> {LOG_PATH} 2>&1"
    )
    subprocess.Popen(["/bin/sh", "-c", cmd], start_new_session=True)
    return {"success": True}


def get_progress() -> dict[str, Any]:
    """Best-effort tail of the running/just-finished update log, for the
    frontend to poll while apply_updates() runs in the background. No
    log file yet (never run, or apt_get_path was missing when
    apply_updates() was called) reads the same as "not done" rather
    than erroring - the frontend just keeps polling either way."""
    try:
        with open(LOG_PATH, "r") as fh:
            content = fh.read()
    except OSError:
        return {"done": False, "tail": ""}
    lines = content.splitlines()
    return {
        "done": DONE_MARKER in content,
        "tail": "\n".join(lines[-40:]),
        "reboot_required": os.path.isfile(REBOOT_REQUIRED_PATH),
    }
