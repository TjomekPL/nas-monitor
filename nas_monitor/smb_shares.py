"""
nas_monitor.smb_shares
------------------------
SMB share management. Shares created through this tool always live under
a single base directory (/srv, matching the common NAS convention e.g.
OpenMediaVault) and are defined in a dedicated, tool-owned config file
included from the main smb.conf - so the main file (which the admin may
hand-edit, with its own comments and formatting) is only ever touched
once, to add a single `include =` line, and never rewritten wholesale.

Every write to the managed file is validated with `testparm` against the
REAL effective configuration before being kept - on failure, the previous
content is restored and nothing is left broken. This is the one thing
that matters most in this module: a bad share definition must never be
able to take down every other share by breaking the whole config.
"""

from __future__ import annotations

import configparser
import grp
import os
import re
from typing import Any

from nas_monitor import system_tools
from nas_monitor import users as users_mod
from nas_monitor import smb as smb_mod

BASE_SHARE_PATH = "/srv"
MAIN_SMB_CONF = "/etc/samba/smb.conf"
MANAGED_CONF_DIR = "/etc/samba/smb.conf.d"
MANAGED_CONF_PATH = os.path.join(MANAGED_CONF_DIR, "nas-monitor-shares.conf")

# Reserved Samba section names that must never collide with a share name.
# testparm does not reject these - a share literally named "global" just
# silently merges its keys into the [global] section instead of erroring,
# which is a real footgun (confirmed by hand), not a hypothetical one.
_RESERVED_NAMES = {"global", "homes", "printers", "print$", "netlogon", "profiles"}

_VALID_SHARE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def is_valid_share_name(name: str) -> bool:
    return bool(_VALID_SHARE_NAME_RE.match(name)) and name not in _RESERVED_NAMES


def access_group_name(share_name: str) -> str:
    """Name of the group this module auto-manages for one share's access.
    Access is granted per-user in the UI/API - this group is the plumbing
    that makes that work at the Samba/filesystem level (valid users,
    force group, folder ownership), not something the admin manages
    directly."""
    return f"{share_name}_access"


def _missing_smb_password_warning(usernames: list[str]) -> str | None:
    """Being in a share's access group is not enough to log in - Samba
    also needs an actual SMB password for the account (smbpasswd). This
    is easy to miss for a pre-existing system account (e.g. the admin's
    own desktop login) that was never given one through this tool."""
    if not usernames:
        return None
    samba_info = smb_mod.list_samba_users()
    if not samba_info.get("available"):
        return None
    has_password = set(samba_info.get("usernames", []))
    missing = [u for u in usernames if u not in has_password]
    if not missing:
        return None
    return (
        f"Uwaga: {', '.join(missing)} nie ma jeszcze ustawionego hasła SMB - "
        f"nie będzie mógł się zalogować, dopóki nie ustawisz go w sekcji Użytkownicy."
    )


def is_installed() -> bool:
    return os.path.isfile(MAIN_SMB_CONF)


def share_path(name: str) -> str:
    return os.path.join(BASE_SHARE_PATH, name)


# --------------------------------------------------------------------------
# Reading/writing the managed shares file
# --------------------------------------------------------------------------

def _read_managed_shares(managed_conf_path: str = MANAGED_CONF_PATH) -> list[dict[str, Any]]:
    """Parse the tool-managed shares file. Empty/missing file -> []."""
    if not os.path.isfile(managed_conf_path):
        return []

    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(managed_conf_path)
    except configparser.Error:
        return []  # a hand-corrupted managed file shouldn't crash detection

    shares = []
    for name in cp.sections():
        section = cp[name]
        valid_users = section.get("valid users", "").split()
        raw_groups = [u[1:] for u in valid_users if u.startswith("@")]
        resolved_users = _resolve_group_members(raw_groups)
        shares.append(
            {
                "name": name,
                "path": section.get("path", ""),
                "comment": section.get("comment", ""),
                "read_only": section.get("read only", "no").strip().lower() in ("yes", "true", "1"),
                "users": resolved_users,
                "access_group": raw_groups[0] if raw_groups else None,
                "managed": True,
            }
        )
    return sorted(shares, key=lambda s: s["name"])


def _resolve_group_members(group_names: list[str]) -> list[str]:
    members: set[str] = set()
    for g in group_names:
        try:
            members.update(grp.getgrnam(g).gr_mem)
        except KeyError:
            continue
    return sorted(members)


