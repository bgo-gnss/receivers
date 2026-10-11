"""Tests for the report-only station-lifecycle drift sweep (Step 3).

The sweep's whole value is that it is READ-ONLY: it may only ever produce
cfg_discrepancy rows, never a cfg or TOS write. These tests pin that, plus the
verdict mapping and the "out-of-scope stations produce nothing" rule.

`current_lifecycle` and `_plan_station_status` are deliberately NOT mocked —
the fixtures are TOS-shaped so the real (already-tested) derivation and planner
run. That is the point: the sweep must report exactly what `cfg sync-from-tos`
would propose, and only using the real code proves it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import pytest

from receivers.cfg import lifecycle_sweep as ls
from receivers.cfg.discrepancy_log import DETECTED_BY_LIFECYCLE_SWEEP
from receivers.cfg.reconciler import Verdict

# ---------------------------------------------------------------------------
# TOS-shaped fixtures (feed the REAL tos_adapter.current_lifecycle)
# ---------------------------------------------------------------------------


def _open_receiver(*, date_end: Optional[str] = None) -> Dict[str, Any]:
    """TOS record for a station with a live receiver attached."""
    rec: Dict[str, Any] = {
        "device_history": [
            {
                "time_from": "2019-01-01T00:00:00",
                "time_to": None,
                "gnss_receiver": {"model": "SEPT POLARX5", "serial_number": "3093129"},
            }
        ]
    }
    if date_end:
        rec["date_end"] = date_end
    return rec


def _no_receiver(*, date_end: Optional[str] = None) -> Dict[str, Any]:
    rec: Dict[str, Any] = {"device_history": []}
    if date_end:
        rec["date_end"] = date_end
    return rec


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def sweep(monkeypatch):
    """Patch cfg-read + TOS-query + the discrepancy writer.

    Returns a callable: ``run(cfg_fields, tos_by_sid) -> recorded rows``.
    """

    def _run(cfg_fields, tos_by_sid, *, limit=None):
        import receivers.cfg.probe as probe

        recorded: list = []
        monkeypatch.setattr(ls, "_cfg_lifecycle_fields", lambda: cfg_fields)
        monkeypatch.setattr(probe, "_query_tos", lambda sid: tos_by_sid.get(sid))

        def fake_sync_station(sid, entries, *, detected_by, **_kw):
            for e in entries:
                recorded.append(
                    {
                        "sid": sid,
                        "cfg_key": e.cfg_key,
                        "verdict": e.verdict,
                        "cfg_value": e.cfg_value,
                        "tos_value": e.tos_value,
                        "detected_by": detected_by,
                    }
                )
            return len(entries)

        monkeypatch.setattr(ls, "sync_station", fake_sync_station)
        result = ls.run_lifecycle_sweep(limit=limit)
        return result, recorded

    return _run


def _cfg(status=None, *, health_check=None, role=None):
    return {
        "station_status": status,
        "health_check": health_check,
        "station_role": role,
    }


# ---------------------------------------------------------------------------
# _verdict_for — the label mapping
# ---------------------------------------------------------------------------


class TestVerdictFor:
    def test_cfg_empty_is_missing(self):
        assert ls._verdict_for(("set", "inactive"), None) == Verdict.MISSING
        assert ls._verdict_for(("set", "inactive"), "") == Verdict.MISSING

    def test_cfg_present_and_differs_is_conflict(self):
        assert ls._verdict_for(("set", "inactive"), "active") == Verdict.CONFLICT
        assert ls._verdict_for(("clear", None), "inactive") == Verdict.CONFLICT

    def test_tos_self_contradiction_is_sources_disagree(self):
        plan = ("flag", "TOS date_end is set but an open gnss_receiver join ... hygiene")
        assert ls._verdict_for(plan, "active") == Verdict.SOURCES_DISAGREE

    def test_non_hygiene_flag_is_conflict(self):
        # e.g. the cfg-discontinued-but-TOS-alive "resurrection candidate" flag:
        # that is a cfg-vs-TOS disagreement, not TOS disagreeing with itself.
        plan = ("flag", "cfg says discontinued but TOS shows an open receiver join")
        assert ls._verdict_for(plan, "discontinued") == Verdict.CONFLICT


# ---------------------------------------------------------------------------
# The sweep itself
# ---------------------------------------------------------------------------


class TestRunLifecycleSweep:
    def test_in_sync_produces_auto_resolve_only(self, sweep):
        """cfg active + TOS has an open receiver → nothing to report, and any
        previously-open row is closed (verdict=None)."""
        result, recorded = sweep(
            {"HAMR": _cfg(None)},
            {"HAMR": _open_receiver()},
        )
        assert result.drift == 0
        assert result.in_scope == 1
        assert len(recorded) == 1
        assert recorded[0]["verdict"] is None  # auto-resolve, not a detection
        assert recorded[0]["detected_by"] == DETECTED_BY_LIFECYCLE_SWEEP

    def test_stale_inactive_is_reported_as_conflict(self, sweep):
        """The HAMR/KOTC class: cfg says inactive but TOS shows a live receiver.
        Recorded, NOT applied — the sweep has no write path."""
        result, recorded = sweep(
            {"HAMR": _cfg("inactive")},
            {"HAMR": _open_receiver()},
        )
        assert result.drift == 1
        detection = [r for r in recorded if r["verdict"] is not None]
        assert len(detection) == 1
        assert detection[0]["verdict"] == Verdict.CONFLICT
        assert detection[0]["cfg_value"] == "inactive"
        assert detection[0]["tos_value"] == "active"
        assert detection[0]["detected_by"] == DETECTED_BY_LIFECYCLE_SWEEP
        assert result.findings[0]["proposed"] == "clear:None"

    def test_cfg_empty_but_tos_has_lifecycle_is_missing(self, sweep):
        result, recorded = sweep(
            {"NEWC": _cfg(None)},
            {"NEWC": _no_receiver()},
        )
        assert result.drift == 1
        detection = [r for r in recorded if r["verdict"] is not None]
        assert detection[0]["verdict"] == Verdict.MISSING
        assert detection[0]["tos_value"] == "inactive"

    def test_discontinued_reported(self, sweep):
        result, recorded = sweep(
            {"OLDY": _cfg("active")},
            {"OLDY": _no_receiver(date_end="2020-03-26")},
        )
        assert result.drift == 1
        detection = [r for r in recorded if r["verdict"] is not None]
        assert detection[0]["verdict"] == Verdict.CONFLICT
        assert detection[0]["tos_value"] == "discontinued"

    def test_tos_self_contradiction_is_sources_disagree(self, sweep):
        """date_end set AND an open receiver join — TOS disagrees with itself.
        Flagged, never auto-resolved."""
        result, recorded = sweep(
            {"ODD1": _cfg("active")},
            {"ODD1": _open_receiver(date_end="2020-03-26")},
        )
        assert result.drift == 1
        detection = [r for r in recorded if r["verdict"] is not None]
        assert detection[0]["verdict"] == Verdict.SOURCES_DISAGREE

    def test_out_of_scope_stations_produce_nothing(self, sweep):
        """Passive/external stations may legitimately have no receiver in TOS.
        They must get NO row at all — not even an auto-resolve — because the
        sweep has no opinion about them."""
        result, recorded = sweep(
            {
                "KRAC": _cfg(None, health_check="passive"),
                "TORK": _cfg(None, role="passive"),
            },
            {"KRAC": _no_receiver(), "TORK": _no_receiver()},
        )
        assert result.in_scope == 0
        assert result.drift == 0
        assert recorded == []

    def test_out_of_scope_never_reports_even_on_wild_mismatch(self, sweep):
        result, recorded = sweep(
            {"KRAC": _cfg("inactive", health_check="passive")},
            {"KRAC": _open_receiver()},
        )
        assert result.drift == 0
        assert recorded == []

    def test_tos_unavailable_is_counted_not_fatal(self, sweep):
        """One station missing from TOS must not end the pass, and must not be
        recorded as drift (absent TOS is not evidence of disagreement)."""
        result, recorded = sweep(
            {"GONE": _cfg("active"), "HAMR": _cfg("inactive")},
            {"HAMR": _open_receiver()},  # GONE has no TOS record
        )
        assert result.tos_failures == 1
        assert result.drift == 1  # only HAMR
        assert all(r["sid"] != "GONE" for r in recorded)

    def test_tos_exception_is_isolated(self, sweep, monkeypatch):
        import receivers.cfg.probe as probe

        monkeypatch.setattr(ls, "_cfg_lifecycle_fields", lambda: {"BOMB": _cfg(None)})

        def boom(sid):
            raise RuntimeError("tos exploded")

        monkeypatch.setattr(probe, "_query_tos", boom)
        monkeypatch.setattr(ls, "sync_station", lambda *a, **k: 0)

        result = ls.run_lifecycle_sweep()  # must not raise
        assert result.tos_failures == 1
        assert result.drift == 0

    def test_no_cfg_sections_short_circuits(self, sweep):
        result, recorded = sweep({}, {})
        assert result.stations_checked == 0
        assert recorded == []

    def test_limit_caps_stations(self, sweep):
        cfg = {f"AA{i:02d}": _cfg(None) for i in range(5)}
        tos = {sid: _open_receiver() for sid in cfg}
        result, _ = sweep(cfg, tos, limit=2)
        assert result.in_scope == 2

    def test_never_writes_cfg_or_tos(self, sweep, monkeypatch):
        """Hard guarantee: the sweep's only write is to cfg_discrepancy.

        Fails loudly if any cfg/TOS write helper is ever reached from here.
        """
        import receivers.config.receivers_config as rc

        def forbidden(*a, **k):  # pragma: no cover - only runs on regression
            raise AssertionError("lifecycle sweep attempted a cfg/TOS write!")

        monkeypatch.setattr(rc, "_update_cfg_field", forbidden)
        monkeypatch.setattr(rc, "_remove_cfg_field", forbidden)

        result, _ = sweep(
            {"HAMR": _cfg("inactive")},
            {"HAMR": _open_receiver()},
        )
        assert result.drift == 1  # detected and reported, without writing


# ---------------------------------------------------------------------------
# Job wrapper
# ---------------------------------------------------------------------------


class TestSweepJob:
    def test_job_never_raises_when_sweep_explodes(self, monkeypatch):
        monkeypatch.setattr(
            ls, "run_lifecycle_sweep", lambda *a, **k: (_ for _ in ()).throw(RuntimeError)
        )
        ls._run_lifecycle_sweep_job({})  # must not raise

    def test_job_logs_each_drift(self, monkeypatch, caplog):
        """The wrapper logs an actionable DRIFT line per finding — the rows go
        to cfg_discrepancy, but a log line is what an operator watching the
        scheduler actually sees.

        Patched with monkeypatch (not raw attribute assignment) so the test
        doubles stay type-compatible with the real functions.
        """
        import receivers.cfg.probe as probe

        monkeypatch.setattr(ls, "_cfg_lifecycle_fields", lambda: {"HAMR": _cfg("inactive")})
        monkeypatch.setattr(probe, "_query_tos", lambda sid: _open_receiver())
        monkeypatch.setattr(ls, "sync_station", lambda *a, **k: 1)

        with caplog.at_level(logging.WARNING, logger=ls.__name__):
            ls._run_lifecycle_sweep_job({})

        assert any("lifecycle-sweep DRIFT HAMR" in r.message for r in caplog.records)

    def test_job_uses_stations_filter_from_config(self, monkeypatch):
        seen = {}

        def fake(sid_list, **kw):
            seen["stations"] = sid_list
            return ls.SweepResult()

        monkeypatch.setattr(ls, "run_lifecycle_sweep", fake)
        ls._run_lifecycle_sweep_job({"stations": ["HAMR", "KOTC"]})
        assert seen["stations"] == ["HAMR", "KOTC"]


# ---------------------------------------------------------------------------
# Scheduler registration / default-off gate
# ---------------------------------------------------------------------------


def _sched_stub(yaml_config):
    """BulkDownloadScheduler shell + the MagicMock standing in for its scheduler.

    Returns ``(sched, mock)`` — and the mock is returned SEPARATELY because
    ``BulkDownloadScheduler.scheduler`` is annotated with the real APScheduler
    type, so pyright reads ``sched.scheduler.add_job`` as a BOUND METHOD of that
    class and rejects ``.assert_not_called()`` / ``.call_args_list`` on it.
    Holding the typed MagicMock in its own variable keeps the assertions legal.
    """
    import logging as _logging
    from unittest.mock import MagicMock

    from receivers.scheduling import bulk_scheduler as bs

    sched = bs.BulkDownloadScheduler.__new__(bs.BulkDownloadScheduler)
    sched.logger = _logging.getLogger("receivers.test")
    sched._disabled_jobs = []
    mock = MagicMock()
    sched.scheduler = mock
    sched.yaml_config = yaml_config
    return sched, mock


class TestSchedulerRegistration:
    def test_disabled_by_default(self):
        """Inert unless scheduler.yaml opts in — the review scoped this as
        'optional, after Steps 0-2 have soaked'."""
        sched, mock = _sched_stub({})
        sched._schedule_lifecycle_sweep()
        mock.add_job.assert_not_called()
        assert "lifecycle_sweep" in sched._disabled_jobs

    def test_enabled_schedules_one_daily_job(self):
        sched, mock = _sched_stub({"lifecycle_sweep": {"enabled": True}})
        sched._schedule_lifecycle_sweep()
        mock.add_job.assert_called_once()
        kwargs = mock.add_job.call_args.kwargs
        assert kwargs["id"] == "lifecycle_sweep"
        # Runs on the backfill executor so it never competes with live downloads.
        assert kwargs["executor"] == "backfill"
        assert kwargs["max_instances"] == 1

    def test_no_startup_immediate_run(self):
        """Deliberately unlike the horizon probe: an immediate run would fire
        the whole ~190-call TOS pass on every scheduler restart (and every
        deploy restarts it). Exactly ONE job must be registered."""
        sched, mock = _sched_stub({"lifecycle_sweep": {"enabled": True}})
        sched._schedule_lifecycle_sweep()
        ids = [c.kwargs.get("id") for c in mock.add_job.call_args_list]
        assert ids == ["lifecycle_sweep"]
        assert not any(i and i.endswith("_startup") for i in ids)
