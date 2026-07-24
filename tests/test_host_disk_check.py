"""Tests for the host disk + liveness Icinga/Nagios check (monitoring.host_disk_check).

The evaluators are pure over the filesystem: disk usage via shutil.disk_usage
(monkeypatched here) and activity freshness via file mtimes (real tmp files).
Covers the disk ladder (ok/warn/crit/missing), the liveness ladder
(fresh/aging/stale/none), and worst-of aggregation incl. WARN outranking UNKNOWN.
"""

from __future__ import annotations

import time
from collections import namedtuple

import pytest

from receivers.monitoring import host_disk_check as hdc
from receivers.monitoring.host_disk_check import (
    NAGIOS_CRITICAL,
    NAGIOS_OK,
    NAGIOS_UNKNOWN,
    NAGIOS_WARNING,
    evaluate_disk,
    evaluate_forecast,
    evaluate_host,
    evaluate_liveness,
)

_Usage = namedtuple("_Usage", "total used free")


def _fake_disk_usage(pct_by_path):
    """Return a shutil.disk_usage stub yielding the given percent-used per path."""

    def _inner(path):
        pct = pct_by_path[str(path)]
        total = 100
        used = pct
        return _Usage(total=total, used=used, free=total - used)

    return _inner


# --- disk ladder ----------------------------------------------------------


@pytest.mark.parametrize(
    "pct,expected",
    [
        (50, NAGIOS_OK),
        (85, NAGIOS_WARNING),
        (92, NAGIOS_CRITICAL),
        (100, NAGIOS_CRITICAL),
    ],
)
def test_disk_thresholds(monkeypatch, tmp_path, pct, expected):
    monkeypatch.setattr(
        hdc.shutil, "disk_usage", _fake_disk_usage({str(tmp_path): pct})
    )
    exit_status, reasons, perf = evaluate_disk(
        [str(tmp_path)], warn_pct=85, crit_pct=92
    )
    assert exit_status == expected
    assert perf and perf[0].startswith("disk_")


def test_disk_missing_mount_is_unknown(tmp_path):
    missing = tmp_path / "does_not_exist"
    exit_status, reasons, perf = evaluate_disk([str(missing)], warn_pct=85, crit_pct=92)
    assert exit_status == NAGIOS_UNKNOWN
    assert "not present" in reasons[0]
    assert perf == [f"disk_{hdc._sanitize(str(missing))}=U"]


