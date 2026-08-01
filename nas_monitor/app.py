"""
nas_monitor.app
----------------
Read-only web dashboard for disk S.M.A.R.T. health and mdadm/RAID status.
No write operations, no mounting, no array creation - this is phase 1
(monitoring only) of a larger tool. See README.md.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template

from nas_monitor import monitor

app = Flask(__name__)


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/status")
def api_status():
    return jsonify(monitor.get_full_status())


def main():
    # Binds to all interfaces so it's reachable on the LAN - this is a
    # read-only dashboard with no authentication yet, so only run it on
    # a trusted network. See README.md before exposing it more widely.
    app.run(host="0.0.0.0", port=8420)


if __name__ == "__main__":
    main()
