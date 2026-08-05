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

from nas_monitor import system_tools, errors
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


def _missing_smb_password_warning(usernames: list[str]) -> dict[str, Any] | None:
    """Being in a share's access group is not enough to log in - Samba
    also needs an actual SMB password for the account (smbpasswd). This
    is easy to miss for a pre-existing system account (e.g. the admin's
    own desktop login) that was never given one through this tool.
    Returns a warning {"code", "context"} dict, or None if nothing to
    warn about."""
    if not usernames:
        return None
    samba_info = smb_mod.list_samba_users()
    if not samba_info.get("available"):
        return None
    has_password = set(samba_info.get("usernames", []))
    missing = [u for u in usernames if u not in has_password]
    if not missing:
        return None
    return {"code": "shares.missing_smb_password", "context": {"usernames": ", ".join(missing)}}


def is_installed() -> bool:
    return os.path.isfile(MAIN_SMB_CONF)


def _is_group_ref(token: str) -> bool:
    """Samba treats a leading '@' or '+' as marking a group reference in
    valid users / read list / write list (see _render_managed_shares for
    why this tool writes '+', but a hand-edited or pre-existing share
    could use either)."""
    return token.startswith("@") or token.startswith("+")


def _group_ref_name(token: str) -> str:
    return token[1:]


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
        group_refs = [_group_ref_name(u) for u in valid_users if _is_group_ref(u)]
        # The share's own dedicated group (<name>_access) is distinct
        # from any GENERAL group also granted access - the former backs
        # per-user checkboxes (unchanged from before), the latter is a
        # separate, explicit grant that doesn't get flattened into
        # "permissions" the way individual users do.
        dedicated_group = access_group_name(name)
        access_group = dedicated_group if dedicated_group in group_refs else None
        # Only trust OTHER group refs as genuine general-group grants once
        # the dedicated group itself is recognized - a share whose access
        # group doesn't match our naming convention at all predates this
        # feature (see the legacy-migration handling elsewhere in this
        # module) and its other group refs are equally foreign; sweeping
        # them into group_grants would let this tool start managing ACLs
        # on a group it never created and knows nothing about.
        granted_group_names = [g for g in group_refs if g != dedicated_group] if access_group else []

        resolved_users = _resolve_group_members([access_group]) if access_group else []

        read_list_raw = section.get("read list", "").split()
        read_list_group_refs = [_group_ref_name(g) for g in read_list_raw if _is_group_ref(g)]
        read_only_users = set(u for u in read_list_raw if not _is_group_ref(u))
        if access_group in read_list_group_refs:
            read_only_users |= set(_resolve_group_members([access_group]))

        permissions = {u: ("ro" if u in read_only_users else "rw") for u in resolved_users}
        group_grants = {g: ("ro" if g in read_list_group_refs else "rw") for g in granted_group_names}

        shares.append(
            {
                "name": name,
                "path": section.get("path", ""),
                "comment": section.get("comment", ""),
                "permissions": permissions,
                "group_grants": group_grants,
                "access_group": access_group,
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
        # Always writable at the share level - per-user restriction to
        # read-only happens via "read list" below, which lets RW and RO
        # users coexist on the same share (OMV-style per-user access).
        lines.append("   read only = no")
        lines.append("   browseable = yes")
        access_group = s.get("access_group")
        permissions = s.get("permissions") or {}
        group_grants = s.get("group_grants") or {}
        valid_users_tokens = []
        if access_group:
            # '+group' checks the UNIX group database only, skipping the
            # NIS-netgroup-first lookup that plain '@group' does - on
            # systems where that NIS pre-check misbehaves (even with no
            # NIS actually configured), '@group' can fail with
            # NT_STATUS_NO_SUCH_GROUP even though the group genuinely
            # exists and `getent group` finds it fine. Confirmed against
            # a real failure, not theoretical.
            valid_users_tokens.append(f"+{access_group}")
        valid_users_tokens.extend(f"+{g}" for g in sorted(group_grants))
        if valid_users_tokens:
            lines.append(f"   valid users = {' '.join(valid_users_tokens)}")
        if access_group:
            # force group takes a bare group name - no @/+ prefix syntax
            # applies here, there's no user/group ambiguity to resolve.
            # Always the share's OWN dedicated group, regardless of how
            # many other groups are ALSO granted access above - this is
            # just "who owns newly created files", not "who can access
            # them", so it doesn't need to (and can't) name more than one.
            lines.append(f"   force group = {access_group}")
        read_list_tokens = sorted(u for u, level in permissions.items() if level == "ro")
        read_list_tokens.extend(f"+{g}" for g, level in sorted(group_grants.items()) if level == "ro")
        if read_list_tokens:
            lines.append(f"   read list = {' '.join(read_list_tokens)}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _ensure_include_directive(
    smb_conf_path: str = MAIN_SMB_CONF, managed_conf_path: str = MANAGED_CONF_PATH
) -> dict[str, Any]:
    """Make sure the main smb.conf includes our managed file. Appends a
    single line under [global] the first time only - never touches
    anything else in the (possibly hand-edited) main file."""
    result: dict[str, Any] = {"success": False}

    os.makedirs(os.path.dirname(managed_conf_path), exist_ok=True)
    if not os.path.isfile(managed_conf_path):
        open(managed_conf_path, "a").close()

    try:
        with open(smb_conf_path, "r") as fh:
            content = fh.read()
    except OSError as exc:
        return errors.io_failed(result, exc, smb_conf_path)

    include_line = f"   include = {managed_conf_path}"
    if managed_conf_path in content:
        result["success"] = True
        return result

    if "[global]" not in content:
        return errors.fail(result, "shares.global_section_missing", path=smb_conf_path)

    new_content = content.replace("[global]", f"[global]\n{include_line}", 1)
    try:
        with open(smb_conf_path, "w") as fh:
            fh.write(new_content)
    except OSError as exc:
        return errors.io_failed(result, exc, smb_conf_path)

    result["success"] = True
    return result


def _validate_and_apply(
    new_content: str, managed_conf_path: str = MANAGED_CONF_PATH
) -> dict[str, Any]:
    """Write new_content to the managed file, validate the REAL effective
    config with testparm, and roll back to the previous content if
    validation fails. Nothing is left broken either way."""
    result: dict[str, Any] = {"success": False}

    include_result = _ensure_include_directive(managed_conf_path=managed_conf_path)
    if not include_result["success"]:
        return errors.propagate(result, include_result)

    try:
        with open(managed_conf_path, "r") as fh:
            previous_content = fh.read()
    except OSError:
        previous_content = ""

    try:
        with open(managed_conf_path, "w") as fh:
            fh.write(new_content)
    except OSError as exc:
        return errors.io_failed(result, exc, managed_conf_path)

    testparm_path = system_tools.find_binary("testparm")
    if testparm_path is None:
        # restore - we can't validate, so we can't safely apply
        with open(managed_conf_path, "w") as fh:
            fh.write(previous_content)
        return errors.fail(result, "shares.validate_tool_missing", tool="testparm")

    code, out, err = system_tools.run([testparm_path, "-s"])
    if code != 0:
        with open(managed_conf_path, "w") as fh:
            fh.write(previous_content)
        return errors.fail(result, "shares.config_rejected", detail=(err or out).strip()[:300])

    reload_result = reload_smbd()
    if not reload_result["success"]:
        # config is valid and saved, but the running smbd wasn't told -
        # this is a soft failure, not a rollback case (config on disk is
        # correct, a manual/next restart will pick it up regardless)
        result["success"] = True
        errors.warn(
            result,
            "shares.reload_failed",
            reload_error_code=reload_result.get("error_code"),
            **(reload_result.get("error_context") or {}),
        )
        return result

    result["success"] = True
    return result


def reload_smbd() -> dict[str, Any]:
    result: dict[str, Any] = {"success": False}
    smbcontrol_path = system_tools.find_binary("smbcontrol")
    if smbcontrol_path is None:
        return errors.tool_missing(result, "smbcontrol")
    code, out, err = system_tools.run([smbcontrol_path, "smbd", "reload-config"])
    if code != 0:
        return errors.command_failed(result, err, out, code, "smbcontrol")
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
    result: dict[str, Any] = {"available": False, "shares": []}

    if not os.path.isfile(main_conf_path):
        return errors.fail(result, "shares.main_conf_missing", path=main_conf_path)

    managed = _read_managed_shares(managed_conf_path)
    managed_names = {s["name"] for s in managed}

    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(main_conf_path)
    except configparser.Error as exc:
        return errors.fail(result, "shares.main_conf_parse_failed", path=main_conf_path, detail=str(exc))

    others = []
    for name in cp.sections():
        if name in _RESERVED_NAMES or name in managed_names:
            continue
        section = cp[name]
        valid_users = section.get("valid users", "").split()
        plain_users = [u for u in valid_users if not _is_group_ref(u)]
        group_refs = [_group_ref_name(u) for u in valid_users if _is_group_ref(u)]
        resolved = sorted(set(plain_users) | set(_resolve_group_members(group_refs)))

        share_read_only = section.get("read only", "yes").strip().lower() in ("yes", "true", "1")
        read_list_raw = section.get("read list", "").split()
        explicit_ro = set(u for u in read_list_raw if not _is_group_ref(u))
        explicit_ro |= set(_resolve_group_members([_group_ref_name(g) for g in read_list_raw if _is_group_ref(g)]))
        write_list_raw = section.get("write list", "").split()
        explicit_rw = set(u for u in write_list_raw if not _is_group_ref(u))
        explicit_rw |= set(_resolve_group_members([_group_ref_name(g) for g in write_list_raw if _is_group_ref(g)]))

        permissions = {}
        for u in resolved:
            if u in explicit_ro:
                permissions[u] = "ro"
            elif u in explicit_rw:
                permissions[u] = "rw"
            else:
                permissions[u] = "ro" if share_read_only else "rw"

        others.append(
            {
                "name": name,
                "path": section.get("path", ""),
                "comment": section.get("comment", ""),
                "permissions": permissions,
                "group_grants": {},
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
    result: dict[str, Any] = {"success": False}
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return errors.io_failed(result, exc, path)

    if group:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError:
            return errors.fail(result, "shares.group_not_found", group=group)
        try:
            os.chown(path, -1, gid)  # -1 = leave owner unchanged
            os.chmod(path, 0o2775)  # rwxrwsr-x - setgid so new files inherit the group
        except OSError as exc:
            return errors.io_failed(result, exc, path)
    else:
        try:
            os.chmod(path, 0o755)
        except OSError as exc:
            return errors.io_failed(result, exc, path)

    result["success"] = True
    return result


def _set_group_acl(path: str, group: str, level: str) -> dict[str, Any]:
    """Grants a general group real filesystem access to a share
    directory via POSIX ACLs - on top of the share's own dedicated
    access group (which owns the directory outright via 'force group').

    Samba's valid users/read list only gates the SMB PROTOCOL
    connection - the underlying filesystem check still applies
    (security = user means smbd impersonates the real UID), and that
    only respects ONE owning group per file (chown/chmod can't express
    "these two different groups both get write access"). ACLs can.

    -R applies recursively to whatever's already in the directory; a
    second call with -d sets the DEFAULT ACL too, so files and
    subdirectories created *after* this runs inherit it automatically -
    the ACL equivalent of what the setgid bit already does for the
    dedicated access group."""
    result: dict[str, Any] = {"success": False}

    setfacl_path = system_tools.find_binary("setfacl")
    if setfacl_path is None:
        return errors.tool_missing(result, "setfacl")

    perm = "rwx" if level == "rw" else "r-x"
    code, out, err = system_tools.run([setfacl_path, "-R", "-m", f"g:{group}:{perm}", path])
    if code != 0:
        return errors.command_failed(result, err, out, code, "setfacl")

    code, out, err = system_tools.run([setfacl_path, "-R", "-d", "-m", f"g:{group}:{perm}", path])
    if code != 0:
        return errors.command_failed(result, err, out, code, "setfacl")

    result["success"] = True
    return result


def _remove_group_acl(path: str, group: str) -> dict[str, Any]:
    """Best-effort removal - if the group's ACL entry was never actually
    set (e.g. setfacl was missing when it was granted), there's nothing
    to clean up and that's fine, not a failure worth blocking on."""
    result: dict[str, Any] = {"success": True}

    setfacl_path = system_tools.find_binary("setfacl")
    if setfacl_path is None:
        return result

    system_tools.run([setfacl_path, "-R", "-x", f"g:{group}", path])
    system_tools.run([setfacl_path, "-R", "-d", "-x", f"g:{group}", path])
    return result


def _sync_group_acls(path: str, desired_grants: dict[str, str], current_grants: dict[str, str]) -> dict[str, Any]:
    """Reconciles filesystem ACLs with the desired group grants for a
    share: removes groups no longer granted, (re)applies ACLs for
    groups that are granted or whose level changed. Returns success
    even if some individual ACL calls failed (each failure becomes a
    warning, not a hard stop) - a share's core SMB-level access already
    succeeded by the time this runs, and a missing ACL tool shouldn't
    undo that; it should be visible as a warning instead."""
    result: dict[str, Any] = {"success": True, "warnings": []}

    for group in set(current_grants) - set(desired_grants):
        _remove_group_acl(path, group)

    for group, level in desired_grants.items():
        if current_grants.get(group) == level:
            continue
        acl_result = _set_group_acl(path, group, level)
        if not acl_result["success"]:
            result["warnings"].append({"code": "shares.group_acl_failed", "context": {"group": group, "detail": acl_result.get("error_context", {}).get("detail", "")}})

    return result


def create_share(
    name: str,
    comment: str = "",
    permissions: dict[str, str] | None = None,
    group_grants: dict[str, str] | None = None,
    managed_conf_path: str = MANAGED_CONF_PATH,
) -> dict[str, Any]:
    """permissions maps username -> "rw" or "ro" (individual grants, via
    the share's own dedicated access group). group_grants maps a
    GENERAL group name -> "rw" or "ro" - a live binding (Samba resolves
    group membership itself; adding someone to the group later gives
    them access without touching the share again), backed by both an
    extra +group entry in valid users (protocol-level) AND a POSIX ACL
    on the directory (filesystem-level - see _set_group_acl for why
    both are required)."""
    result: dict[str, Any] = {"name": name, "success": False}

    if not is_valid_share_name(name):
        return errors.fail(result, "shares.invalid_name")

    existing = _read_managed_shares(managed_conf_path)
    if any(s["name"] == name for s in existing):
        return errors.fail(result, "shares.already_exists", name=name)

    permissions = dict(permissions or {})
    for u, level in permissions.items():
        if level not in ("rw", "ro"):
            return errors.fail(result, "shares.invalid_permission_level", user=u, level=str(level))

    group_grants = dict(group_grants or {})
    for g, level in group_grants.items():
        if level not in ("rw", "ro"):
            return errors.fail(result, "shares.invalid_permission_level", user=g, level=str(level))
        try:
            grp.getgrnam(g)
        except KeyError:
            return errors.fail(result, "shares.group_not_found", group=g)

    group = access_group_name(name) if (permissions or group_grants) else None

    if group:
        for u in permissions:
            add_result = users_mod.add_user_to_group(u, group)
            if not add_result["success"]:
                return errors.propagate(result, add_result, user=u, group=group)

    path = share_path(name)
    dir_result = _prepare_share_directory(path, group)
    if not dir_result["success"]:
        return errors.propagate(result, dir_result)

    acl_warnings = []
    if group_grants:
        acl_result = _sync_group_acls(path, group_grants, {})
        acl_warnings = acl_result.get("warnings") or []

    new_share = {
        "name": name,
        "path": path,
        "comment": comment,
        "permissions": permissions,
        "group_grants": group_grants,
        "access_group": group,
    }
    new_content = _render_managed_shares(existing + [new_share])
    apply_result = _validate_and_apply(new_content, managed_conf_path)
    if not apply_result["success"]:
        return errors.propagate(result, apply_result)

    result["success"] = True
    result["path"] = path
    result["permissions"] = permissions
    result["group_grants"] = group_grants
    warnings = list(apply_result.get("warnings") or []) + acl_warnings
    pw_warning = _missing_smb_password_warning(list(permissions))
    if pw_warning:
        warnings.append(pw_warning)
    if warnings:
        result["warnings"] = warnings
    return result


def update_share(
    name: str,
    comment: str | None = None,
    permissions: dict[str, str] | None = None,
    group_grants: dict[str, str] | None = None,
    managed_conf_path: str = MANAGED_CONF_PATH,
) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "success": False}

    existing = _read_managed_shares(managed_conf_path)
    match = next((s for s in existing if s["name"] == name), None)
    if match is None:
        return errors.fail(result, "shares.not_found", name=name)

    if comment is not None:
        match["comment"] = comment

    if permissions is not None:
        for u, level in permissions.items():
            if level not in ("rw", "ro"):
                return errors.fail(result, "shares.invalid_permission_level", user=u, level=str(level))

    if group_grants is not None:
        for g, level in group_grants.items():
            if level not in ("rw", "ro"):
                return errors.fail(result, "shares.invalid_permission_level", user=g, level=str(level))
            try:
                grp.getgrnam(g)
            except KeyError:
                return errors.fail(result, "shares.group_not_found", group=g)

    acl_warnings = []
    if permissions is not None or group_grants is not None:
        dedicated_group = access_group_name(name)
        existing_group = match.get("access_group")
        if existing_group and existing_group != dedicated_group:
            # This share was created before per-user access existed (an
            # old single-group picker let it point at ANY group, e.g.
            # someone's own personal account group) - never diff
            # membership against a group we don't own. Migrate forward:
            # start counting membership as empty in our own dedicated
            # group, without touching the old group's membership at all.
            current_users: set[str] = set()
        else:
            current_users = set(match.get("permissions") or {})
        current_group_grants = dict(match.get("group_grants") or {})

        desired_users = set(permissions) if permissions is not None else current_users
        desired_group_grants = group_grants if group_grants is not None else current_group_grants
        group = dedicated_group

        if permissions is not None:
            for u in desired_users - current_users:
                add_result = users_mod.add_user_to_group(u, group)
                if not add_result["success"]:
                    return errors.propagate(result, add_result, user=u, group=group)
            for u in current_users - desired_users:
                remove_result = users_mod.remove_user_from_group(u, group)
                if not remove_result["success"]:
                    return errors.propagate(result, remove_result, user=u, group=group)
            match["permissions"] = permissions

        has_any_access = bool(desired_users) or bool(desired_group_grants)
        match["access_group"] = group if has_any_access else None

        if has_any_access:
            dir_result = _prepare_share_directory(match["path"], group)
            if not dir_result["success"]:
                return errors.propagate(result, dir_result)

        if group_grants is not None:
            acl_result = _sync_group_acls(match["path"], desired_group_grants, current_group_grants)
            acl_warnings = acl_result.get("warnings") or []
            match["group_grants"] = desired_group_grants

    new_content = _render_managed_shares(existing)
    apply_result = _validate_and_apply(new_content, managed_conf_path)
    if not apply_result["success"]:
        return errors.propagate(result, apply_result)

    result["success"] = True
    warnings = list(apply_result.get("warnings") or []) + acl_warnings
    pw_warning = _missing_smb_password_warning(list(permissions or {}))
    if pw_warning:
        warnings.append(pw_warning)
    if warnings:
        result["warnings"] = warnings
    return result


def delete_share(
    name: str, delete_files: bool = False, managed_conf_path: str = MANAGED_CONF_PATH
) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "success": False}

    existing = _read_managed_shares(managed_conf_path)
    match = next((s for s in existing if s["name"] == name), None)
    if match is None:
        return errors.fail(result, "shares.not_found", name=name)

    remaining = [s for s in existing if s["name"] != name]
    new_content = _render_managed_shares(remaining)
    apply_result = _validate_and_apply(new_content, managed_conf_path)
    if not apply_result["success"]:
        return errors.propagate(result, apply_result)

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

    for granted_group in (match.get("group_grants") or {}):
        _remove_group_acl(match["path"], granted_group)  # best-effort, ignore result

    if delete_files:
        import shutil as _shutil

        try:
            _shutil.rmtree(match["path"])
        except OSError as exc:
            result["success"] = True
            errors.warn(result, "shares.file_delete_failed", path=match["path"], detail=str(exc))
            return result

    result["success"] = True
    return result
