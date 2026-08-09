"""
nas_monitor.update_manager
-----------------------------
Self-update via git. As of this version, install.sh makes /opt/nas-monitor
a real git checkout of the project's GitHub repo instead of a plain
directory copy - so "check for updates" is just "how far behind is our
HEAD from origin/main", and "apply update" is just a `git fetch` +
`git reset --hard`. Git's own transfer already fetches only the objects
that changed, which is the whole reason this exists instead of a
hand-rolled per-file downloader.

If /opt/nas-monitor isn't a git checkout yet (an install from before
this feature shipped), everything here reports git_managed: False
instead of raising - the fix for that is one more `sudo ./install.sh`,
which converts it. After that this module works unattended.

Applying an update hands off to install.sh in a detached background
process rather than duplicating a subset of what it does - see
apply_update()'s own comment for why. install.sh restarts the service
itself as its last step; the operations log entry for the attempt is
written by app.py right after this module returns "success" (meaning
"the update was kicked off", not "it's finished" - there's no way to
know that synchronously), not here, since this module has no oplog
dependency of its own.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from nas_monitor import system_tools

APP_DIR = "/opt/nas-monitor"
BRANCH = "main"
FETCH_TIMEOUT = 20  # seconds - network call, slower than the local git ops below


def _git(*args: str, timeout: int = 8) -> tuple[int, str, str]:
    git_path = system_tools.find_binary("git")
    if not git_path:
        return 127, "", "git not found"
    return system_tools.run([git_path, "-C", APP_DIR, *args], timeout=timeout)


def _is_git_checkout() -> bool:
    code, out, _ = _git("rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def get_current_version() -> str | None:
    if not _is_git_checkout():
        return None
    code, out, _ = _git("describe", "--tags", "--always", "--dirty")
    return out.strip() if code == 0 else None


def check_for_update() -> dict[str, Any]:
    if not _is_git_checkout():
        return {"git_managed": False, "current_version": None, "update_available": False}

    fetch_code, _, _ = _git("fetch", "--tags", "--quiet", "origin", BRANCH, timeout=FETCH_TIMEOUT)
    current = get_current_version()
    if fetch_code != 0:
        return {
            "git_managed": True,
            "current_version": current,
            "update_available": False,
            "error_code": "update.fetch_failed",
        }

    _, latest_out, _ = _git("describe", "--tags", "--always", f"origin/{BRANCH}")
    _, behind_out, _ = _git("rev-list", "--count", f"HEAD..origin/{BRANCH}")
    try:
        commits_behind = int(behind_out.strip())
    except ValueError:
        commits_behind = 0

    return {
        "git_managed": True,
        "current_version": current,
        "latest_version": latest_out.strip() or None,
        "update_available": commits_behind > 0,
        "commits_behind": commits_behind,
    }


def apply_update() -> dict[str, Any]:
    if not _is_git_checkout():
        return {"success": False, "error_code": "update.not_git_managed"}

    fetch_code, _, _ = _git("fetch", "--tags", "--quiet", "origin", BRANCH, timeout=FETCH_TIMEOUT)
    if fetch_code != 0:
        return {"success": False, "error_code": "update.fetch_failed"}

    reset_code, _, _ = _git("reset", "--hard", f"origin/{BRANCH}", timeout=60)
    if reset_code != 0:
        return {"success": False, "error_code": "update.apply_failed"}

    # Defensive: on a slow disk, `git reset --hard` checking out a large
    # number of files can in principle get interrupted by a timeout
    # partway through, leaving the working tree with HEAD already moved
    # but some tracked files not yet written. Catch that here with a
    # clear, specific error instead of install.sh failing on a missing
    # file with no obvious connection to "the checkout didn't finish".
    if not _path_exists(f"{APP_DIR}/install.sh"):
        return {"success": False, "error_code": "update.incomplete_checkout"}

    new_version = get_current_version()

    # install.sh handles everything a release might actually need -
    # system packages (apt), the venv/pip, nginx + fail2ban config, and
    # finally the systemd service itself (which it restarts as its own
    # last step) - exactly the same path a manual `sudo ./install.sh`
    # takes. An earlier version of this function tried to duplicate a
    # *subset* of that by hand (pip only, only when requirements.txt
    # changed) and kept missing things a real release needed - a new
    # apt package, an nginx config tweak - which only a manual reinstall
    # ever picked up. Running the actual script is what stays correct
    # release over release without this file needing to keep guessing
    # what a given update might touch.
    #
    # Detached and logged, not awaited: apt can take anywhere from
    # instant (nothing changed) to the better part of a minute, and this
    # request should return either way rather than hold the connection
    # open - the frontend already polls for the service coming back
    # up. install.sh's own restart step is what actually brings gunicorn
    # back, same as the old direct Popen used to (see git history) -
    # nothing here schedules a second one.
    subprocess.Popen(
        ["/bin/sh", "-c", f"mkdir -p /var/log/nas-monitor && cd {APP_DIR} && ./install.sh >> /var/log/nas-monitor/self-update.log 2>&1"],
        start_new_session=True,
    )
    return {"success": True, "version": new_version}


def _path_exists(path: str) -> bool:
    return os.path.isfile(path)
