"""
nas_monitor.auth
------------------
Login for the dashboard itself - deliberately NOT a Linux/PAM system
account. This is a single app-level credential (username + hashed
password), stored in its own state file, with no relationship to any
system account this tool manages elsewhere. That's a deliberate choice,
not a shortcut: tying "who can open the dashboard" to "who has a system
account that could SSH in" would be a much bigger, riskier surface than
this tool needs, and it's how every comparable self-hosted admin panel
(OMV, Portainer, Proxmox) does it too.

Password hashing uses werkzeug.security (PBKDF2) - already a dependency
of Flask itself, so this adds no new library to the project.

Sessions are Flask's built-in signed-cookie sessions, which need a
consistent secret key across every process reading them. nas-monitor
runs under gunicorn with multiple worker PROCESSES (see
nas-monitor.service and network_mutate.py's docstring for the same
concern elsewhere in this project) - a secret key generated fresh in
each worker's memory would make a session cookie signed by worker A
fail to validate in worker B. The key is generated once and persisted
via state_store so every worker loads the same one at startup.

The AUTH_ENABLED environment variable (set in nas-monitor.service) is
the kill switch: set it to "0" and restart the service to bypass login
entirely. That's an intentional SSH/root-only escape hatch - if you
already have root on the box, you shouldn't need the web login to get
around the web login.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from nas_monitor import state_store, errors

CREDENTIALS_FILE = "auth-credentials.json"
SECRET_KEY_FILE = "auth-secret-key.json"

MIN_PASSWORD_LENGTH = 10
DEFAULT_USERNAME = "admin"


def auth_enabled() -> bool:
    """The kill switch. Anything other than the literal string "0" (or
    "false", case-insensitive) counts as enabled - the default, with no
    env var set at all, is enabled."""
    value = os.environ.get("AUTH_ENABLED", "1").strip().lower()
    return value not in ("0", "false")


def is_configured() -> bool:
    return state_store.load(CREDENTIALS_FILE, default=None) is not None


def get_username() -> str | None:
    data = state_store.load(CREDENTIALS_FILE, default=None)
    return data.get("username") if data else None


def validate_password(password: str) -> dict[str, Any]:
    """Rules: at least 10 characters, at least one letter AND one digit.
    Uppercase and special characters are allowed but never required -
    exactly the middle ground between "too weak" and "so strict people
    write it on a sticky note"."""
    result: dict[str, Any] = {"success": False}
    if len(password) < MIN_PASSWORD_LENGTH:
        return errors.fail(result, "auth.password_too_short", min_length=MIN_PASSWORD_LENGTH)
    if not any(c.isalpha() for c in password):
        return errors.fail(result, "auth.password_needs_letter")
    if not any(c.isdigit() for c in password):
        return errors.fail(result, "auth.password_needs_digit")
    result["success"] = True
    return result


def set_credentials(username: str, password: str) -> dict[str, Any]:
    """Create or completely replace the admin credentials - used by the
    install-time setup script and nowhere else (changing the password
    later goes through change_password(), which requires the current
    one)."""
    result: dict[str, Any] = {"success": False}
    username = username.strip()
    if not username:
        return errors.fail(result, "auth.invalid_username")

    validation = validate_password(password)
    if not validation["success"]:
        return errors.propagate(result, validation)

    existing = state_store.load(CREDENTIALS_FILE, default={}) or {}
    data = {
        "username": username,
        "password_hash": generate_password_hash(password),
        # None = "until the browser closes" (a non-permanent session
        # cookie) - the default; a number is hours.
        "session_duration_minutes": existing.get("session_duration_minutes"),
    }
    save_result = state_store.save(CREDENTIALS_FILE, data)
    if not save_result["success"]:
        return errors.fail(result, "system.io_failed", path=CREDENTIALS_FILE, detail=save_result.get("error", ""))

    result["success"] = True
    result["username"] = username
    return result


def verify_credentials(username: str, password: str) -> bool:
    data = state_store.load(CREDENTIALS_FILE, default=None)
    if not data:
        return False
    if username != data.get("username"):
        return False
    return check_password_hash(data.get("password_hash", ""), password)


def change_password(current_password: str, new_password: str) -> dict[str, Any]:
    result: dict[str, Any] = {"success": False}
    data = state_store.load(CREDENTIALS_FILE, default=None)
    if not data:
        return errors.fail(result, "auth.not_configured")

    if not check_password_hash(data.get("password_hash", ""), current_password):
        return errors.fail(result, "auth.wrong_current_password")

    validation = validate_password(new_password)
    if not validation["success"]:
        return errors.propagate(result, validation)

    data["password_hash"] = generate_password_hash(new_password)
    save_result = state_store.save(CREDENTIALS_FILE, data)
    if not save_result["success"]:
        return errors.fail(result, "system.io_failed", path=CREDENTIALS_FILE, detail=save_result.get("error", ""))

    result["success"] = True
    return result


def get_session_duration_minutes() -> int | None:
    """None means "until the browser closes" - a non-permanent session
    cookie with no explicit expiry, rather than a fixed duration."""
    data = state_store.load(CREDENTIALS_FILE, default=None)
    return data.get("session_duration_minutes") if data else None


def set_session_duration_minutes(minutes: int | None) -> dict[str, Any]:
    result: dict[str, Any] = {"success": False}
    data = state_store.load(CREDENTIALS_FILE, default=None)
    if not data:
        return errors.fail(result, "auth.not_configured")
    if minutes is not None:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            return errors.fail(result, "auth.invalid_session_duration")
        if minutes < 5 or minutes > 30 * 24 * 60:
            return errors.fail(result, "auth.invalid_session_duration")
    data["session_duration_minutes"] = minutes
    save_result = state_store.save(CREDENTIALS_FILE, data)
    if not save_result["success"]:
        return errors.fail(result, "system.io_failed", path=CREDENTIALS_FILE, detail=save_result.get("error", ""))
    result["success"] = True
    result["session_duration_minutes"] = minutes
    return result


def get_or_create_secret_key() -> str:
    """The Flask session-signing key - generated once, persisted, and
    reused by every gunicorn worker process (see module docstring).

    Race-safe on purpose: gunicorn runs multiple worker PROCESSES, and
    on a genuinely fresh install (no key file yet) they can start
    within milliseconds of each other. A naive "check if it exists,
    else write" has a real window where two workers both see "doesn't
    exist yet" and each generate and save their OWN key - whichever
    write lands last silently wins on disk, while the other worker
    keeps running with the key it already loaded into memory. A
    session cookie signed by one worker then fails verification on the
    other, which looks exactly like "logs in, works for a moment, then
    gets bounced back to the login page" depending on which worker
    happens to handle the next request. Confirmed against a real report.

    os.link() creates the file atomically at the OS level and raises
    FileExistsError if the target is already there - so at most one
    worker's key ever actually gets written; every other worker (or a
    retry after losing the race) just reads whatever won."""
    data = state_store.load(SECRET_KEY_FILE, default=None)
    if data and data.get("key"):
        return data["key"]

    os.makedirs(state_store.STATE_DIR, exist_ok=True)
    final_path = os.path.join(state_store.STATE_DIR, SECRET_KEY_FILE)
    tmp_path = f"{final_path}.{os.getpid()}.tmp"

    with open(tmp_path, "w") as f:
        json.dump({"key": secrets.token_hex(32)}, f)
    try:
        os.link(tmp_path, final_path)
    except FileExistsError:
        pass  # another worker won the race - its key is the one that counts
    finally:
        os.remove(tmp_path)

    data = state_store.load(SECRET_KEY_FILE, default=None)
    return data["key"]