def _render_managed_shares(shares: list[dict[str, Any]]) -> str:
    """Build the full text content of the managed shares file from scratch
    - this file is entirely tool-owned, so a full regenerate each time
    (rather than surgical edits) keeps this simple and hard to corrupt."""
    lines = [
        "# Zarzadzane przez nas-monitor - nie edytuj recznie, zmiany zostana nadpisane.",
        "",
    ]
    for s in shares:
        lines.append(f"[{s['name']}]")
        lines.append(f"   path = {s['path']}")
        if s.get("comment"):
            lines.append(f"   comment = {s['comment']}")
        lines.append(f"   read only = {'yes' if s.get('read_only') else 'no'}")
        lines.append("   browseable = yes")
        access_group = s.get("access_group")
        if access_group:
            lines.append(f"   valid users = @{access_group}")
            # force group makes newly created files belong to this group
            # regardless of which of the connecting user's OTHER secondary
            # groups the client happened to negotiate - more reliable than
            # depending on group inheritance alone.
            lines.append(f"   force group = @{access_group}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _ensure_include_directive(
    smb_conf_path: str = MAIN_SMB_CONF, managed_conf_path: str = MANAGED_CONF_PATH
) -> dict[str, Any]:
    """Make sure the main smb.conf includes our managed file. Appends a
    single line under [global] the first time only - never touches
    anything else in the (possibly hand-edited) main file."""
    result: dict[str, Any] = {"success": False, "error": None}

    os.makedirs(os.path.dirname(managed_conf_path), exist_ok=True)
    if not os.path.isfile(managed_conf_path):
        open(managed_conf_path, "a").close()

    try:
        with open(smb_conf_path, "r") as fh:
            content = fh.read()
    except OSError as exc:
        result["error"] = str(exc)
        return result

    include_line = f"   include = {managed_conf_path}"
    if managed_conf_path in content:
        result["success"] = True
        return result

    if "[global]" not in content:
        result["error"] = f"Nie znaleziono sekcji [global] w {smb_conf_path}"
        return result

    new_content = content.replace("[global]", f"[global]\n{include_line}", 1)
    try:
        with open(smb_conf_path, "w") as fh:
            fh.write(new_content)
    except OSError as exc:
        result["error"] = str(exc)
        return result

    result["success"] = True
    return result


def _validate_and_apply(
    new_content: str, managed_conf_path: str = MANAGED_CONF_PATH
) -> dict[str, Any]:
    """Write new_content to the managed file, validate the REAL effective
    config with testparm, and roll back to the previous content if
    validation fails. Nothing is left broken either way."""
    result: dict[str, Any] = {"success": False, "error": None}

    include_result = _ensure_include_directive(managed_conf_path=managed_conf_path)
    if not include_result["success"]:
        result["error"] = include_result["error"]
        return result

    try:
        with open(managed_conf_path, "r") as fh:
            previous_content = fh.read()
    except OSError:
        previous_content = ""

    try:
        with open(managed_conf_path, "w") as fh:
            fh.write(new_content)
    except OSError as exc:
        result["error"] = f"Nie udało się zapisać {managed_conf_path}: {exc}"
        return result

    testparm_path = system_tools.find_binary("testparm")
    if testparm_path is None:
        # restore - we can't validate, so we can't safely apply
        with open(managed_conf_path, "w") as fh:
            fh.write(previous_content)
        result["error"] = "testparm not installed - zmiana wycofana"
        return result

    code, out, err = system_tools.run([testparm_path, "-s"])
    if code != 0:
        with open(managed_conf_path, "w") as fh:
            fh.write(previous_content)
        result["error"] = f"testparm odrzucił zmianę, cofnięto: {(err or out).strip()[:300]}"
        return result

    reload_result = reload_smbd()
    if not reload_result["success"]:
        # config is valid and saved, but the running smbd wasn't told -
        # this is a soft failure, not a rollback case (config on disk is
        # correct, a manual/next restart will pick it up regardless)
        result["success"] = True
        result["warning"] = f"Konfiguracja zapisana, ale reload smbd nie powiódł się: {reload_result['error']}"
        return result

    result["success"] = True
    return result


