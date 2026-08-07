"""
nas_monitor.app
----------------
Flask app, all routes. Every mutating endpoint returns machine-readable
error_code/error_context (and, for soft failures, warning_code/context)
instead of pre-composed prose - see nas_monitor/errors.py. The frontend
(nas_monitor/static/i18n/{pl,en}.js) owns all user-facing text, so this
file and everything it calls never needs to know which language the
dashboard is showing.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from nas_monitor import monitor, users, smb, smb_shares, ssh_keys, network, network_mutate, oplog, auth, system_stats, update_manager, disk_mutate, layout

# Every account that gets SMB access lands here by default - removable
# afterward like any other group (just uncheck it in the edit form).
# What makes this worth doing at all: general groups can now be granted
# real share access (see smb_shares.py's group_grants), so "everyone
# with SMB access" becomes a genuine one-click group to hand a share to,
# instead of a label nobody ever populates.
DEFAULT_SMB_GROUP = "smb_users"

app = Flask(__name__)
# nginx (see nginx/nas-monitor.conf) is the only thing gunicorn ever
# talks to now - it sits at 127.0.0.1, fronted by nginx doing TLS
# termination. Without this, request.remote_addr would be nginx's own
# address (127.0.0.1) for every single request, which would make the
# fail2ban-facing failed-login log (auth.log_failed_login_attempt) and
# the operations log both useless for telling requests apart by source.
# x_for=1 / x_proto=1: trust exactly one hop of X-Forwarded-* headers -
# there's exactly one proxy (nginx) in front of this app, never more.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.secret_key = auth.get_or_create_secret_key()
# Not Strict: Strict would also drop the cookie on a plain top-level link
# click into the dashboard from outside it (e.g. a bookmark opened in a
# new tab counts as "cross-site" to some browsers' Strict handling),
# which would just look like a random extra login prompt for no reason.
# Lax still blocks the case that actually matters here - a cross-site
# POST (the only way any state-changing endpoint in this app is ever
# reached) - since Lax only forwards cookies on top-level GET navigation.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
network_mutate.check_and_recover_on_startup()


@app.before_request
def require_login():
    if not auth.auth_enabled():
        return None
    if not auth.is_configured():
        # Setup never ran (e.g. install.sh's prompt was skipped) - don't
        # lock the admin out of a tool that has no configured way to log
        # into yet. Once credentials exist, this branch stops applying.
        return None
    if request.endpoint in ("login", "static"):
        return None
    if session.get("authenticated"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error_code": "auth.login_required", "error_context": {}}), 401
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    locked, seconds_remaining = auth.is_locked_out(username)
    if locked:
        oplog.log_event("auth", "login", "failure", params={"username": username, "reason": "locked_out"})
        return jsonify({"success": False, "error_code": "auth.locked_out", "error_context": {"seconds": seconds_remaining}}), 429

    if not auth.verify_credentials(username, password):
        auth.record_failed_login(username)
        auth.log_failed_login_attempt(username, request.remote_addr)
        oplog.log_event("auth", "login", "failure", params={"username": username})
        return jsonify({"success": False, "error_code": "auth.invalid_credentials", "error_context": {}}), 401

    auth.clear_login_attempts(username)
    session.clear()
    session["authenticated"] = True
    session["username"] = username
    duration = auth.get_session_duration_minutes()
    if duration is not None:
        session.permanent = True
        app.permanent_session_lifetime = timedelta(minutes=duration)
    else:
        session.permanent = False  # a plain session cookie - gone when the browser closes

    oplog.log_event("auth", "login", "success", params={"username": username})
    return jsonify({"success": True})


@app.route("/logout", methods=["POST"])
def logout():
    username = session.get("username", "")
    session.clear()
    oplog.log_event("auth", "logout", "success", params={"username": username})
    return jsonify({"success": True})


@app.route("/api/auth/status")
def api_auth_status():
    return jsonify(
        {
            "enabled": auth.auth_enabled(),
            "configured": auth.is_configured(),
            "authenticated": bool(session.get("authenticated")),
            "username": auth.get_username(),
            "session_duration_minutes": auth.get_session_duration_minutes(),
        }
    )


@app.route("/api/auth/change-password", methods=["POST"])
def api_auth_change_password():
    data = request.get_json(force=True, silent=True) or {}
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""

    result = auth.change_password(current_password, new_password)
    if not result["success"]:
        oplog.log_event("auth", "change_password", "failure", params={})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400

    oplog.log_event("auth", "change_password", "success", params={})
    return jsonify({"success": True})


@app.route("/api/auth/session-duration", methods=["POST"])
def api_auth_session_duration():
    data = request.get_json(force=True, silent=True) or {}
    minutes = data.get("minutes")  # null/None = until the browser closes

    result = auth.set_session_duration_minutes(minutes)
    if not result["success"]:
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    return jsonify({"success": True, "session_duration_minutes": result["session_duration_minutes"]})


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/status")
def api_status():
    return jsonify(monitor.get_full_status())


@app.route("/api/disks/manageable")
def api_disks_manageable():
    return jsonify({"disks": disk_mutate.list_manageable_disks()})


@app.route("/api/layout/<section>")
def api_layout_get(section):
    return jsonify({"order": layout.get_order(section)})


@app.route("/api/layout/<section>", methods=["POST"])
def api_layout_set(section):
    data = request.get_json(force=True, silent=True) or {}
    order = data.get("order")
    if not isinstance(order, list):
        return jsonify({"success": False, "error_code": "layout.invalid_order", "error_context": {}}), 400
    result = layout.set_order(section, order)
    if not result["success"]:
        return jsonify({"success": False, "error_code": "system.io_failed", "error_context": {"detail": result.get("error") or ""}}), 400
    return jsonify({"success": True})


@app.route("/api/disks/<name>/smart")
def api_disk_smart(name):
    # On-demand only ("Sprawdź stan" button) - unlike the known/mounted
    # disks in /api/status, raw disks aren't worth polling smartctl for
    # continuously since nothing's using them yet.
    known = {d["name"]: d for d in monitor.list_disks()}
    if name not in known:
        return jsonify({"available": False, "error": "not found"}), 404
    smart = monitor.get_smart_health(known[name]["path"])
    smart["health"] = monitor.classify_health(smart)
    return jsonify(smart)


@app.route("/api/disks/<name>/format", methods=["POST"])
def api_disk_format(name):
    data = request.get_json(force=True, silent=True) or {}
    filesystem = (data.get("filesystem") or "").strip()
    label = (data.get("label") or "").strip()
    auto_mount = data.get("auto_mount", True)
    device = f"/dev/{name}"

    result = disk_mutate.format_disk(device, filesystem, label=label, auto_mount=auto_mount)
    if not result["success"]:
        oplog.log_event("disks", "format", "failure", params={"device": device, "filesystem": filesystem})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event(
        "disks", "format", "success",
        params={"device": device, "filesystem": filesystem, "label": label, "mount_point": result.get("mount_point")},
    )
    return jsonify({"success": True, "mount_point": result.get("mount_point"), "warnings": result.get("warnings", [])})


@app.route("/api/disks/<name>/wipe", methods=["POST"])
def api_disk_wipe(name):
    device = f"/dev/{name}"
    result = disk_mutate.wipe_disk(device)
    if not result["success"]:
        oplog.log_event("disks", "wipe", "failure", params={"device": device})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("disks", "wipe", "success", params={"device": device})
    return jsonify({"success": True})


@app.route("/api/disks/<name>/mount", methods=["POST"])
def api_disk_mount(name):
    data = request.get_json(force=True, silent=True) or {}
    label = (data.get("label") or "").strip()
    device = f"/dev/{name}"
    result = disk_mutate.mount_disk(device, label=label)
    if not result["success"]:
        oplog.log_event("disks", "mount", "failure", params={"device": device})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("disks", "mount", "success", params={"device": device, "mount_point": result["mount_point"]})
    return jsonify({"success": True, "mount_point": result["mount_point"]})


def _shares_blocking_unmount(mount_point: str) -> list[str]:
    """Names of shares whose path is this exact mount point or lives
    somewhere underneath it - what api_disk_unmount refuses to unmount
    over (or, once confirmed, deletes). A pure lookup (no side effects)
    so it's testable without needing the whole Flask request/response
    cycle around it."""
    shares_result = smb_shares.list_shares()
    return [
        s["name"] for s in shares_result.get("shares", [])
        if s["path"] == mount_point or s["path"].startswith(mount_point + "/")
    ]


@app.route("/api/disks/<name>/unmount", methods=["POST"])
def api_disk_unmount(name):
    device = f"/dev/{name}"
    data = request.get_json(force=True, silent=True) or {}
    delete_blocking_shares = bool(data.get("delete_blocking_shares", False))

    # Check for shares depending on this mount point BEFORE unmounting -
    # not something disk_mutate.unmount_disk itself can check (it has
    # no reason to know shares exist, and importing smb_shares there
    # would create a circular import the other way around).
    #
    # His explicit preference (not OMV's separate multi-step gate, and
    # not silently letting a share start pointing at nothing either):
    # warn what's about to be deleted, and on confirmation actually
    # delete the blocking share(s) - Samba definition + access group
    # only, files preserved, same as any other share deletion - THEN
    # unmount, in one action. Without delete_blocking_shares=true this
    # still just refuses with the exact list, so a stale/missing
    # frontend warning (see unmountDisk's own client-side check) can
    # never silently delete something nobody was told about - the
    # server independently re-verifies this every time.
    manageable_by_name = {d["name"]: d for d in disk_mutate.list_manageable_disks()}
    mount_point = manageable_by_name.get(name, {}).get("mount_point")
    deleted_shares = []
    unmount_warnings = []
    if mount_point:
        blocking = _shares_blocking_unmount(mount_point)
        if blocking:
            if not delete_blocking_shares:
                oplog.log_event("disks", "unmount", "failure", params={"device": device})
                return jsonify({
                    "success": False,
                    "error_code": "disks.unmount_blocked_by_shares",
                    "error_context": {"shares": ", ".join(blocking)},
                }), 400
            for share_name in blocking:
                del_result = smb_shares.delete_share(share_name, delete_files=False)
                if del_result["success"]:
                    deleted_shares.append(share_name)
                    oplog.log_event("shares", "delete", "success", params={"name": share_name, "reason": "disk_unmount"})
                    # e.g. groupdel failing to remove the share's own
                    # access group - forwarded to the client the same
                    # way a normal (non-cascaded) share delete already
                    # does, so this doesn't quietly drop something
                    # worth knowing about just because it happened as
                    # part of an unmount instead of a direct delete.
                    for w in del_result.get("warnings", []):
                        unmount_warnings.append(w)
                else:
                    oplog.log_event("shares", "delete", "failure", params={"name": share_name, "reason": "disk_unmount"})

    result = disk_mutate.unmount_disk(device)
    if not result["success"]:
        oplog.log_event("disks", "unmount", "failure", params={"device": device})
        return jsonify({
            "success": False,
            "error_code": result["error_code"],
            "error_context": result["error_context"],
            "deleted_shares": deleted_shares,
        }), 400
    oplog.log_event("disks", "unmount", "success", params={"device": device, "deleted_shares": ", ".join(deleted_shares)})
    return jsonify({"success": True, "deleted_shares": deleted_shares, "warnings": unmount_warnings})


@app.route("/api/system-stats")
def api_system_stats():
    # Deliberately not logged to the operations log - this is a
    # read-only poll firing every couple of seconds for the statusbar,
    # not a user-initiated action, and would drown out everything else
    # in the Log tab.
    return jsonify(system_stats.get_live_stats())


@app.route("/api/update/check")
def api_update_check():
    return jsonify(update_manager.check_for_update())


@app.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    result = update_manager.apply_update()
    if not result.get("success"):
        oplog.log_event("update", "apply", "failure", params={})
        return jsonify(result), 400
    oplog.log_event("update", "apply", "success", params={"version": result.get("version") or "?"})
    return jsonify(result)


def _general_groups():
    """Groups a human should ever see/manage directly - excludes the
    <share>_access groups this tool auto-manages from the Udziały
    section. Showing those here would invite exactly the kind of
    accidental un-sharing (or accidental deletion, for the Groups tab)
    that this filter exists to prevent."""
    share_access_groups = {
        s["access_group"] for s in smb_shares.list_shares().get("shares", []) if s.get("access_group")
    }
    return [g for g in users.list_system_groups() if g["name"] not in share_access_groups]


@app.route("/api/users")
def api_users():
    samba = smb.list_samba_users()
    samba_set = set(samba.get("usernames", []))
    # The dedicated sync account (see ssh_keys.SYNC_ACCOUNT_USERNAME) is
    # not a person and has no SMB access - showing it in the general
    # Users list would invite exactly the kind of accidental edit/delete
    # that share-access groups are already filtered out to avoid below.
    system_users = [u for u in users.list_system_users() if u["username"] != ssh_keys.SYNC_ACCOUNT_USERNAME]
    for u in system_users:
        u["has_smb"] = u["username"] in samba_set
        u["smb_disabled"] = smb.get_account_flags(u["username"])["disabled"] if u["has_smb"] else False

    return jsonify(
        {
            "users": system_users,
            "groups": _general_groups(),
            "samba": samba,
        }
    )


@app.route("/api/groups")
def api_groups():
    return jsonify({"groups": _general_groups()})


@app.route("/api/groups/create", methods=["POST"])
def api_groups_create():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()

    result = users.create_group(name)
    if not result["success"]:
        oplog.log_event("groups", "create", "failure", params={"name": name})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("groups", "create", "success", params={"name": name})
    return jsonify({"success": True})


@app.route("/api/groups/<name>/delete", methods=["POST"])
def api_groups_delete(name):
    # Same defense-in-depth as the sync account: the Groups tab UI never
    # offers a share's own access group for deletion, but nothing stops
    # a direct API call from naming one - reject that outright rather
    # than pulling access out from under a share.
    share_access_groups = {
        s["access_group"] for s in smb_shares.list_shares().get("shares", []) if s.get("access_group")
    }
    if name in share_access_groups:
        return jsonify({"success": False, "error_code": "groups.is_share_access_group", "error_context": {"group": name}}), 400

    result = users.delete_group(name)
    if not result["success"]:
        oplog.log_event("groups", "delete", "failure", params={"name": name})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    smb_shares.remove_group_from_all_shares(name)
    oplog.log_event("groups", "delete", "success", params={"name": name})
    return jsonify({"success": True})


@app.route("/api/groups/<name>/members", methods=["POST"])
def api_groups_set_members(name):
    # Same guard as delete - this tab must never touch a share's own
    # auto-managed access group, direct API call or not.
    share_access_groups = {
        s["access_group"] for s in smb_shares.list_shares().get("shares", []) if s.get("access_group")
    }
    if name in share_access_groups:
        return jsonify({"success": False, "error_code": "groups.is_share_access_group", "error_context": {"group": name}}), 400

    existing = {g["name"]: g for g in users.list_system_groups()}
    if name not in existing:
        return jsonify({"success": False, "error_code": "users.group_not_found", "error_context": {"group": name}}), 404

    data = request.get_json(force=True, silent=True) or {}
    desired = {u.strip() for u in (data.get("usernames") or []) if u.strip()}
    current = set(existing[name]["members"])

    # add_user_to_group/remove_user_from_group each touch exactly one
    # membership without disturbing the rest of that user's groups (see
    # their docstrings) - applying only the actual diff, rather than
    # e.g. removing everyone and re-adding the desired set, means a
    # failure partway through still leaves every untouched membership
    # exactly as it was.
    for username in sorted(desired - current):
        result = users.add_user_to_group(username, name)
        if not result["success"]:
            oplog.log_event("groups", "update_members", "failure", params={"name": name, "username": username})
            return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    for username in sorted(current - desired):
        result = users.remove_user_from_group(username, name)
        if not result["success"]:
            oplog.log_event("groups", "update_members", "failure", params={"name": name, "username": username})
            return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400

    oplog.log_event(
        "groups", "update_members", "success",
        params={"name": name, "added": sorted(desired - current), "removed": sorted(current - desired)},
    )
    return jsonify({"success": True})


@app.route("/api/users/create", methods=["POST"])
def api_users_create():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    groups = [g.strip() for g in (data.get("groups") or []) if g.strip()]
    if password and DEFAULT_SMB_GROUP not in groups:
        groups.append(DEFAULT_SMB_GROUP)

    user_result = users.create_user(username, groups=groups, shell=None)
    if not user_result["success"]:
        oplog.log_event("users", "create", "failure", params={"username": username})
        return jsonify(
            {"success": False, "step": "user", "error_code": user_result["error_code"], "error_context": user_result["error_context"]}
        ), 400

    if password:
        smb_result = smb.set_password(user_result["username"], password)
        if not smb_result["success"]:
            oplog.log_event("users", "create", "failure", params={"username": username}, message="smb_password_failed")
            return jsonify(
                {
                    "success": False,
                    "step": "smb",
                    "error_code": smb_result["error_code"],
                    "error_context": smb_result["error_context"],
                    "note_code": "users.create_smb_password_failed",
                }
            ), 400

    oplog.log_event("users", "create", "success", params={"username": username})
    return jsonify({"success": True, "user": user_result})


@app.route("/api/users/<username>/update", methods=["POST"])
def api_users_update(username):
    data = request.get_json(force=True, silent=True) or {}
    groups = [g.strip() for g in (data.get("groups") or []) if g.strip()]
    display_name = (data.get("display_name") or "").strip() or None
    password = data.get("password") or ""  # empty = leave SMB password unchanged

    user_result = users.update_user(
        username,
        groups=groups,
        display_name=display_name,
    )
    if not user_result["success"]:
        oplog.log_event("users", "update", "failure", params={"username": username})
        return jsonify(
            {"success": False, "step": "user", "error_code": user_result["error_code"], "error_context": user_result["error_context"]}
        ), 400

    if password:
        smb_result = smb.set_password(username, password)
        if not smb_result["success"]:
            oplog.log_event("users", "update", "failure", params={"username": username}, message="smb_password_failed")
            return jsonify(
                {
                    "success": False,
                    "step": "smb",
                    "error_code": smb_result["error_code"],
                    "error_context": smb_result["error_context"],
                    "note_code": "users.update_smb_password_failed",
                }
            ), 400

    oplog.log_event("users", "update", "success", params={"username": username})
    return jsonify({"success": True, "user": user_result})


@app.route("/api/users/<username>/remove-smb", methods=["POST"])
def api_users_remove_smb(username):
    result = smb.remove_user(username)
    if not result["success"]:
        oplog.log_event("users", "remove_smb", "failure", params={"username": username})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("users", "remove_smb", "success", params={"username": username})
    return jsonify({"success": True})


@app.route("/api/users/<username>/disable", methods=["POST"])
def api_users_disable(username):
    result = smb.disable_account(username)
    if not result["success"]:
        oplog.log_event("users", "disable", "failure", params={"username": username})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("users", "disable", "success", params={"username": username})
    return jsonify({"success": True})


@app.route("/api/users/<username>/enable", methods=["POST"])
def api_users_enable(username):
    result = smb.enable_account(username)
    if not result["success"]:
        oplog.log_event("users", "enable", "failure", params={"username": username})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("users", "enable", "success", params={"username": username})
    return jsonify({"success": True})


@app.route("/api/users/<username>/delete", methods=["POST"])
def api_users_delete(username):
    if username == ssh_keys.SYNC_ACCOUNT_USERNAME:
        return jsonify({"success": False, "error_code": "users.not_found", "error_context": {"username": username}}), 400
    data = request.get_json(force=True, silent=True) or {}
    remove_home = bool(data.get("remove_home", False))

    # Best-effort side effects, before the account itself is gone:
    # - drop SMB access (less destructive than deleting the account)
    # - remove the local SSH keypair, so it can't be left orphaned under
    #   a now-deleted account's UID
    # - drop them from every managed share's individual permissions, so
    #   a share's config never keeps a stale entry pointing at a
    #   username that no longer exists
    # A pre-existing remote deployment is NOT touched here - revoking it
    # needs that remote host's password, which nobody supplied as part
    # of "delete this user". had_deployments is captured before the
    # local key is gone, purely to decide whether to warn about it.
    had_deployments = bool(ssh_keys.get_deployments(username))
    smb.remove_user(username)
    ssh_keys.delete_key(username)
    ssh_keys.forget_user(username)
    smb_shares.remove_user_from_all_shares(username)

    user_result = users.delete_user(username, remove_home=remove_home)
    if not user_result["success"]:
        oplog.log_event("users", "delete", "failure", params={"username": username})
        return jsonify(
            {"success": False, "step": "user", "error_code": user_result["error_code"], "error_context": user_result["error_context"]}
        ), 400

    oplog.log_event("users", "delete", "success", params={"username": username})
    response = {"success": True}
    if had_deployments:
        response["note_code"] = "users.delete_remote_keys_remain"
    return jsonify(response)


@app.route("/api/shares")
def api_shares():
    return jsonify(smb_shares.list_shares())


@app.route("/api/shares/locations")
def api_shares_locations():
    return jsonify({"locations": smb_shares.list_share_locations()})


@app.route("/api/shares/create", methods=["POST"])
def api_shares_create():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip().lower()
    comment = (data.get("comment") or "").strip()
    base_path = (data.get("base_path") or "").strip() or None
    permissions = {
        u.strip(): level
        for u, level in (data.get("permissions") or {}).items()
        if u.strip() and level in ("rw", "ro")
    }
    group_grants = {
        g.strip(): level
        for g, level in (data.get("group_grants") or {}).items()
        if g.strip() and level in ("rw", "ro")
    }

    result = smb_shares.create_share(name, comment=comment, permissions=permissions, group_grants=group_grants, base_path=base_path)
    if not result["success"]:
        oplog.log_event("shares", "create", "failure", params={"name": name})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("shares", "create", "success", params={"name": name, "base_path": base_path or smb_shares.BASE_SHARE_PATH})
    return jsonify({"success": True, "share": result})


@app.route("/api/shares/<name>/update", methods=["POST"])
def api_shares_update(name):
    data = request.get_json(force=True, silent=True) or {}
    comment = data.get("comment")
    raw_permissions = data.get("permissions")
    permissions = None
    if raw_permissions is not None:
        permissions = {
            u.strip(): level for u, level in raw_permissions.items() if u.strip() and level in ("rw", "ro")
        }
    raw_group_grants = data.get("group_grants")
    group_grants = None
    if raw_group_grants is not None:
        group_grants = {
            g.strip(): level for g, level in raw_group_grants.items() if g.strip() and level in ("rw", "ro")
        }

    result = smb_shares.update_share(
        name,
        comment=comment,
        permissions=permissions,
        group_grants=group_grants,
    )
    if not result["success"]:
        oplog.log_event("shares", "update", "failure", params={"name": name})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("shares", "update", "success", params={"name": name})
    return jsonify({"success": True, "share": result})


@app.route("/api/shares/<name>/delete", methods=["POST"])
def api_shares_delete(name):
    data = request.get_json(force=True, silent=True) or {}
    delete_files = bool(data.get("delete_files", False))

    result = smb_shares.delete_share(name, delete_files=delete_files)
    if not result["success"]:
        oplog.log_event("shares", "delete", "failure", params={"name": name})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("shares", "delete", "success", params={"name": name})
    return jsonify({"success": True, "share": result})


@app.route("/api/ssh-keys")
def api_ssh_keys():
    ensure_result = ssh_keys.ensure_sync_account_exists()
    if not ensure_result["success"]:
        return jsonify({"keys": [], "error_code": ensure_result["error_code"], "error_context": ensure_result["error_context"]})
    return jsonify({"keys": [ssh_keys.get_key_status(ssh_keys.SYNC_ACCOUNT_USERNAME)]})


def _reject_non_sync_account(username):
    """Defense in depth: the frontend only ever calls these routes with
    the dedicated sync account, but nothing stops a direct API call from
    naming any other account - reject that outright rather than quietly
    generating/deploying a key for whatever username was passed."""
    if username != ssh_keys.SYNC_ACCOUNT_USERNAME:
        return jsonify({"success": False, "error_code": "ssh_keys.not_sync_account", "error_context": {}}), 400
    return None


@app.route("/api/ssh-keys/<username>/generate", methods=["POST"])
def api_ssh_keys_generate(username):
    rejected = _reject_non_sync_account(username)
    if rejected:
        return rejected
    result = ssh_keys.generate_key(username)
    if not result["success"]:
        oplog.log_event("certs", "generate", "failure", params={"username": username})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("certs", "generate", "success", params={"username": username})
    return jsonify({"success": True, "public_key": result["public_key"]})


@app.route("/api/ssh-keys/<username>/deploy", methods=["POST"])
def api_ssh_keys_deploy(username):
    rejected = _reject_non_sync_account(username)
    if rejected:
        return rejected
    data = request.get_json(force=True, silent=True) or {}
    remote_host = (data.get("remote_host") or "").strip()
    remote_user = (data.get("remote_user") or "").strip()
    remote_password = data.get("remote_password") or ""
    display_name = (data.get("display_name") or "").strip() or None

    result = ssh_keys.deploy_key_to_remote(username, remote_host, remote_user, remote_password, display_name)
    target = display_name or remote_host
    if not result["success"]:
        oplog.log_event("certs", "deploy", "failure", params={"username": username, "target": target})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("certs", "deploy", "success", params={"username": username, "target": target})
    return jsonify({"success": True})


@app.route("/api/ssh-keys/<username>/delete", methods=["POST"])
def api_ssh_keys_delete(username):
    rejected = _reject_non_sync_account(username)
    if rejected:
        return rejected
    result = ssh_keys.delete_key(username)
    if not result["success"]:
        oplog.log_event("certs", "delete", "failure", params={"username": username})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("certs", "delete", "success", params={"username": username})
    return jsonify({"success": True})


@app.route("/api/ssh-keys/<username>/deployments/remove", methods=["POST"])
def api_ssh_keys_remove_deployment(username):
    rejected = _reject_non_sync_account(username)
    if rejected:
        return rejected
    data = request.get_json(force=True, silent=True) or {}
    remote_host = (data.get("remote_host") or "").strip()
    remote_user = (data.get("remote_user") or "").strip()
    remote_password = data.get("remote_password") or ""

    result = ssh_keys.remove_deployment(username, remote_host, remote_user, remote_password)
    if not result["success"]:
        oplog.log_event("certs", "remove_deployment", "failure", params={"username": username, "target": remote_host})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("certs", "remove_deployment", "success", params={"username": username, "target": remote_host})
    return jsonify({"success": True})


@app.route("/api/network")
def api_network():
    status = network.get_status()
    pending = network_mutate.get_pending_change()
    if pending:
        status["pending_change"] = {
            "token": pending["token"],
            "interface": pending["interface"],
            "created_at": pending["created_at"],
        }
    return jsonify(status)


@app.route("/api/network/<iface>/apply", methods=["POST"])
def api_network_apply(iface):
    data = request.get_json(force=True, silent=True) or {}
    ip = (data.get("ip") or "").strip()
    prefixlen = data.get("prefixlen")
    gateway = (data.get("gateway") or "").strip()
    dns = data.get("dns") or []

    result = network_mutate.request_ip_change(iface, ip, prefixlen, gateway, dns)
    if not result["success"]:
        oplog.log_event("network", "apply", "failure", params={"interface": iface})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400

    oplog.log_event("network", "apply", "success", params={"interface": iface, "ip": ip})
    return jsonify({"success": True, "token": result["token"], "expires_in": result["expires_in"], "new_host": result["new_host"]})


@app.route("/api/network/confirm", methods=["POST"])
def api_network_confirm():
    data = request.get_json(force=True, silent=True) or {}
    token = (data.get("token") or "").strip()

    result = network_mutate.confirm_change(token)
    if not result["success"]:
        oplog.log_event("network", "confirm", "failure", params={})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400

    oplog.log_event("network", "confirm", "success", params={"interface": result.get("interface", "")})
    return jsonify({"success": True})


@app.route("/api/log")
def api_log():
    since = request.args.get("since") or None
    until = request.args.get("until") or None
    limit_raw = request.args.get("limit")
    limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else None
    return jsonify(
        {
            "events": oplog.list_events(since=since, until=until, limit=limit),
            "max_entries": oplog.get_max_entries(),
        }
    )


@app.route("/api/log/clear", methods=["POST"])
def api_log_clear():
    result = oplog.clear_events()
    if not result["success"]:
        return jsonify({"success": False, "error_code": "system.io_failed", "error_context": {"detail": result.get("error", "")}}), 400
    return jsonify({"success": True})


@app.route("/api/log/settings", methods=["POST"])
def api_log_settings():
    data = request.get_json(force=True, silent=True) or {}
    max_entries = data.get("max_entries")
    try:
        max_entries = int(max_entries)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error_code": "log.invalid_max_entries", "error_context": {}}), 400

    result = oplog.set_max_entries(max_entries)
    if not result["success"]:
        return jsonify({"success": False, "error_code": "system.io_failed", "error_context": {"detail": result.get("error", "")}}), 400
    return jsonify({"success": True, "max_entries": result["max_entries"]})


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    """Last-resort safety net. Without this, any bug that raises before a
    route's own error handling kicks in - or a routing-level mistake like
    a URL variable name that doesn't match the view function's parameter -
    falls through to Werkzeug's default HTML error page. The frontend
    then fails trying to parse that HTML as JSON ("Unexpected token '<'"),
    and - just as importantly - the failure never reaches oplog, since
    the crash happens before the route's own log_event() call is ever
    reached. This handler guarantees every /api/ failure is both valid
    JSON and visible in the operations log, regardless of where in the
    stack it went wrong."""
    if isinstance(exc, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error_code": "system.http_error", "error_context": {"status": exc.code}}), exc.code
        return exc

    app.logger.exception("Unhandled error in %s %s", request.method, request.path)
    if request.path.startswith("/api/"):
        oplog.log_event("system", "error", "failure", params={"path": request.path}, message=str(exc))
        return jsonify({"success": False, "error_code": "system.unexpected_error", "error_context": {"detail": str(exc)}}), 500
    raise exc


def main():
    # Binds to all interfaces so it's reachable on the LAN - this is a
    # read-only dashboard with no authentication yet, so only run it on
    # a trusted network. See README.md before exposing it more widely.
    app.run(host="0.0.0.0", port=8420)


if __name__ == "__main__":
    main()
