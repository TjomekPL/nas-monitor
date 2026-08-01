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
from typing import Any

from nas_monitor import system_tools
from nas_monitor import users as users_mod

KEY_COMMENT_SUFFIX = "@nas-monitor"
_HOST_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,255}$")


def _ssh_dir(username: str) -> str:
    return os.path.join(pwd.getpwnam(username).pw_dir, ".ssh")


def _key_paths(username: str) -> tuple[str, str]:
    ssh_dir = _ssh_dir(username)
    return os.path.join(ssh_dir, "id_ed25519"), os.path.join(ssh_dir, "id_ed25519.pub")


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
    username: str, remote_host: str, remote_user: str, remote_password: str
) -> dict[str, Any]:
    """Install username's PUBLIC key into remote_user@remote_host's
    authorized_keys, using remote_password exactly once (an env var, not
    an argv - never written to disk, never logged, never touches argv
    where `ps` could see it)."""
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

    result["success"] = True
    return result
