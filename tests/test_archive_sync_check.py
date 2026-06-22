"""Tests for the archive-sync Icinga/Nagios check (monitoring.archive_sync_check).

evaluate_archive_sync is pure over a DB connection — exercised here with a fake
conn that routes by SQL: sync_state → last_success_ts, file_tracking → missing
count. Covers the freshness ladder (fresh/aging/stale/never) and the missing-15s
ladder (ok/warn/crit), plus worst-of aggregation.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from receivers.monitoring.archive_sync_check import (
    NAGIOS_CRITICAL,
    NAGIOS_OK,
    NAGIOS_UNKNOWN,
    NAGIOS_WARNING,
    evaluate_archive_sync,
)

NOW = datetime(2026, 6, 22, 12, 0, 0)


class _FakeCursor:
    def __init__(self, last_success, missing):
        self._last_success = last_success
        self._missing = missing
        self._pending = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "sync_state" in sql:
            self._pending = (self._last_success,)
        elif "file_tracking" in sql:
            if self._missing == "raise":
                raise RuntimeError("simulated DB error")
            self._pending = (self._missing,)
        else:
            self._pending = None

    def fetchone(self):
        return self._pending


class _FakeConn:
    def __init__(self, last_success, missing):
        self._last_success = last_success
        self._missing = missing

    def cursor(self):
        return _FakeCursor(self._last_success, self._missing)


def _eval(last_success, missing, **kw):
    return evaluate_archive_sync(_FakeConn(last_success, missing), now=NOW, **kw)


class TestFreshness:
    def test_fresh_and_clean_is_ok(self):
        r = _eval(NOW - timedelta(minutes=30), 0)
        assert r.exit_status == NAGIOS_OK
        assert "fresh" in r.summary

    def test_aging_is_warning(self):
        r = _eval(NOW - timedelta(minutes=150), 0, max_age_minutes=120)
        assert r.exit_status == NAGIOS_WARNING
        assert "aging" in r.summary

    def test_stale_is_critical(self):
        r = _eval(NOW - timedelta(minutes=300), 0, max_age_minutes=120)
        assert r.exit_status == NAGIOS_CRITICAL
        assert "stale" in r.summary

    def test_never_synced_is_critical(self):
        r = _eval(None, 0)
        assert r.exit_status == NAGIOS_CRITICAL
        assert "no successful" in r.summary
        assert "sync_age_min=U" in r.perfdata


class TestMissing15s:
    def test_few_missing_is_ok(self):
        r = _eval(
            NOW - timedelta(minutes=10), 3, missing_15s_warn=5, missing_15s_crit=15
        )
        assert r.exit_status == NAGIOS_OK

    def test_warn_threshold(self):
        r = _eval(
            NOW - timedelta(minutes=10), 7, missing_15s_warn=5, missing_15s_crit=15
        )
        assert r.exit_status == NAGIOS_WARNING
        assert "7 15s_24hr" in r.summary

    def test_crit_threshold(self):
        r = _eval(
            NOW - timedelta(minutes=10), 20, missing_15s_warn=5, missing_15s_crit=15
        )
        assert r.exit_status == NAGIOS_CRITICAL

    def test_query_error_is_unknown(self):
        r = _eval(NOW - timedelta(minutes=10), "raise")
        assert r.exit_status == NAGIOS_UNKNOWN
        assert "missing_15s=U" in r.perfdata


class TestWorstOf:
    def test_takes_worst_signal(self):
        # fresh sync (OK) but a missing-15s spike (CRIT) → overall CRIT
        r = _eval(NOW - timedelta(minutes=10), 20)
        assert r.exit_status == NAGIOS_CRITICAL

    def test_perfdata_always_present(self):
        r = _eval(NOW - timedelta(minutes=10), 2)
        assert "sync_age_min=" in r.perfdata
        assert "missing_15s=2" in r.perfdata

    def test_plugin_output_prefixes_label(self):
        assert _eval(NOW - timedelta(minutes=10), 0).plugin_output.startswith("OK - ")
        assert _eval(None, 0).plugin_output.startswith("CRITICAL - ")
