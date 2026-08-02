"""
nas_monitor.smb
-----------------
SMB-specific glue: which system accounts (see nas_monitor/users.py) have
Samba access configured, and setting/removing that access. This is the
"backend" module for the SMB protocol - a hypothetical future NFS backend
would be its own separate module and would NOT look like this one, since
NFS's access model isn't username/password at all.

Nothing here creates or modifies Linux accounts - that's users.py's job.
A username must already exist as a system account before it can be given
SMB access (Samba itself enforces this).
"""

from __future__ import annotations

from typing import Any

from nas_monitor import system_tools, errors


def is_installed() -> bool:
    return system_tools.find_binary("smbpasswd") is not None


def list_samba_users() -> dict[str, Any]:
    """Usernames with SMB access configured, via pdbedit -L.

    Returns {"available": bool, "usernames": [...]} plus error_code/
    error_context on failure - same defensive shape as the rest of the
    project: missing tools or permission problems become a status field,
    never an exception.
    """
    result: dict[str, Any] = {"available": False, "usernames": []}

    pdbedit_path = system_tools.find_binary("pdbedit")
    if pdbedit_path is None:
        return errors.tool_missing(result, "pdbedit")

    code, out, err = system_tools.run([pdbedit_path, "-L"])
    if code != 0:
        return errors.command_failed(result, err, out, code, "pdbedit")

    usernames = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # format: username:uid:Full Name (colon-separated, name may itself
        # contain colons - only the first field is guaranteed unambiguous)
        usernames.append(line.split(":", 1)[0])

    result["available"] = True
    result["usernames"] = sorted(usernames)
    return result


def set_password(username: str, password: str) -> dict[str, Any]:
    """Set (or create) a user's SMB password. The system account must
    already exist - use nas_monitor.users.create_user() first."""
    result: dict[str, Any] = {"username": username, "success": False}

    if not password:
        return errors.fail(result, "smb.empty_password")

    smbpasswd_path = system_tools.find_binary("smbpasswd")
    if smbpasswd_path is None:
        return errors.tool_missing(result, "smbpasswd")

    # -s reads the new password twice from stdin (not argv - keeps it out
    # of the process list) - see smbpasswd(8). -a adds the user to Samba's
    # user db if not already present, otherwise just changes the password.
    code, out, err = system_tools.run(
        [smbpasswd_path, "-s", "-a", username],
        input_text=f"{password}\n{password}\n",
    )
    if code != 0:
        return errors.command_failed(result, err, out, code, "smbpasswd")

    result["success"] = True
    return result


def remove_user(username: str) -> dict[str, Any]:
    """Remove a user's SMB access only - does not touch the system account."""
    result: dict[str, Any] = {"username": username, "success": False}

    smbpasswd_path = system_tools.find_binary("smbpasswd")
    if smbpasswd_path is None:
        return errors.tool_missing(result, "smbpasswd")

    code, out, err = system_tools.run([smbpasswd_path, "-x", username])
    if code != 0:
        return errors.command_failed(result, err, out, code, "smbpasswd")

    result["success"] = True
    return result
