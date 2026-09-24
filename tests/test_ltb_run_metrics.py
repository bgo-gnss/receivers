"""LTB run metrics — the row behind the `done:` log line.

Until this, an LTB run reported itself only as a log line nobody consumed, and
the one silent failure it has — stopping on the wall-clock cap while every
pipeline dashboard stays green — was invisible. These tests pin:

* a completed run writes exactly one row, of the right kind, stop_reason NULL;
* a run that hits the CLOCK records that; one that hits SLOTS records that;
* the row's outcome totals are the SUM of the per-station reports;
* **a DB failure during recording does NOT propagate** — the run returns its
  normal result and logs a WARNING (recording must never break a run that
  moves real scientific data);
* the reconnection trigger records ITS kind with ITS ceiling, not the daily one;
* `recovered` / `failed` now survive on the report (they were locals, logged
  and dropped, so the run could not aggregate its own outcomes);
* the writer goes through the fan-out connection (NOT single_host) on a
  natural-key upsert — that is the existing mechanism by which the row reaches
  pgdev, where grafana.vedur.is reads.

Every DB touch is mocked at `DatabaseConnectionFactory.connection`; run with
`-p no_network_plugin` and nothing here can reach a socket.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from receivers.scheduling import ltb_run_metrics as met
from receivers.scheduling.long_term_backfill import (
    LongTermGapReport,
    RunBudget,
    _reset_attempt_history,
    _reset_rotation,
    _run_long_term_backfill_job,
    _run_reconnection_backfill_job,
    run_long_term_backfill_station,
)

LTB = "receivers.scheduling.long_term_backfill"
DBF = "receivers.health.database_factory.DatabaseConnectionFactory.connection"
MET_LOGGER = "receivers.scheduler.long_term_backfill.metrics"
SESSIONS = ["15s_24hr", "1Hz_1hr"]


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


class _Sink:
    """Stands in for the DB: captures every statement the recorder executes.

    ``fail_connect`` raises from ``connection()`` itself (DB down);
    ``fail_execute`` raises from ``cursor.execute`` (table missing, bad SQL).
    ``connectivity`` is what a ``single_host=True`` connection's ``fetchall``
    returns — the reconnection job's candidate query.
    """

    def __init__(self, fail_connect=None, fail_execute=None, connectivity=()):
        self.rows: list[tuple[str, tuple]] = []
        self.calls: list[dict] = []
        self.fail_connect = fail_connect
        self.fail_execute = fail_execute
        self.connectivity = list(connectivity)

    def connection(self, *args, **kwargs):
        self.calls.append(kwargs)
        cm = MagicMock()
        cur = cm.__enter__.return_value.cursor.return_value.__enter__.return_value
        if kwargs.get("single_host"):
            cur.fetchall.return_value = self.connectivity
            return cm
        if self.fail_connect:
            raise self.fail_connect

        def _execute(sql, params=None):
            if self.fail_execute:
                raise self.fail_execute
            self.rows.append((sql, params))

        cur.execute.side_effect = _execute
        return cm

    def recorded(self) -> list[dict]:
        return [dict(zip(met._COLUMNS, params)) for _sql, params in self.rows]


def _report(sid="AAAA", session="15s_24hr", queued=1, **outcomes) -> LongTermGapReport:
    r = LongTermGapReport(
        sid=sid, session=session, start=date(2026, 8, 1), end=date(2026, 8, 31)
    )
    r.queued = [SimpleNamespace(file_date=date(2026, 8, 1)) for _ in range(queued)]
    for k, v in outcomes.items():
        setattr(r, k, v)
    return r


def _run_daily(sink, station, stations=("AAAA",), **job_kw):
    """Drive the daily job with the fleet, the worker and the DB all stubbed."""
    cfgs = {sid: {} for sid in stations}
    job_kw.setdefault("sessions", SESSIONS)
    job_kw.setdefault("lookback_days", 30)
    job_kw.setdefault("max_workers", 1)
    with (
        patch("receivers.cli.main.get_all_station_configs", return_value=cfgs),
        patch(f"{LTB}.run_long_term_backfill_station", side_effect=station),
        patch(DBF, side_effect=sink.connection),
    ):
        return _run_long_term_backfill_job(**job_kw)


@pytest.fixture(autouse=True)
def _fresh_state():
    _reset_rotation()
    _reset_attempt_history()
    yield
    _reset_rotation()
    _reset_attempt_history()


# --------------------------------------------------------------------------
# 1. a completed run -> exactly one row, right kind, stop_reason NULL
# --------------------------------------------------------------------------


class TestCompletedRun:
    def test_writes_exactly_one_daily_row_with_no_stop_reason(self):
        sink = _Sink()
        _run_daily(
            sink,
            lambda sid, session, **kw: _report(sid, session, recovered=2),
            max_run_seconds=3600,
            max_slots_per_run=600,
        )
        rows = sink.recorded()
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["run_kind"] == "daily"
        assert row["stop_reason"] is None and row["stop_detail"] is None
        assert row["max_run_seconds"] == 3600 and row["max_slots"] == 600
        assert row["stations"] == 1 and row["sessions"] == SESSIONS
        assert row["lookback"] == "15s_24hr=30d 1Hz_1hr=30d"
        assert isinstance(row["started_at"], datetime)
        assert row["started_at"].tzinfo is not None
        assert row["finished_at"] >= row["started_at"]
        assert row["duration_seconds"] >= 0

    def test_the_insert_is_the_writers_shape(self):
        """One INSERT on the natural key. Pinned because a mirror replay
        must be a no-op, not a duplicate — and never id-keyed."""
        sink = _Sink()
        _run_daily(sink, lambda sid, session, **kw: _report(sid, session))
        sql, _ = sink.rows[0]
        assert sql.startswith("INSERT INTO ltb_runs")
        assert "ON CONFLICT (run_kind, started_at) DO NOTHING" in sql

    def test_writer_uses_the_fan_out_connection_not_single_host(self):
        """The row reaches pgdev (where grafana.vedur.is reads) ONLY via the
        database_factory mirror fan-out, which single_host=True skips. Every
        other query in the LTB module is single_host — this one must not be."""
        sink = _Sink()
        _run_daily(sink, lambda sid, session, **kw: _report(sid, session))
        assert sink.calls, "recorder never opened a connection"
        assert all(not c.get("single_host") for c in sink.calls), sink.calls


# --------------------------------------------------------------------------
# 2. which ceiling stopped the run
# --------------------------------------------------------------------------


class TestStopReason:
    def test_wall_clock_is_recorded(self):
        sink = _Sink()
        _run_daily(
            sink,
            lambda sid, session, **kw: _report(sid, session),
            max_run_seconds=0.0,  # exhausted before the first station
            max_slots_per_run=600,
        )
        (row,) = sink.recorded()
        assert row["stop_reason"] == "wall-clock"
        assert row["stop_detail"].startswith("wall-clock ")
        assert row["max_run_seconds"] == 0.0

    def test_slots_is_recorded(self):
        def station(sid, session, budget=None, **kw):
            budget.take(5)  # a real worker reserves; the cap trims it
            return _report(sid, session)

        sink = _Sink()
        _run_daily(sink, station, max_run_seconds=None, max_slots_per_run=1)
        (row,) = sink.recorded()
        assert row["stop_reason"] == "slots"
        assert row["stop_detail"] == "slots 1 >= 1"
        assert row["slots_used"] == 1 and row["max_slots"] == 1
        assert row["max_run_seconds"] is None, "unbounded clock is NULL, not 0"

    @pytest.mark.parametrize(
        "reason, expected",
        [
            (None, None),
            ("", None),
            ("wall-clock 3601s >= 3600s", "wall-clock"),
            ("slots 600 >= 600", "slots"),
            ("memory 9G >= 8G", "other"),  # a future ceiling still records
        ],
    )
    def test_categorical_mapping(self, reason, expected):
        assert met.stop_reason_from(reason) == expected


# --------------------------------------------------------------------------
# 3. totals are the sum of the per-station reports
# --------------------------------------------------------------------------


class TestAggregation:
    def test_row_totals_equal_the_sum_of_the_reports(self):
        per_call = {
            ("AAAA", "15s_24hr"): dict(
                queued=3,
                recovered=2,
                nothing_on_receiver=1,
                failed=0,
                unreachable_slots=0,
                budget_capped=0,
            ),
            ("AAAA", "1Hz_1hr"): dict(
                queued=10,
                recovered=0,
                nothing_on_receiver=4,
                failed=1,
                unreachable_slots=1,
                budget_capped=4,
            ),
            ("BBBB", "15s_24hr"): dict(queued=0),  # classified clean
            ("BBBB", "1Hz_1hr"): dict(queued=7, skipped_offline=True),
        }

        def station(sid, session, **kw):
            return _report(sid, session, **per_call[(sid, session)])

        sink = _Sink()
        _run_daily(sink, station, stations=("AAAA", "BBBB"))
        (row,) = sink.recorded()
        assert row["stations"] == 2
        assert row["queued_station_sessions"] == 3  # BBBB/15s had none
        assert row["recovered"] == 2
        assert row["nothing_on_receiver"] == 5
        assert row["failed"] == 1
        assert row["unreachable"] == 1
        assert row["budget_capped"] == 4
        assert row["skipped_offline"] == 1
        assert row["errors"] == 0

    def test_a_station_that_raises_is_counted_not_summed(self):
        def station(sid, session, **kw):
            if sid == "BBBB":
                raise RuntimeError("driver exploded")
            return _report(sid, session, recovered=1)

        sink = _Sink()
        _run_daily(sink, station, stations=("AAAA", "BBBB"))
        (row,) = sink.recorded()
        assert row["errors"] == 2, "one station x two sessions raised"
        assert row["recovered"] == 2

    def test_partial_report_objects_count_as_zero(self):
        """The job tests drive the job with SimpleNamespace reports carrying
        only queued/skipped_offline; a missing field is 0, never an error."""
        tot = met.aggregate_reports(
            [SimpleNamespace(queued=[1], skipped_offline=False), None]
        )
        assert tot["queued_station_sessions"] == 1
        assert tot["recovered"] == 0 and tot["failed"] == 0


# --------------------------------------------------------------------------
# 4. recording can never break the run
# --------------------------------------------------------------------------


class TestRecordingNeverBreaksTheRun:
    def _assert_run_survives(self, sink, caplog):
        with caplog.at_level(logging.WARNING, logger=MET_LOGGER):
            result = _run_daily(
                sink, lambda sid, session, **kw: _report(sid, session, recovered=1)
            )
        assert result is None, "the job's normal return value"
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "NOT recorded" in r.getMessage()
        ]
        assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
        assert (
            "recovered=2" in warnings[0].getMessage()
        ), "the warning must carry the row the table missed"  # 1 station x 2 sessions
        assert sink.recorded() == []

    def test_db_down_at_connect(self, caplog):
        self._assert_run_survives(_Sink(fail_connect=OSError("db down")), caplog)

    def test_table_missing_at_execute(self, caplog):
        self._assert_run_survives(
            _Sink(fail_execute=RuntimeError('relation "ltb_runs" does not exist')),
            caplog,
        )

    def test_a_broken_budget_cannot_build_a_row_and_still_does_not_raise(self, caplog):
        with caplog.at_level(logging.WARNING, logger=MET_LOGGER):
            out = met.record_run(
                "daily", object(), datetime.now(UTC), [], 0, SESSIONS, "x"
            )
        assert out is None
        assert any("could not be built" in r.getMessage() for r in caplog.records)

    def test_record_ltb_run_returns_false_never_raises(self):
        m = met.build_run_metrics(
            "daily",
            RunBudget(max_seconds=None, max_slots=None),
            datetime.now(UTC),
            [],
            0,
            SESSIONS,
            "x",
        )
        with patch(DBF, side_effect=ConnectionError("nope")):
            assert met.record_ltb_run(m) is False


# --------------------------------------------------------------------------
# 5. the reconnection trigger records its own kind and its own ceiling
# --------------------------------------------------------------------------


class TestReconnectionRun:
    def _run(self, sink, station=None, **kw):
        old_gap = SimpleNamespace(file_date=date(2000, 1, 1))  # older than floor
        station = station or (
            lambda sid, session, **k: _report(sid, session, recovered=3)
        )
        with (
            patch(DBF, side_effect=sink.connection),
            patch(f"{LTB}.query_long_term_gaps", return_value=_report(queued=1)),
            patch(f"{LTB}.run_long_term_backfill_station", side_effect=station),
        ):
            # the classifier report needs a real old gap to pass the floor
            with patch(
                f"{LTB}.query_long_term_gaps",
                return_value=SimpleNamespace(queued=[old_gap]),
            ):
                return _run_reconnection_backfill_job(**kw)

    def test_records_reconnect_kind_with_its_own_ceiling(self):
        sink = _Sink(connectivity=[("AAAA",)])
        self._run(sink, lookback_days=30, max_run_seconds=600, max_slots_per_run=600)
        (row,) = sink.recorded()
        assert row["run_kind"] == "reconnect"
        assert row["max_run_seconds"] == 600, "NOT the daily 1800/3600"
        assert row["stations"] == 1 and row["sessions"] == ["15s_24hr"]
        assert row["lookback"] == "15s_24hr=30d"
        assert row["recovered"] == 3 and row["queued_station_sessions"] == 1
        assert row["stop_reason"] is None

    def test_the_default_ceiling_is_the_reconnect_one(self):
        from receivers.scheduling.long_term_backfill import (
            DEFAULT_MAX_RUN_SECONDS,
            DEFAULT_RECONNECT_MAX_RUN_SECONDS,
        )

        sink = _Sink(connectivity=[("AAAA",)])
        self._run(sink, lookback_days=30)
        (row,) = sink.recorded()
        assert row["max_run_seconds"] == DEFAULT_RECONNECT_MAX_RUN_SECONDS
        assert row["max_run_seconds"] != DEFAULT_MAX_RUN_SECONDS

    def test_an_idle_tick_records_nothing(self):
        """No candidates -> no budget -> not a run. 96 empty rows a day would
        bury the ones that matter."""
        sink = _Sink(connectivity=[])
        self._run(sink, lookback_days=30)
        assert sink.recorded() == []

    def test_a_db_failure_does_not_propagate_here_either(self, caplog):
        sink = _Sink(connectivity=[("AAAA",)], fail_execute=RuntimeError("boom"))
        with caplog.at_level(logging.WARNING, logger=MET_LOGGER):
            assert self._run(sink, lookback_days=30) is None
        assert any("NOT recorded" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# 6. recovered / failed survive on the report
# --------------------------------------------------------------------------


class TestReportCarriesOutcomes:
    def test_recovered_and_failed_are_fields_not_locals(self, caplog):
        calls = {"n": 0}

        def _day(
            sid, d, end, session, immediate_archive=False, run_rinex=False, outcome=None
        ):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("pipeline failed")
            outcome["status"] = "completed"
            outcome["files_downloaded"] = 1 if calls["n"] == 1 else 0
            return True

        with (
            patch(f"{LTB}.query_long_term_gaps", return_value=_report(queued=3)),
            patch(f"{LTB}._station_is_offline", return_value=False),
            patch(
                "receivers.scheduling.backfill._backfill_station_day_generic",
                side_effect=_day,
            ),
            caplog.at_level(
                logging.INFO, logger="receivers.scheduler.long_term_backfill"
            ),
        ):
            report = run_long_term_backfill_station("AAAA", "15s_24hr")
        assert report.recovered == 1
        assert report.failed == 1
        assert report.nothing_on_receiver == 1
        # the operator-facing line is unchanged, byte for byte
        done = [m for m in caplog.messages if "AAAA/15s_24hr done:" in m]
        assert done == [
            "Long-term backfill AAAA/15s_24hr done: recovered=1 "
            "nothing_on_receiver=1 failed=1"
        ]

    def test_fields_default_to_zero_on_a_fresh_report(self):
        r = LongTermGapReport(
            sid="AAAA", session="15s_24hr", start=date(2026, 1, 1), end=date(2026, 1, 2)
        )
        assert (r.recovered, r.failed) == (0, 0)
