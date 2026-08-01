"""
nas_monitor.system_tools
--------------------------
Small shared helpers for shelling out to system tools (lsblk, smartctl,
mdadm, useradd, smbpasswd, pdbedit, ...) safely and predictably.

Centralized here so the PATH-independence behavior (binaries are resolved
to an absolute path, not left to the current process's possibly-restricted
PATH - this bit us once already via a systemd unit's PATH= directive) only
has to be correct in one place, used by every module in the project.
"""

from __future__ import annotations

import os
import shutil
import subprocess

DEFAULT_TIMEOUT = 8  # seconds - smartctl can be slow to wake spun-down disks

# Fallback search dirs used when a binary isn't on PATH.
_COMMON_BIN_DIRS = ("/usr/sbin", "/sbin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/local/bin")


def find_binary(name: str) -> str | None:
    """Resolve a binary to an absolute path, independent of the current
    process's PATH. Tries shutil.which() first (respects PATH when it's
    sane), then falls back to the common system directories."""
    found = shutil.which(name)
    if found:
        return found
    for directory in _COMMON_BIN_DIRS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def run(
    cmd: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a command, never raise. Returns (returncode, stdout, stderr).

    input_text is piped to stdin when given (e.g. feeding a password to
    smbpasswd -s without it ever appearing in a process listing / argv).

    extra_env adds/overrides environment variables for just this call
    (e.g. SSHPASS for sshpass -e) - safer than passing a secret as a CLI
    argument, which is visible to anyone on the box via `ps`.
    """
    env = None
    if extra_env:
        env = os.environ.copy()
        env.update(extra_env)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive catch-all
        return 1, "", str(exc)
