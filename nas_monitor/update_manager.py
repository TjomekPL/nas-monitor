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

Applying an update restarts the whole service a second after this
module returns (see apply_update()'s comment below) - the operations
log entry for it is written by app.py just before that happens, not
here, since this module has no oplog dependency of its own.
"""

from __future__ import annotations

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

    reset_code, _, _ = _git("reset", "--hard", f"origin/{BRANCH}", timeout=15)
    if reset_code != 0:
        return {"success": False, "error_code": "update.apply_failed"}

    pip_path = f"{APP_DIR}/venv/bin/pip"
    pip_code, _, _ = system_tools.run(
        [pip_path, "install", "-q", "-r", f"{APP_DIR}/requirements.txt"], timeout=60
    )
    if pip_code != 0:
        return {"success": False, "error_code": "update.deps_failed"}

    new_version = get_current_version()

    # Restart from a detached child, not from this request handler directly -
    # `systemctl restart` would kill this process mid-response. The 1s
    # delay gives Flask time to finish sending the JSON body below before
    # the service (and this worker with it) goes down; dashboard.js polls
    # /api/auth/status afterward until the new process answers again.
    subprocess.Popen(
        ["/bin/sh", "-c", "sleep 1 && systemctl restart nas-monitor"],
        start_new_session=True,
    )
    return {"success": True, "version": new_version}
