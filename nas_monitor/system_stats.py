"""
nas_monitor.system_stats
--------------------------
Live CPU / disk-throughput / network-throughput readout for the
statusbar. Read-only, like nas_monitor.monitor - nothing here ever
writes to disk or touches system config.

Disk and network counters exposed by the OS (psutil.disk_io_counters,
psutil.net_io_counters) are cumulative since boot, not a rate - turning
them into MiB/s needs two samples a known distance apart. Rather than
keep state between requests (which would be per-gunicorn-worker and
give a wrong first reading whenever a request lands on a different
worker than the previous one - see network_mutate.py's comments on the
same multi-worker issue), each call here samples twice itself with a
short sleep in between. That costs `sample_ms` of extra request
latency (default 200ms), but the number returned is always correct and
self-contained, independent of which worker served the previous poll.
"""

from __future__ import annotations

import time
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - exercised only when the
    # optional dependency truly isn't installed; the API layer turns
    # this into a status field instead of a 500.
    psutil = None


def _human_rate(bytes_per_sec: float) -> dict[str, Any]:
    """IEC binary units, matching monitor.py's _human_size - MiB/s not
    the decimal MB/s some tools use."""
    value = float(bytes_per_sec)
    for unit in ("B/s", "KiB/s", "MiB/s", "GiB/s"):
        if value < 1024 or unit == "GiB/s":
            return {"value": round(value, 1), "unit": unit}
        value /= 1024
    return {"value": round(value, 1), "unit": "GiB/s"}


def get_live_stats(sample_ms: int = 200) -> dict[str, Any]:
    """CPU percent + aggregate disk read/write and network up/down
    throughput, sampled over a `sample_ms` window. Returns
    {"available": False, "error_code": ...} if psutil isn't installed
    or a counter genuinely isn't available on this system (e.g. no
    disk_io_counters permission in some containers) - callers show
    that as a dash rather than a crash, same convention as monitor.py."""
    if psutil is None:
        return {"available": False, "error_code": "statusbar.psutil_missing"}

    try:
        disk_before = psutil.disk_io_counters()
        net_before = psutil.net_io_counters()
        cpu_percent = psutil.cpu_percent(interval=sample_ms / 1000)
        disk_after = psutil.disk_io_counters()
        net_after = psutil.net_io_counters()
    except Exception:
        return {"available": False, "error_code": "statusbar.read_failed"}

    if disk_before is None or disk_after is None or net_before is None or net_after is None:
        return {"available": False, "error_code": "statusbar.read_failed"}

    elapsed = max(sample_ms / 1000, 0.001)
    read_rate = (disk_after.read_bytes - disk_before.read_bytes) / elapsed
    write_rate = (disk_after.write_bytes - disk_before.write_bytes) / elapsed
    up_rate = (net_after.bytes_sent - net_before.bytes_sent) / elapsed
    down_rate = (net_after.bytes_recv - net_before.bytes_recv) / elapsed

    return {
        "available": True,
        "cpu_percent": round(cpu_percent, 1),
        "disk_read": _human_rate(max(read_rate, 0)),
        "disk_write": _human_rate(max(write_rate, 0)),
        "net_up": _human_rate(max(up_rate, 0)),
        "net_down": _human_rate(max(down_rate, 0)),
    }
