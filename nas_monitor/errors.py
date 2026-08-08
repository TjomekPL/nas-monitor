"""
nas_monitor.errors
--------------------
Shared helpers for building machine-readable failure results.

Every mutating function in this project returns a dict with "success":
bool and, on failure, "error_code" (a short, stable, NEVER-translated
identifier like "users.already_exists") plus "error_context" (values to
interpolate into the localized message, e.g. {"username": "wieslaw"}).
Nothing in Python ever produces user-facing prose - the actual text for
each code lives in nas_monitor/static/i18n/{pl,en}.js, so behaviour is
identical no matter which language the dashboard is showing, and adding
a new language never touches this file.

Non-fatal "soft" outcomes (the operation succeeded but there's something
worth telling the admin - e.g. "share created, but nobody has an SMB
password yet") use the same code+context shape under "warning_code" /
"warning_context" instead of "error_code"/"error_context".
"""

from __future__ import annotations

from typing import Any


def fail(result: dict[str, Any], code: str, **context: Any) -> dict[str, Any]:
    """Mark a result dict as failed with a given error code. Mutates and
    returns the same dict, so call sites can `return fail(result, ...)`."""
    result["success"] = False
    result["error_code"] = code
    result["error_context"] = context
    result.pop("error", None)
    return result


def warn(result: dict[str, Any], code: str, **context: Any) -> dict[str, Any]:
    """Attach a non-fatal warning code+context to an otherwise-successful
    result. Appends to a "warnings" list (a result can accumulate more
    than one - e.g. a share create that also has a missing-SMB-password
    note). Does not touch "success"."""
    result.setdefault("warnings", []).append({"code": code, "context": context})
    result.pop("warning", None)
    return result


def tool_missing(result: dict[str, Any], tool: str) -> dict[str, Any]:
    """A required system binary isn't installed."""
    return fail(result, "system.tool_missing", tool=tool)


def command_failed(result: dict[str, Any], err: str, out: str = "", code: int = 1, tool: str = "") -> dict[str, Any]:
    """A subprocess exited non-zero. detail is intentionally left as raw,
    untranslated tool output (stderr/stdout, or a generic exit-code
    fallback) - actual command output is not prose to translate, and
    showing the real message is more useful than hiding it behind a
    generic phrase."""
    detail = (err or "").strip() or (out or "").strip() or (f"{tool} exited {code}" if tool else f"exit code {code}")
    return fail(result, "system.command_failed", detail=detail)


def io_failed(result: dict[str, Any], exc: Exception, path: str = "") -> dict[str, Any]:
    """A filesystem operation (open/write/chmod/chown/mkdir/rmtree) raised
    OSError. Same "raw detail, not prose" reasoning as command_failed."""
    return fail(result, "system.io_failed", path=path, detail=str(exc))


def propagate(result: dict[str, Any], nested: dict[str, Any], **extra_context: Any) -> dict[str, Any]:
    """Surface a nested call's failure directly instead of wrapping it in
    a new composed sentence (which would require rendering text in
    Python, defeating the point of code-based errors). extra_context
    values are merged in without overwriting anything the nested result
    already set (e.g. add which group/user was involved, on top of the
    nested error's own context)."""
    result["success"] = False
    result["error_code"] = nested.get("error_code", "system.unknown")
    context = dict(extra_context)
    context.update(nested.get("error_context") or {})
    result["error_context"] = context
    result.pop("error", None)
    return result
