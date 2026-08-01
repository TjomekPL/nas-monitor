"""
nas_monitor.app
----------------
Read-only web dashboard for disk S.M.A.R.T. health and mdadm/RAID status.
No write operations, no mounting, no array creation - this is phase 1
(monitoring only) of a larger tool. See README.md.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from nas_monitor import monitor, users, smb, smb_shares, ssh_keys

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
        return jsonify({"success": False, "step": "user", "error": user_result["error"]}), 400

    if password:
        smb_result = smb.set_password(user_result["username"], password)
        if not smb_result["success"]:
            return jsonify(
                {
                    "success": False,
                    "step": "smb",
                    "error": smb_result["error"],
                    "note": "Konto systemowe zostało utworzone, ale nie udało się ustawić hasła SMB.",
                }
            ), 400

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
        return jsonify({"success": False, "step": "user", "error": user_result["error"]}), 400

    if password:
        smb_result = smb.set_password(username, password)
        if not smb_result["success"]:
            return jsonify(
                {
                    "success": False,
                    "step": "smb",
                    "error": smb_result["error"],
                    "note": "Dane konta zostały zaktualizowane, ale nie udało się zmienić hasła SMB.",
                }
            ), 400

    return jsonify({"success": True, "user": user_result})


@app.route("/api/users/<username>/remove-smb", methods=["POST"])
def api_users_remove_smb(username):
    result = smb.remove_user(username)
    if not result["success"]:
        return jsonify({"success": False, "error": result["error"]}), 400
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
        return jsonify({"success": False, "step": "user", "error": user_result["error"]}), 400

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
        return jsonify({"success": False, "error": result["error"]}), 400
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

    result = smb_shares.update_share(
        name,
        comment=comment,
        permissions=permissions,
    )
    if not result["success"]:
        return jsonify({"success": False, "error": result["error"]}), 400
    return jsonify({"success": True, "share": result})


@app.route("/api/shares/<name>/delete", methods=["POST"])
def api_shares_delete(name):
    data = request.get_json(force=True, silent=True) or {}
    delete_files = bool(data.get("delete_files", False))

    result = smb_shares.delete_share(name, delete_files=delete_files)
    if not result["success"]:
        return jsonify({"success": False, "error": result["error"]}), 400
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
        return jsonify({"success": False, "error": result["error"]}), 400
    return jsonify({"success": True, "public_key": result["public_key"]})


@app.route("/api/ssh-keys/<username>/deploy", methods=["POST"])
def api_ssh_keys_deploy(username):
    data = request.get_json(force=True, silent=True) or {}
    remote_host = (data.get("remote_host") or "").strip()
    remote_user = (data.get("remote_user") or "").strip()
    remote_password = data.get("remote_password") or ""

    result = ssh_keys.deploy_key_to_remote(username, remote_host, remote_user, remote_password)
    if not result["success"]:
        return jsonify({"success": False, "error": result["error"]}), 400
    return jsonify({"success": True})


@app.route("/api/ssh-keys/<username>/delete", methods=["POST"])
def api_ssh_keys_delete(username):
    result = ssh_keys.delete_key(username)
    if not result["success"]:
        return jsonify({"success": False, "error": result["error"]}), 400
    return jsonify({"success": True})


@app.route("/api/ssh-keys/<username>/deployments/remove", methods=["POST"])
def api_ssh_keys_remove_deployment(username):
    data = request.get_json(force=True, silent=True) or {}
    remote_host = (data.get("remote_host") or "").strip()
    remote_user = (data.get("remote_user") or "").strip()
    remote_password = data.get("remote_password") or ""

    result = ssh_keys.remove_deployment(username, remote_host, remote_user, remote_password)
    if not result["success"]:
        return jsonify({"success": False, "error": result["error"]}), 400
    return jsonify({"success": True})


def main():
    # Binds to all interfaces so it's reachable on the LAN - this is a
    # read-only dashboard with no authentication yet, so only run it on
    # a trusted network. See README.md before exposing it more widely.
    app.run(host="0.0.0.0", port=8420)


if __name__ == "__main__":
    main()
