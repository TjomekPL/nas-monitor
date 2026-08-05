#!/usr/bin/env python3
"""
Sets the initial nas-monitor admin credentials. Called from install.sh
after it collects and validates the username/password interactively.

Reads username on the first line of stdin and password on the second -
deliberately never as command-line arguments, which would be visible to
any local user via `ps` for as long as the process runs.

Usage: printf '%s\\n%s\\n' "$USERNAME" "$PASSWORD" | python3 -m nas_monitor.setup_admin
"""

from __future__ import annotations

import sys

from nas_monitor import auth


def main() -> int:
    lines = sys.stdin.readlines()
    if len(lines) < 2:
        print("Error: expected username and password on stdin (two lines)", file=sys.stderr)
        return 1

    username = lines[0].rstrip("\n")
    password = lines[1].rstrip("\n")

    result = auth.set_credentials(username, password)
    if not result["success"]:
        print(f"Error: {result['error_code']}", file=sys.stderr)
        return 1

    print(f"OK:{result['username']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
