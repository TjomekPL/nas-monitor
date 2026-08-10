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
import logging
import logging.handlers
import os
import secrets
import time
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from nas_monitor import state_store, errors

CREDENTIALS_FILE = "auth-credentials.json"
SECRET_KEY_FILE = "auth-secret-key.json"
LOGIN_ATTEMPTS_FILE = "auth-login-attempts.json"

MIN_PASSWORD_LENGTH = 10

# Failed-login file log, purely for fail2ban (see fail2ban/ in the repo
# root) - a completely separate concern from the JSON-backed lockout
# counter below (is_locked_out/record_failed_login), which is what
# actually enforces anything. Lazily set up on first use (not at import
# time) and best-effort throughout: on an install where
# /var/log/nas-monitor doesn't exist yet (nginx+fail2ban setup hasn't
# been run, or install.sh is an older version, or - in tests - there's
# no permission to create it at all), logging is silently skipped
# rather than ever raising and breaking a login attempt over it.
AUTH_LOG_DIR = "/var/log/nas-monitor"
AUTH_LOG_FILE = os.path.join(AUTH_LOG_DIR, "auth.log")

_auth_file_logger: logging.Logger | None = None


def _get_auth_file_logger() -> logging.Logger | None:
    global _auth_file_logger
    if _auth_file_logger is not None:
        return _auth_file_logger
    logger = logging.getLogger("nas_monitor.auth_file_log")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Deliberately not gated on `if not logger.handlers` - this named
    # logger is only ever touched from here, so the module-level
    # _auth_file_logger cache above is already the single source of
    # truth for "have we set this up yet". Checking logger.handlers
    # too seems like reasonable extra caution, but it isn't: anything
    # else that happens to inspect/instrument this same logger (test
    # frameworks' log capture is a real example - pytest attaches its
    # own handler to whatever logger a test touches) would make this
    # believe setup already happened and skip adding the real handler
    # entirely, silently dropping every future log line.
    try:
        os.makedirs(AUTH_LOG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(AUTH_LOG_FILE, maxBytes=1_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
    except OSError:
        return None
    _auth_file_logger = logger
    return logger


def log_failed_login_attempt(username: str, remote_addr: str | None) -> None:
    """Writes one line to AUTH_LOG_FILE for fail2ban's nas-monitor jail
    to watch - see fail2ban/nas-monitor.filter.conf for the exact
    format this must match. Never raises: a login attempt must never
    fail (or succeed) differently depending on whether this log write
    works."""
    logger = _get_auth_file_logger()
    if logger is None:
        return
    try:
        logger.info("Failed login attempt for user '%s' from %s", username, remote_addr or "unknown")
    except OSError:
        pass

# Login brute-force guard. Keyed by username, not source IP - this tool
# has exactly one admin account, so limiting guesses against that
# username is what actually matters (an IP-based limit would be easy to
# sidestep on a LAN behind NAT, and adds nothing here). Persisted via
# state_store like everything else that needs to survive across
# gunicorn's worker processes (see get_or_create_secret_key's docstring
# for the same multi-worker concern) - an in-memory counter would reset
# per-worker and let an attacker get MAX_ATTEMPTS tries per worker
# instead of in total.
MAX_LOGIN_ATTEMPTS = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 300
LOGIN_LOCKOUT_SECONDS = 300


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


def is_locked_out(username: str) -> tuple[bool, int]:
    """(locked, seconds_remaining). Called before verify_credentials so a
    lockout skips the password check entirely - not that checking it
    would be expensive, but there's no reason to do it at all once a
    username is locked."""
    attempts = state_store.load(LOGIN_ATTEMPTS_FILE, default={})
    entry = attempts.get(username)
    if not entry:
        return False, 0
    remaining = entry.get("locked_until", 0) - time.time()
    return (remaining > 0, int(remaining) + 1 if remaining > 0 else 0)


def record_failed_login(username: str) -> None:
    """Bump the failure count for username, resetting it first if the
    last failure was outside the rolling window (so a handful of typos
    weeks apart never accumulates into a lockout). Hitting the threshold
    sets locked_until and starts the count fresh for whenever the
    lockout expires."""
    attempts = state_store.load(LOGIN_ATTEMPTS_FILE, default={})
    now = time.time()
    entry = attempts.get(username) or {"count": 0, "window_start": now}
    if now - entry.get("window_start", now) > LOGIN_ATTEMPT_WINDOW_SECONDS:
        entry = {"count": 0, "window_start": now}
    entry["count"] += 1
    if entry["count"] >= MAX_LOGIN_ATTEMPTS:
        entry["locked_until"] = now + LOGIN_LOCKOUT_SECONDS
        entry["count"] = 0
        entry["window_start"] = now
    attempts[username] = entry
    state_store.save(LOGIN_ATTEMPTS_FILE, attempts)


def clear_login_attempts(username: str) -> None:
    """Called on a successful login - a legitimate login is the clearest
    possible signal that whatever failures preceded it weren't an
    attack in progress."""
    attempts = state_store.load(LOGIN_ATTEMPTS_FILE, default={})
    if username in attempts:
        del attempts[username]
        state_store.save(LOGIN_ATTEMPTS_FILE, attempts)


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


# Round, intuitive numbers (his correction after the first pass used
# 90/110/130 purely as an arbitrary reference point) - 80/100/120, with
# 100 meaning the browser's own true, unscaled default (no CSS zoom
# applied at all), not an app-side boost on top of it.
VALID_UI_SCALES = (80, 100, 120)


def get_ui_scale() -> int:
    data = state_store.load(CREDENTIALS_FILE, default=None)
    scale = data.get("ui_scale") if data else None
    return scale if scale in VALID_UI_SCALES else 100


def set_ui_scale(scale: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"success": False}
    data = state_store.load(CREDENTIALS_FILE, default=None)
    if not data:
        return errors.fail(result, "auth.not_configured")
    try:
        scale = int(scale)
    except (TypeError, ValueError):
        return errors.fail(result, "auth.invalid_ui_scale")
    if scale not in VALID_UI_SCALES:
        return errors.fail(result, "auth.invalid_ui_scale")
    data["ui_scale"] = scale
    save_result = state_store.save(CREDENTIALS_FILE, data)
    if not save_result["success"]:
        return errors.fail(result, "system.io_failed", path=CREDENTIALS_FILE, detail=save_result.get("error", ""))
    result["success"] = True
    result["ui_scale"] = scale
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
    os.chmod(tmp_path, 0o600)
    try:
        os.link(tmp_path, final_path)
    except FileExistsError:
        pass  # another worker won the race - its key is the one that counts
    finally:
        os.remove(tmp_path)

    data = state_store.load(SECRET_KEY_FILE, default=None)
    return data["key"]
