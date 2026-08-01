"""
nas_monitor.ssh_keys
----------------------
SSH keypair management for system accounts - the piece that was missing
for the "sync files with another NAS via rsync" workflow: an SMB password
(see smb.py) does nothing for SSH auth, and a system account needs both a
login-capable shell (see users.py's allow_login) AND its own SSH key.

This module only ever touches ~/.ssh for the account in question. The
private key never leaves this machine; "deploying" a key means installing
the PUBLIC half into a remote host's authorized_keys, using a one-time
password that is never written to disk or logged (passed via an env var
to sshpass, not argv - argv is visible to anyone on the box via `ps`).
"""

from __future__ import annotations

import os
import pwd
import re
import shlex
from datetime import datetime, timezone
from typing import Any

from nas_monitor import system_tools
from nas_monitor import users as users_mod
from nas_monitor import state_store

KEY_COMMENT_SUFFIX = "@nas-monitor"
_HOST_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,255}$")
_DEPLOYMENTS_FILE = "ssh-deployments.json"


def _ssh_dir(username: str) -> str:
    return os.path.join(pwd.getpwnam(username).pw_dir, ".ssh")


def _key_paths(username: str) -> tuple[str, str]:
    ssh_dir = _ssh_dir(username)
    return os.path.join(ssh_dir, "id_ed25519"), os.path.join(ssh_dir, "id_ed25519.pub")


def _load_deployments() -> dict[str, list[dict[str, Any]]]:
    return state_store.load(_DEPLOYMENTS_FILE, default={}) or {}


