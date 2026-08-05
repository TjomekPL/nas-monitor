from __future__ import annotations

from unittest.mock import patch, MagicMock

from nas_monitor import system_stats


def _counters(read=0, write=0, sent=0, recv=0):
    m = MagicMock()
    m.read_bytes = read
    m.write_bytes = write
    m.bytes_sent = sent
    m.bytes_recv = recv
    return m


def test_get_live_stats_computes_rate_from_two_samples():
    before_disk = _counters(read=1000, write=500)
    after_disk = _counters(read=1000 + 2048, write=500 + 1024)
    before_net = _counters(sent=0, recv=0)
    before_net.bytes_sent, before_net.bytes_recv = 0, 0
    after_net = _counters()
    after_net.bytes_sent, after_net.bytes_recv = 4096, 8192

    fake_mem = MagicMock()
    fake_mem.percent = 45.6
    fake_mem.used = 4 * 1024 ** 3
    fake_mem.total = 16 * 1024 ** 3

    fake_psutil = MagicMock()
    fake_psutil.disk_io_counters.side_effect = [before_disk, after_disk]
    fake_psutil.net_io_counters.side_effect = [before_net, after_net]
    fake_psutil.cpu_percent.return_value = 12.3
    fake_psutil.virtual_memory.return_value = fake_mem

    with patch.object(system_stats, "psutil", fake_psutil):
        result = system_stats.get_live_stats(sample_ms=1000)

    assert result["available"] is True
    assert result["cpu_percent"] == 12.3
    assert result["mem_percent"] == 45.6
    assert result["mem_used_gib"] == 4.0
    assert result["mem_total_gib"] == 16.0
    assert result["disk_read"] == {"value": 2.0, "unit": "KiB/s"}
    assert result["disk_write"] == {"value": 1.0, "unit": "KiB/s"}
    assert result["net_up"] == {"value": 4.0, "unit": "KiB/s"}
    assert result["net_down"] == {"value": 8.0, "unit": "KiB/s"}


def test_get_live_stats_reports_unavailable_when_psutil_missing():
    with patch.object(system_stats, "psutil", None):
        result = system_stats.get_live_stats()
    assert result == {"available": False, "error_code": "statusbar.psutil_missing"}


def test_get_live_stats_reports_unavailable_when_counters_are_none():
    fake_psutil = MagicMock()
    fake_psutil.disk_io_counters.return_value = None
    fake_psutil.net_io_counters.return_value = MagicMock()
    fake_psutil.cpu_percent.return_value = 5.0

    with patch.object(system_stats, "psutil", fake_psutil):
        result = system_stats.get_live_stats(sample_ms=50)

    assert result == {"available": False, "error_code": "statusbar.read_failed"}


def test_get_live_stats_reports_unavailable_on_exception():
    fake_psutil = MagicMock()
    fake_psutil.disk_io_counters.side_effect = RuntimeError("boom")

    with patch.object(system_stats, "psutil", fake_psutil):
        result = system_stats.get_live_stats()

    assert result == {"available": False, "error_code": "statusbar.read_failed"}


def test_human_rate_picks_appropriate_unit():
    assert system_stats._human_rate(512) == {"value": 512.0, "unit": "B/s"}
    assert system_stats._human_rate(2048) == {"value": 2.0, "unit": "KiB/s"}
    assert system_stats._human_rate(5 * 1024 * 1024) == {"value": 5.0, "unit": "MiB/s"}