def reload_smbd() -> dict[str, Any]:
    result: dict[str, Any] = {"success": False, "error": None}
    smbcontrol_path = system_tools.find_binary("smbcontrol")
    if smbcontrol_path is None:
        result["error"] = "smbcontrol not installed"
        return result
    code, out, err = system_tools.run([smbcontrol_path, "smbd", "reload-config"])
    if code != 0:
        result["error"] = err.strip() or f"smbcontrol exited {code}"
        return result
    result["success"] = True
    return result


# --------------------------------------------------------------------------
# Detection (main smb.conf + managed file, merged)
# --------------------------------------------------------------------------

def list_shares(
    main_conf_path: str = MAIN_SMB_CONF, managed_conf_path: str = MANAGED_CONF_PATH
) -> dict[str, Any]:
    """All shares Samba actually knows about: ones this tool manages, and
    any pre-existing ones defined directly in the main smb.conf (e.g. from
    manual setup before this tool existed) - the latter shown read-only-
    for-management-purposes, tagged managed=False, so nothing here is
    hidden just because this tool didn't create it."""
    result: dict[str, Any] = {"available": False, "shares": [], "error": None}

    if not os.path.isfile(main_conf_path):
        result["error"] = f"{main_conf_path} nie istnieje"
        return result

    managed = _read_managed_shares(managed_conf_path)
    managed_names = {s["name"] for s in managed}

    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(main_conf_path)
    except configparser.Error as exc:
        result["error"] = f"Nie udało się sparsować {main_conf_path}: {exc}"
        return result

    others = []
    for name in cp.sections():
        if name in _RESERVED_NAMES or name in managed_names:
            continue
        section = cp[name]
        valid_users = section.get("valid users", "").split()
        plain_users = [u for u in valid_users if not u.startswith("@")]
        group_refs = [u[1:] for u in valid_users if u.startswith("@")]
        resolved = sorted(set(plain_users) | set(_resolve_group_members(group_refs)))
        others.append(
            {
                "name": name,
                "path": section.get("path", ""),
                "comment": section.get("comment", ""),
                "read_only": section.get("read only", "yes").strip().lower() in ("yes", "true", "1"),
                "users": resolved,
                "access_group": None,
                "managed": False,
            }
        )

    result["available"] = True
    result["shares"] = sorted(managed + others, key=lambda s: s["name"])
    return result


# --------------------------------------------------------------------------
# Mutations - create / update / delete
# --------------------------------------------------------------------------

def _prepare_share_directory(path: str, group: str | None) -> dict[str, Any]:
    """Create the share folder if missing, and if a group is given, own it
    by that group with the setgid bit so new files inherit the group."""
    result: dict[str, Any] = {"success": False, "error": None}
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        result["error"] = f"Nie udało się utworzyć {path}: {exc}"
        return result

    if group:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError:
            result["error"] = f"Grupa '{group}' nie istnieje"
            return result
        try:
            os.chown(path, -1, gid)  # -1 = leave owner unchanged
            os.chmod(path, 0o2775)  # rwxrwsr-x - setgid so new files inherit the group
        except OSError as exc:
            result["error"] = f"Nie udało się ustawić uprawnień na {path}: {exc}"
            return result
    else:
        try:
            os.chmod(path, 0o755)
        except OSError as exc:
            result["error"] = f"Nie udało się ustawić uprawnień na {path}: {exc}"
            return result

    result["success"] = True
    return result


def create_share(
    name: str,
    comment: str = "",
    users: list[str] | None = None,
    read_only: bool = False,
    managed_conf_path: str = MANAGED_CONF_PATH,
) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "success": False, "error": None}

    if not is_valid_share_name(name):
        result["error"] = (
            "Nieprawidłowa nazwa udziału (litery/cyfry/_/-, musi zaczynać się "
            "literą, max 32 znaki, nie może być nazwą zarezerwowaną)"
        )
        return result

    existing = _read_managed_shares(managed_conf_path)
    if any(s["name"] == name for s in existing):
        result["error"] = f"Udział '{name}' już istnieje"
        return result

    users = users or []
    group = access_group_name(name) if users else None

    if group:
        for u in users:
            add_result = users_mod.add_user_to_group(u, group)
            if not add_result["success"]:
                result["error"] = f"Nie udało się dodać '{u}' do grupy dostępu: {add_result['error']}"
                return result

    path = share_path(name)
    dir_result = _prepare_share_directory(path, group)
    if not dir_result["success"]:
        result["error"] = dir_result["error"]
        return result

    new_share = {
        "name": name,
        "path": path,
        "comment": comment,
        "read_only": read_only,
        "access_group": group,
    }
    new_content = _render_managed_shares(existing + [new_share])
    apply_result = _validate_and_apply(new_content, managed_conf_path)
    if not apply_result["success"]:
        result["error"] = apply_result["error"]
        return result

    result["success"] = True
    result["path"] = path
    result["users"] = users
    warnings = [w for w in [apply_result.get("warning"), _missing_smb_password_warning(users)] if w]
    if warnings:
        result["warning"] = " ".join(warnings)
    return result


