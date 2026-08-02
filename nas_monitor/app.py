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

from flask import Flask, jsonify, render_template, request

from nas_monitor import monitor, users, smb, smb_shares, ssh_keys, network, oplog

app = Flask(__name__)


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/status")
def api_status():
    return jsonify(monitor.get_full_status())


@app.route("/api/users")
def api_users():
    samba = smb.list_samba_users()
    samba_set = set(samba.get("usernames", []))
    system_users = users.list_system_users()
    for u in system_users:
        u["has_smb"] = u["username"] in samba_set

    # Share-access groups (<share>_access) are auto-managed exclusively
    # from the Udziały section - showing them in the general "edit user"
    # checklist invites exactly the kind of accidental un-sharing that
    # happened before this filter existed: editing something else about
    # a user, not realizing one of the checked boxes IS their access to
    # a share, and losing it on save.
    share_access_groups = {
        s["access_group"] for s in smb_shares.list_shares().get("shares", []) if s.get("access_group")
    }
    general_groups = [g for g in users.list_system_groups() if g["name"] not in share_access_groups]

    return jsonify(
        {
            "users": system_users,
            "groups": general_groups,
            "samba": samba,
        }
    )


@app.route("/api/users/create", methods=["POST"])
def api_users_create():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    groups = [g.strip() for g in (data.get("groups") or []) if g.strip()]
    allow_login = bool(data.get("allow_login", False))

    user_result = users.create_user(
        username,
        groups=groups,
        shell="/bin/bash" if allow_login else None,
    )
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
    allow_login = bool(data.get("allow_login", False))
    display_name = (data.get("display_name") or "").strip() or None
    password = data.get("password") or ""  # empty = leave SMB password unchanged

    user_result = users.update_user(
        username,
        groups=groups,
        shell="/bin/bash" if allow_login else users.default_nologin_shell(),
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


@app.route("/api/users/<username>/delete", methods=["POST"])
def api_users_delete(username):
    data = request.get_json(force=True, silent=True) or {}
    remove_home = bool(data.get("remove_home", False))

    # Best-effort: remove any SMB access first (less destructive than
    # deleting the account). A missing/already-gone SMB entry isn't a
    # failure - the point is the account being gone, not this side effect.
    smb.remove_user(username)

    user_result = users.delete_user(username, remove_home=remove_home)
    if not user_result["success"]:
        oplog.log_event("users", "delete", "failure", params={"username": username})
        return jsonify(
            {"success": False, "step": "user", "error_code": user_result["error_code"], "error_context": user_result["error_context"]}
        ), 400

    oplog.log_event("users", "delete", "success", params={"username": username})
    return jsonify({"success": True})


@app.route("/api/shares")
def api_shares():
    return jsonify(smb_shares.list_shares())


@app.route("/api/shares/create", methods=["POST"])
def api_shares_create():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip().lower()
    comment = (data.get("comment") or "").strip()
    permissions = {
        u.strip(): level
        for u, level in (data.get("permissions") or {}).items()
        if u.strip() and level in ("rw", "ro")
    }

    result = smb_shares.create_share(name, comment=comment, permissions=permissions)
    if not result["success"]:
        oplog.log_event("shares", "create", "failure", params={"name": name})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("shares", "create", "success", params={"name": name})
    return jsonify({"success": True, "share": result})


@app.route("/api/shares/<n>/update", methods=["POST"])
def api_shares_update(name):
    data = request.get_json(force=True, silent=True) or {}
    comment = data.get("comment")
    raw_permissions = data.get("permissions")
    permissions = None
    if raw_permissions is not None:
        permissions = {
            u.strip(): level for u, level in raw_permissions.items() if u.strip() and level in ("rw", "ro")
        }

    result = smb_shares.update_share(
        name,
        comment=comment,
        permissions=permissions,
    )
    if not result["success"]:
        oplog.log_event("shares", "update", "failure", params={"name": name})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("shares", "update", "success", params={"name": name})
    return jsonify({"success": True, "share": result})


@app.route("/api/shares/<n>/delete", methods=["POST"])
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
    system_users = users.list_system_users()
    statuses = [ssh_keys.get_key_status(u["username"]) for u in system_users]
    return jsonify({"keys": statuses})


@app.route("/api/ssh-keys/<username>/generate", methods=["POST"])
def api_ssh_keys_generate(username):
    result = ssh_keys.generate_key(username)
    if not result["success"]:
        oplog.log_event("certs", "generate", "failure", params={"username": username})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("certs", "generate", "success", params={"username": username})
    return jsonify({"success": True, "public_key": result["public_key"]})


@app.route("/api/ssh-keys/<username>/deploy", methods=["POST"])
def api_ssh_keys_deploy(username):
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
    result = ssh_keys.delete_key(username)
    if not result["success"]:
        oplog.log_event("certs", "delete", "failure", params={"username": username})
        return jsonify({"success": False, "error_code": result["error_code"], "error_context": result["error_context"]}), 400
    oplog.log_event("certs", "delete", "success", params={"username": username})
    return jsonify({"success": True})


@app.route("/api/ssh-keys/<username>/deployments/remove", methods=["POST"])
def api_ssh_keys_remove_deployment(username):
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
    return jsonify(network.get_status())


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


def main():
    # Binds to all interfaces so it's reachable on the LAN - this is a
    # read-only dashboard with no authentication yet, so only run it on
    # a trusted network. See README.md before exposing it more widely.
    app.run(host="0.0.0.0", port=8420)


if __name__ == "__main__":
    main()