def test_disk_worst_of_multiple(monkeypatch, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr(
        hdc.shutil,
        "disk_usage",
        _fake_disk_usage({str(a): 50, str(b): 95}),
    )
    exit_status, reasons, _ = evaluate_disk([str(a), str(b)], warn_pct=85, crit_pct=92)
    assert exit_status == NAGIOS_CRITICAL


# --- liveness ladder ------------------------------------------------------


def _touch_age(path, minutes_old):
    path.write_text("x")
    old = time.time() - minutes_old * 60
    import os

    os.utime(path, (old, old))


@pytest.mark.parametrize(
    "age_min,expected",
    [(0, NAGIOS_OK), (25, NAGIOS_WARNING), (90, NAGIOS_CRITICAL)],
)
def test_liveness_thresholds(tmp_path, age_min, expected):
    f = tmp_path / "download_audit.jsonl"
    _touch_age(f, age_min)
    exit_status, reasons, perf = evaluate_liveness(
        [str(f)], warn_minutes=20, crit_minutes=60
    )
    assert exit_status == expected
    assert perf[0].startswith("activity_age_min=")


def test_liveness_newest_wins(tmp_path):
    """A fresh heartbeat rescues an otherwise-stale audit trail."""
    stale = tmp_path / "download_audit.jsonl"
    fresh = tmp_path / "heartbeat"
    _touch_age(stale, 200)
    _touch_age(fresh, 1)
    exit_status, _, _ = evaluate_liveness(
        [str(stale), str(fresh)], warn_minutes=20, crit_minutes=60
    )
    assert exit_status == NAGIOS_OK


def test_liveness_no_files_is_unknown(tmp_path):
    exit_status, reasons, perf = evaluate_liveness(
        [str(tmp_path / "nope")], warn_minutes=20, crit_minutes=60
    )
    assert exit_status == NAGIOS_UNKNOWN
    assert perf == ["activity_age_min=U"]


# --- forecast (days-to-full) ---------------------------------------------


def _patch_forecast(monkeypatch, days_by_vol):
    """Patch archive.prune.record_and_forecast to return canned days-to-full."""

    def _fake(volume, state_path, *, warn_days_to_full, today=None):
        val = days_by_vol[str(volume)]
        if isinstance(val, Exception):
            raise val
        return val

    monkeypatch.setattr("receivers.archive.prune.record_and_forecast", _fake)


def test_forecast_empty_is_ok_no_perfdata():
    exit_status, reasons, perf = evaluate_forecast(
        [], state_path="x", warn_days=21, crit_days=7
    )
    assert exit_status == NAGIOS_OK
    assert reasons == [] and perf == []


@pytest.mark.parametrize(
    "days,expected",
    [(None, NAGIOS_OK), (40, NAGIOS_OK), (15, NAGIOS_WARNING), (5, NAGIOS_CRITICAL)],
)
def test_forecast_thresholds(monkeypatch, days, expected):
    _patch_forecast(monkeypatch, {"/mnt/rawgpsdata": days})
    exit_status, _, perf = evaluate_forecast(
        ["/mnt/rawgpsdata"], state_path="x", warn_days=21, crit_days=7
    )
    assert exit_status == expected
    assert perf[0].startswith("days_to_full_")


def test_forecast_volume_failure_is_unknown(monkeypatch):
    _patch_forecast(monkeypatch, {"/mnt/rawgpsdata": RuntimeError("statvfs boom")})
    exit_status, reasons, perf = evaluate_forecast(
        ["/mnt/rawgpsdata"], state_path="x", warn_days=21, crit_days=7
    )
    assert exit_status == NAGIOS_UNKNOWN
    assert perf == ["days_to_full_mnt_rawgpsdata=U"]


def test_host_folds_forecast_crit(monkeypatch, tmp_path):
    """A CRIT forecast drives the overall result even when disk + liveness are OK."""
    mount = tmp_path / "m"
    mount.mkdir()
    beat = tmp_path / "heartbeat"
    _touch_age(beat, 0)
    monkeypatch.setattr(hdc.shutil, "disk_usage", _fake_disk_usage({str(mount): 40}))
    _patch_forecast(monkeypatch, {"/mnt/rawgpsdata": 3})
    result = evaluate_host(
        mounts=[str(mount)],
        activity_files=[str(beat)],
        warn_pct=85,
        crit_pct=92,
        activity_warn_minutes=20,
        activity_crit_minutes=60,
        forecast_volumes=["/mnt/rawgpsdata"],
    )
    assert result.exit_status == NAGIOS_CRITICAL
    assert "to full" in result.summary


# --- aggregation ----------------------------------------------------------


def test_host_worst_of_and_warn_outranks_unknown(monkeypatch, tmp_path):
    """A WARN disk + UNKNOWN liveness aggregates to WARN, not UNKNOWN."""
    mount = tmp_path / "m"
    mount.mkdir()
    monkeypatch.setattr(hdc.shutil, "disk_usage", _fake_disk_usage({str(mount): 88}))
    result = evaluate_host(
        mounts=[str(mount)],
        activity_files=[str(tmp_path / "absent")],  # -> UNKNOWN liveness
        warn_pct=85,
        crit_pct=92,
        activity_warn_minutes=20,
        activity_crit_minutes=60,
    )
    assert result.exit_status == NAGIOS_WARNING


def test_host_all_ok(monkeypatch, tmp_path):
    mount = tmp_path / "m"
    mount.mkdir()
    beat = tmp_path / "heartbeat"
    _touch_age(beat, 0)
    monkeypatch.setattr(hdc.shutil, "disk_usage", _fake_disk_usage({str(mount): 40}))
    result = evaluate_host(
        mounts=[str(mount)],
        activity_files=[str(beat)],
        warn_pct=85,
        crit_pct=92,
        activity_warn_minutes=20,
        activity_crit_minutes=60,
    )
    assert result.exit_status == NAGIOS_OK
    assert "OK" in result.plugin_output