def _save_deployments(data: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return state_store.save(_DEPLOYMENTS_FILE, data)


def _record_deployment(
    username: str, host: str, remote_user: str, public_key: str, display_name: str | None = None
) -> None:
    data = _load_deployments()
    entries = data.setdefault(username, [])
    entries[:] = [e for e in entries if not (e["host"] == host and e["remote_user"] == remote_user)]
    entries.append(
        {
            "host": host,
            "remote_user": remote_user,
            "public_key": public_key,
            "display_name": (display_name or "").strip() or None,
            "deployed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    _save_deployments(data)


def get_deployments(username: str) -> list[dict[str, Any]]:
    """Every remote host this key has ever been deployed to, each flagged
    is_current: True if that deployment's public key still matches what's
    currently on disk for this user - False means the key was regenerated
    since (see generate_key) and that device now has a stale entry."""
    data = _load_deployments()
    entries = data.get(username, [])

    current_pub = None
    _, pub_path = _key_paths(username)
    if os.path.isfile(pub_path):
        try:
            with open(pub_path, "r") as fh:
                current_pub = fh.read().strip()
        except OSError:
            pass

    result = []
    for e in sorted(entries, key=lambda e: e.get("deployed_at", "")):
        result.append(
            {
                "host": e["host"],
                "remote_user": e["remote_user"],
                "display_name": e.get("display_name") or e["host"],
                "deployed_at": e.get("deployed_at"),
                "is_current": current_pub is not None and e.get("public_key") == current_pub,
            }
        )
    return result


def get_key_status(username: str) -> dict[str, Any]:
    """Whether this user has a keypair here, and their public key text if
    so - never returns anything about the private key's content."""
    result: dict[str, Any] = {"username": username, "has_key": False, "public_key": None, "error": None}

    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        result["error"] = f"Użytkownik '{username}' nie istnieje"
        return result

    _, pub_path = _key_paths(username)
    if os.path.isfile(pub_path):
        try:
            with open(pub_path, "r") as fh:
                result["public_key"] = fh.read().strip()
            result["has_key"] = True
        except OSError as exc:
            result["error"] = str(exc)

    result["can_login"] = pw.pw_shell not in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false", "")
    result["deployments"] = get_deployments(username)
    return result


def generate_key(username: str) -> dict[str, Any]:
    """Generate a new ed25519 keypair for username's own ~/.ssh, owned by
    that user with correct (private-key-only-readable) permissions."""
    result: dict[str, Any] = {"username": username, "success": False, "error": None}

    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        result["error"] = f"Użytkownik '{username}' nie istnieje"
        return result

    if pw.pw_shell in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false", ""):
        result["error"] = (
            "To konto ma wyłączone logowanie (nologin) - klucz SSH nic by nie dał. "
            "Włącz logowanie/SSH przy edycji użytkownika, jeśli to konto ma używać rsync przez SSH."
        )
        return result

    priv_path, pub_path = _key_paths(username)
    if os.path.isfile(priv_path):
        result["error"] = f"Klucz dla '{username}' już istnieje - usuń go najpierw, jeśli chcesz wygenerować nowy"
        return result

    ssh_dir = _ssh_dir(username)
    try:
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        os.chown(ssh_dir, pw.pw_uid, pw.pw_gid)
        os.chmod(ssh_dir, 0o700)
    except OSError as exc:
        result["error"] = f"Nie udało się przygotować {ssh_dir}: {exc}"
        return result

    keygen_path = system_tools.find_binary("ssh-keygen")
    if keygen_path is None:
        result["error"] = "ssh-keygen not installed"
        return result

    code, out, err = system_tools.run(
        [
            keygen_path,
            "-t", "ed25519",
            "-f", priv_path,
            "-N", "",  # no passphrase - this key is meant for unattended rsync
            "-C", f"{username}{KEY_COMMENT_SUFFIX}",
        ],
        timeout=15,
    )
    if code != 0:
        result["error"] = err.strip() or out.strip() or f"ssh-keygen exited {code}"
        return result

    try:
        os.chown(priv_path, pw.pw_uid, pw.pw_gid)
        os.chmod(priv_path, 0o600)
        os.chown(pub_path, pw.pw_uid, pw.pw_gid)
        os.chmod(pub_path, 0o644)
    except OSError as exc:
        result["error"] = f"Klucz wygenerowany, ale nie udało się ustawić uprawnień: {exc}"
        return result

    with open(pub_path, "r") as fh:
        result["public_key"] = fh.read().strip()
    result["success"] = True
    return result


def delete_key(username: str) -> dict[str, Any]:
    result: dict[str, Any] = {"username": username, "success": False, "error": None}
    try:
        pwd.getpwnam(username)
    except KeyError:
        result["error"] = f"Użytkownik '{username}' nie istnieje"
        return result

    priv_path, pub_path = _key_paths(username)
    try:
        for p in (priv_path, pub_path):
            if os.path.isfile(p):
                os.remove(p)
    except OSError as exc:
        result["error"] = f"Nie udało się usunąć klucza: {exc}"
        return result

    result["success"] = True
    return result


def deploy_key_to_remote(
    username: str, remote_host: str, remote_user: str, remote_password: str, display_name: str | None = None
) -> dict[str, Any]:
    """Install username's PUBLIC key into remote_user@remote_host's
    authorized_keys, using remote_password exactly once (an env var, not
    an argv - never written to disk, never logged, never touches argv
    where `ps` could see it). display_name is a purely cosmetic label for
    the deployments list (e.g. "vOMV") - reverse DNS on a typical home
    LAN is unreliable, so this is asked for explicitly instead of guessed."""
    result: dict[str, Any] = {"username": username, "success": False, "error": None}

    if not remote_password:
        result["error"] = "Puste hasło zdalnego konta"
        return result
    if not _HOST_RE.match(remote_host):
        result["error"] = "Nieprawidłowa nazwa/adres zdalnego hosta"
        return result
    if not users_mod.is_valid_username(remote_user):
        result["error"] = "Nieprawidłowa nazwa zdalnego użytkownika"
        return result

    status = get_key_status(username)
    if not status.get("has_key"):
        result["error"] = f"Użytkownik '{username}' nie ma jeszcze wygenerowanego klucza"
        return result

    sshpass_path = system_tools.find_binary("sshpass")
    if sshpass_path is None:
        result["error"] = "sshpass not installed"
        return result
    ssh_copy_id_path = system_tools.find_binary("ssh-copy-id")
    if ssh_copy_id_path is None:
        result["error"] = "ssh-copy-id not installed"
        return result

    _, pub_path = _key_paths(username)
    cmd = [
        sshpass_path, "-e",
        ssh_copy_id_path,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-i", pub_path,
        f"{remote_user}@{remote_host}",
    ]
    code, out, err = system_tools.run(cmd, timeout=20, extra_env={"SSHPASS": remote_password})
    if code != 0:
        result["error"] = (err.strip() or out.strip() or f"ssh-copy-id exited {code}")[:400]
        return result

    with open(pub_path, "r") as fh:
        pub_content = fh.read().strip()
    _record_deployment(username, remote_host, remote_user, pub_content, display_name=display_name)

    result["success"] = True
    return result


def remove_deployment(
    username: str, remote_host: str, remote_user: str, remote_password: str
) -> dict[str, Any]:
    """Remove this key from a remote host's authorized_keys (using the
    PUBLIC KEY TEXT recorded at deploy time, not the current local key -
    matters for a stale entry, where the current local key is a different
    one entirely) and drop the local tracking record. Needs the remote
    password again - there's no other way to reliably reach a host whose
    key might already be stale/replaced."""
    result: dict[str, Any] = {"success": False, "error": None}

    if not remote_password:
        result["error"] = "Puste hasło zdalnego konta"
        return result

    data = _load_deployments()
    entries = data.get(username, [])
    match = next((e for e in entries if e["host"] == remote_host and e["remote_user"] == remote_user), None)
    if match is None:
        result["error"] = "Nie znaleziono zapisu o tym wdrożeniu"
        return result

    sshpass_path = system_tools.find_binary("sshpass")
    ssh_path = system_tools.find_binary("ssh")
    if sshpass_path is None or ssh_path is None:
        result["error"] = "sshpass/ssh not installed"
        return result

    # Fixed-string grep -v removes exactly the one recorded line, leaving
    # any other authorized_keys entries (from this tool or elsewhere)
    # untouched. The pubkey text is shell-quoted HERE, locally, and sent
    # as a single already-escaped command string - env vars do NOT
    # propagate through SSH to the remote shell by default, so passing
    # the value that way would leave $VAR empty on the other end and
    # turn `grep -vF ""` into "match everything", wiping the whole file.
    quoted_pubkey = shlex.quote(match["public_key"])
    remote_script = (
        f"f=~/.ssh/authorized_keys; "
        f"if [ -f \"$f\" ]; then "
        f"grep -vF -- {quoted_pubkey} \"$f\" > \"$f.tmp\" || true; "
        f"mv \"$f.tmp\" \"$f\"; "
        f"fi"
    )
    cmd = [
        sshpass_path, "-e",
        ssh_path,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        f"{remote_user}@{remote_host}",
        remote_script,
    ]
    code, out, err = system_tools.run(cmd, timeout=20, extra_env={"SSHPASS": remote_password})
    if code != 0:
        result["error"] = (err.strip() or out.strip() or f"exit {code}")[:400]
        return result

    entries[:] = [e for e in entries if not (e["host"] == remote_host and e["remote_user"] == remote_user)]
    data[username] = entries
    save_result = _save_deployments(data)
    if not save_result["success"]:
        result["error"] = f"Usunięto ze zdalnego urządzenia, ale nie udało się zapisać lokalnie: {save_result['error']}"
        return result

    result["success"] = True
    return result