def update_share(
    name: str,
    comment: str | None = None,
    users: list[str] | None = None,
    read_only: bool | None = None,
    managed_conf_path: str = MANAGED_CONF_PATH,
) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "success": False, "error": None}

    existing = _read_managed_shares(managed_conf_path)
    match = next((s for s in existing if s["name"] == name), None)
    if match is None:
        result["error"] = f"Udział '{name}' nie istnieje (albo nie jest zarządzany przez to narzędzie)"
        return result

    if comment is not None:
        match["comment"] = comment
    if read_only is not None:
        match["read_only"] = read_only

    if users is not None:
        dedicated_group = access_group_name(name)
        existing_group = match.get("access_group")
        if existing_group and existing_group != dedicated_group:
            # This share was created before per-user access existed (an
            # old single-group picker let it point at ANY group, e.g.
            # someone's own personal account group) - never diff
            # membership against a group we don't own. Migrate forward:
            # start counting membership as empty in our own dedicated
            # group, without touching the old group's membership at all.
            current_users = set()
        else:
            current_users = set(_resolve_group_members([dedicated_group]))
        group = dedicated_group
        desired_users = set(users)

        for u in desired_users - current_users:
            add_result = users_mod.add_user_to_group(u, group)
            if not add_result["success"]:
                result["error"] = f"Nie udało się dodać '{u}' do grupy dostępu: {add_result['error']}"
                return result
        for u in current_users - desired_users:
            remove_result = users_mod.remove_user_from_group(u, group)
            if not remove_result["success"]:
                result["error"] = f"Nie udało się usunąć '{u}' z grupy dostępu: {remove_result['error']}"
                return result

        match["access_group"] = group if desired_users else None
        if desired_users:
            dir_result = _prepare_share_directory(match["path"], group)
            if not dir_result["success"]:
                result["error"] = dir_result["error"]
                return result

    new_content = _render_managed_shares(existing)
    apply_result = _validate_and_apply(new_content, managed_conf_path)
    if not apply_result["success"]:
        result["error"] = apply_result["error"]
        return result

    result["success"] = True
    warnings = [w for w in [apply_result.get("warning"), _missing_smb_password_warning(users or [])] if w]
    if warnings:
        result["warning"] = " ".join(warnings)
    return result


def delete_share(
    name: str, delete_files: bool = False, managed_conf_path: str = MANAGED_CONF_PATH
) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "success": False, "error": None}

    existing = _read_managed_shares(managed_conf_path)
    match = next((s for s in existing if s["name"] == name), None)
    if match is None:
        result["error"] = f"Udział '{name}' nie istnieje (albo nie jest zarządzany przez to narzędzie)"
        return result

    remaining = [s for s in existing if s["name"] != name]
    new_content = _render_managed_shares(remaining)
    apply_result = _validate_and_apply(new_content, managed_conf_path)
    if not apply_result["success"]:
        result["error"] = apply_result["error"]
        return result

    access_group = match.get("access_group")
    if access_group and access_group == access_group_name(name):
        # Only ever delete a group WE created with our own naming
        # convention. A share made through the old single-group picker
        # (before this became per-user) can point at an arbitrary
        # existing group - e.g. someone's own personal account group -
        # and that must never be touched here.
        groupdel_path = system_tools.find_binary("groupdel")
        if groupdel_path is not None:
            system_tools.run([groupdel_path, access_group])  # best-effort, ignore result

    if delete_files:
        import shutil as _shutil

        try:
            _shutil.rmtree(match["path"])
        except OSError as exc:
            result["success"] = True
            result["warning"] = f"Udział usunięty z Samby, ale nie udało się skasować {match['path']}: {exc}"
            return result

    result["success"] = True
    return result
