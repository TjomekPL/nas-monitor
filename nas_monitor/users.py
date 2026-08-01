"""
nas_monitor.users
-------------------
System-level user and group management (protocol-agnostic - nothing here
knows about SMB, NFS, or anything else). Detection reads real system state
(/etc/passwd, /etc/group), never a separate database. Creation wraps
useradd/usermod with validation.

See nas_monitor/smb.py for the SMB-specific layer (SMB passwords, share
access) that sits on top of the accounts this module creates.
"""

from __future__ import annotations

import re
import pwd
import grp
import os
from typing import Any

from nas_monitor import system_tools

# Standard Debian range for real ("human") accounts/groups - below this are
# system/service accounts (root, daemon, www-data, ...) we don't want to
# surface as candidates for SMB access.
DEFAULT_MIN_UID = 1000
DEFAULT_MAX_UID = 59999
DEFAULT_MIN_GID = 1000

# Shells that mean "this account can log in" - anything else (nologin,
# false, or a shell not in this set) is treated as login-incapable. Debian's
# nologin lives at /usr/sbin/nologin; some other distros/older setups use
# /sbin/nologin or /bin/false.
_NOLOGIN_SHELLS = {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false", ""}

# Username rule matching useradd's own default validation (POSIX-ish):
# lowercase letters/digits/underscore/hyphen, must start with a letter or
# underscore, max 32 chars. Rejecting anything else here - before it ever
# reaches a shell command - is the actual injection defense; subprocess
# with an argv list already avoids shell interpretation, but a bad username
# could still confuse useradd/smbpasswd in surprising ways.
_VALID_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def default_nologin_shell() -> str:
    for candidate in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false"):
        if os.path.isfile(candidate):
            return candidate
    # Debian always ships nologin via base-passwd even if not found above -
    # fall back to the standard path rather than failing outright.
    return "/usr/sbin/nologin"


def is_valid_username(username: str) -> bool:
    return bool(_VALID_USERNAME_RE.match(username))


def list_system_users(min_uid: int = DEFAULT_MIN_UID, max_uid: int = DEFAULT_MAX_UID) -> list[dict[str, Any]]:
    """Real ('human') system accounts, via the pwd database - not a parsed
    /etc/passwd file, so this also works with NSS backends (LDAP etc.)."""
    users = []
    for entry in pwd.getpwall():
        if not (min_uid <= entry.pw_uid <= max_uid):
            continue
        users.append(
            {
                "username": entry.pw_name,
                "display_name": (entry.pw_gecos.split(",")[0].strip() or entry.pw_name) if entry.pw_gecos else entry.pw_name,
                "uid": entry.pw_uid,
                "gid": entry.pw_gid,
                "home": entry.pw_dir,
                "shell": entry.pw_shell,
                "can_login": entry.pw_shell not in _NOLOGIN_SHELLS,
                "groups": _groups_for_user(entry.pw_name),
            }
        )
    return sorted(users, key=lambda u: u["username"])


def list_system_groups(min_gid: int = DEFAULT_MIN_GID) -> list[dict[str, Any]]:
    """Real (non-system) groups, via the grp database."""
    groups = []
    for entry in grp.getgrall():
        if entry.gr_gid < min_gid:
            continue
        groups.append(
            {
                "name": entry.gr_name,
                "gid": entry.gr_gid,
                "members": list(entry.gr_mem),
            }
        )
    return sorted(groups, key=lambda g: g["name"])


def _groups_for_user(username: str) -> list[str]:
    return sorted(g.gr_name for g in grp.getgrall() if username in g.gr_mem)


def user_exists(username: str) -> bool:
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def ensure_group_exists(group_name: str) -> dict[str, Any]:
    """Create a group if it doesn't already exist. useradd -G requires
    secondary groups to pre-exist - it won't create them itself, and the
    UI lets you type a brand new group name at user-creation time."""
    result: dict[str, Any] = {"group": group_name, "success": False, "error": None}

    try:
        grp.getgrnam(group_name)
        result["success"] = True  # already exists, nothing to do
        return result
    except KeyError:
        pass

    groupadd_path = system_tools.find_binary("groupadd")
    if groupadd_path is None:
        result["error"] = "groupadd not installed"
        return result

    code, out, err = system_tools.run([groupadd_path, group_name])
    if code != 0:
        result["error"] = err.strip() or f"groupadd exited {code}"
        return result

    result["success"] = True
    return result


def create_user(
    raw_username: str,
    groups: list[str] | None = None,
    shell: str | None = None,
    create_home: bool = True,
) -> dict[str, Any]:
    """Create a system account. Read-only detection functions above never
    mutate anything; this is the one function in this module that does.

    Linux usernames must be lowercase, but SMB usage (Tomek, Wacek, ...)
    doesn't follow that convention. raw_username is lowercased to become
    the actual system/SMB account name; the original capitalization is
    kept as the account's GECOS "full name" (display_name in the result
    and in list_system_users()) purely for display - it has no effect on
    login/auth, which always happens against the lowercase account.

    shell=None uses the nologin shell (the default for SMB-only accounts -
    see the project README for why this matters for SSH access).
    """
    display_name = raw_username.strip()
    username = display_name.lower()
    result: dict[str, Any] = {"username": username, "display_name": display_name, "success": False, "error": None}

    if ":" in display_name or "\n" in display_name:
        result["error"] = "Nazwa nie może zawierać ':' ani znaku nowej linii"
        return result

    if not is_valid_username(username):
        result["error"] = (
            "Nieprawidłowa nazwa użytkownika (litery/cyfry/_/-, "
            "musi zaczynać się literą lub _, max 32 znaki)"
        )
        return result

    if user_exists(username):
        result["error"] = f"Użytkownik '{username}' już istnieje"
        return result

    useradd_path = system_tools.find_binary("useradd")
    if useradd_path is None:
        result["error"] = "useradd not installed"
        return result

    for g in groups or []:
        if not is_valid_username(g):  # group names follow the same rules
            result["error"] = f"Nieprawidłowa nazwa grupy: {g}"
            return result
    for g in groups or []:
        group_result = ensure_group_exists(g)
        if not group_result["success"]:
            result["error"] = f"Nie udało się przygotować grupy '{g}': {group_result['error']}"
            return result

    resolved_shell = shell or default_nologin_shell()

    cmd = [useradd_path, "-s", resolved_shell, "-c", display_name]
    if create_home:
        cmd.append("-m")
    else:
        cmd.append("-M")
    if groups:
        cmd += ["-G", ",".join(groups)]
    cmd.append(username)

    code, out, err = system_tools.run(cmd)
    if code != 0:
        result["error"] = err.strip() or f"useradd exited {code}"
        return result

    result["success"] = True
    result["shell"] = resolved_shell
    result["groups"] = groups or []
    return result


def update_user(
    username: str,
    groups: list[str] | None = None,
    shell: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Update an existing account. Every parameter is optional - pass None
    for anything you don't want to touch. groups=[] explicitly clears all
    secondary group memberships (usermod -G replaces the whole list, so
    this always sends the FULL desired membership, not a delta).

    Renaming the account itself is intentionally not supported here - it
    touches the home directory path and can strand running processes; for
    these disposable SMB-only accounts, delete + recreate is the safer
    equivalent.
    """
    result: dict[str, Any] = {"username": username, "success": False, "error": None}

    if not user_exists(username):
        result["error"] = f"Użytkownik '{username}' nie istnieje"
        return result

    usermod_path = system_tools.find_binary("usermod")
    if usermod_path is None:
        result["error"] = "usermod not installed"
        return result

    cmd = [usermod_path]

    if groups is not None:
        for g in groups:
            if not is_valid_username(g):
                result["error"] = f"Nieprawidłowa nazwa grupy: {g}"
                return result
        for g in groups:
            group_result = ensure_group_exists(g)
            if not group_result["success"]:
                result["error"] = f"Nie udało się przygotować grupy '{g}': {group_result['error']}"
                return result
        cmd += ["-G", ",".join(groups)]

    if shell is not None:
        cmd += ["-s", shell]

    if display_name is not None:
        if ":" in display_name or "\n" in display_name:
            result["error"] = "Nazwa nie może zawierać ':' ani znaku nowej linii"
            return result
        cmd += ["-c", display_name]

    if len(cmd) == 1:
        result["success"] = True  # nothing requested to change
        return result

    cmd.append(username)
    code, out, err = system_tools.run(cmd)
    if code != 0:
        result["error"] = err.strip() or f"usermod exited {code}"
        return result

    result["success"] = True
    return result


def delete_user(username: str, remove_home: bool = False) -> dict[str, Any]:
    """Remove a system account. remove_home defaults to False - these are
    typically SMB-only accounts whose actual files live in a share
    directory, not the account's own home dir, so there's rarely a reason
    to force this and no reason to risk it by default."""
    result: dict[str, Any] = {"username": username, "success": False, "error": None}

    if not user_exists(username):
        result["error"] = f"Użytkownik '{username}' nie istnieje"
        return result

    userdel_path = system_tools.find_binary("userdel")
    if userdel_path is None:
        result["error"] = "userdel not installed"
        return result

    cmd = [userdel_path]
    if remove_home:
        cmd.append("-r")
    cmd.append(username)

    code, out, err = system_tools.run(cmd)
    if code != 0:
        result["error"] = err.strip() or f"userdel exited {code}"
        return result

    result["success"] = True
    return result


def add_user_to_group(username: str, group: str) -> dict[str, Any]:
    """Add username to group WITHOUT touching their other secondary group
    memberships (usermod -aG appends; update_user's groups= replaces the
    whole list, which is right for the edit-user form but wrong here)."""
    result: dict[str, Any] = {"username": username, "success": False, "error": None}

    if not user_exists(username):
        result["error"] = f"Użytkownik '{username}' nie istnieje"
        return result

    group_result = ensure_group_exists(group)
    if not group_result["success"]:
        result["error"] = group_result["error"]
        return result

    usermod_path = system_tools.find_binary("usermod")
    if usermod_path is None:
        result["error"] = "usermod not installed"
        return result

    code, out, err = system_tools.run([usermod_path, "-aG", group, username])
    if code != 0:
        result["error"] = err.strip() or f"usermod exited {code}"
        return result

    result["success"] = True
    return result


def remove_user_from_group(username: str, group: str) -> dict[str, Any]:
    """Remove username from exactly one group, leaving other memberships
    untouched. usermod has no single-group-removal flag, so this
    recomputes the full desired list and replaces it via update_user."""
    if not user_exists(username):
        return {"username": username, "success": False, "error": f"Użytkownik '{username}' nie istnieje"}

    current = _groups_for_user(username)
    if group not in current:
        return {"username": username, "success": True, "error": None}  # already not a member

    return update_user(username, groups=[g for g in current if g != group])
