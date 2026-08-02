"""
nas_monitor.oplog
-------------------
Operations log: a record of what nas-monitor has *done* (user/share/key/
account mutations), not what it has merely displayed. Deliberately NOT a
raw console-style log - the UI shows a short human header per entry
("Utworzono użytkownika wieslaw", success/failure), and the full raw
detail is available on demand (expand + copy) rather than always visible,
since a wall of terminal-style text is exactly what discourages people
from ever looking at a log.

Persisted via state_store (like ssh_keys deployment state) so history
survives a service restart. Two things are stored under one file to
avoid juggling two read/modify/write cycles for related data:
  - "max_entries": the configured retention cap (default 50)
  - "events": the entries themselves, newest last on disk (oldest
    dropped first once max_entries is exceeded)
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

from nas_monitor import state_store

STATE_FILE = "oplog.json"
DEFAULT_MAX_ENTRIES = 50
MIN_MAX_ENTRIES = 10
MAX_MAX_ENTRIES = 1000

_id_counter = itertools.count(1)


def _load() -> dict[str, Any]:
    data = state_store.load(STATE_FILE, default=None)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("max_entries", DEFAULT_MAX_ENTRIES)
    data.setdefault("events", [])
    return data


def _next_id(events: list[dict[str, Any]]) -> int:
    return (max((e.get("id", 0) for e in events), default=0)) + 1


def log_event(category: str, action: str, status: str, summary: str, message: str = "") -> dict[str, Any]:
    """Record one operation. status is "success" or "failure".
    summary is the short header line shown collapsed in the UI; message
    is the full detail (error text, command output, ...) shown only when
    the entry is expanded. Never raises - a logging failure must not
    break the operation it's trying to record."""
    data = _load()
    entry = {
        "id": _next_id(data["events"]),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "category": category,
        "action": action,
        "status": "success" if status == "success" else "failure",
        "summary": summary,
        "message": message or "",
    }
    data["events"].append(entry)
    max_entries = data.get("max_entries", DEFAULT_MAX_ENTRIES)
    if len(data["events"]) > max_entries:
        data["events"] = data["events"][-max_entries:]
    state_store.save(STATE_FILE, data)
    return entry


def list_events(since: str | None = None, until: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Newest first. since/until are ISO8601 timestamps (inclusive) used
    for the time-range search; a malformed value is ignored rather than
    raising, so a bad query param just returns the unfiltered list."""
    data = _load()
    events = list(reversed(data["events"]))

    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            events = [e for e in events if datetime.fromisoformat(e["timestamp"]) >= since_dt]
        except ValueError:
            pass
    if until:
        try:
            until_dt = datetime.fromisoformat(until)
            events = [e for e in events if datetime.fromisoformat(e["timestamp"]) <= until_dt]
        except ValueError:
            pass
    if limit:
        events = events[:limit]
    return events


def clear_events() -> dict[str, Any]:
    data = _load()
    data["events"] = []
    return state_store.save(STATE_FILE, data)


def get_max_entries() -> int:
    return _load().get("max_entries", DEFAULT_MAX_ENTRIES)


def set_max_entries(value: int) -> dict[str, Any]:
    value = max(MIN_MAX_ENTRIES, min(MAX_MAX_ENTRIES, int(value)))
    data = _load()
    data["max_entries"] = value
    # Trim immediately so a lowered cap takes effect right away, not just
    # on the next logged event.
    if len(data["events"]) > value:
        data["events"] = data["events"][-value:]
    result = state_store.save(STATE_FILE, data)
    result["max_entries"] = value
    return result
