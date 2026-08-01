"""
nas_monitor.app
----------------
Read-only web dashboard for disk S.M.A.R.T. health and mdadm/RAID status.
No write operations, no mounting, no array creation - this is phase 1
(monitoring only) of a larger tool. See README.md.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from nas_monitor import monitor, users, smb

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
    return jsonify(
        {
            "users": system_users,
            "groups": users.list_system_groups(),
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
        smb_result = smb.set_password(username, password)
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


def main():
    # Binds to all interfaces so it's reachable on the LAN - this is a
    # read-only dashboard with no authentication yet, so only run it on
    # a trusted network. See README.md before exposing it more widely.
    app.run(host="0.0.0.0", port=8420)


if __name__ == "__main__":
    main()
