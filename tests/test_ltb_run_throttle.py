"""#174/S3 — the throttle that bounds an LTB run.

`scheduler.yaml`'s `long_term_backfill:` block disables this feature and names
two conditions for re-enabling it: the idempotence bug fixed, AND "either
load_monitoring is back or another throttle exists". S1 closed the first (the
classifier reads `archive_catalog`, so a recovered day returns `already_ok`).
This is the second.

It cannot route through `_load_monitor_overloaded()` — that returns False
whenever `_load_monitor is None`, and `load_monitoring` stays disabled because
enabling it makes the reconciler gate on process-wide thread count and starve
live downloads. So those yield points are dead code and the budget stands alone.

What these tests pin, in order of how badly each would hurt:

* **An offline station costs one DB read, not one ping per slot.** The measured
  case: SKDA/SVIN/THNA each queue 720 hours having been unreachable for 111
  days; at 5-9 s per ping that is ~1 h of a worker each, per run, against
  `max_workers=2`.
* **A run is bounded in wall-clock**, so it cannot still be running when its
  successor is due. Slot count alone cannot do this — slot cost varies from
  microseconds (`already_ok`) to minutes (a real download).
* **A station that goes unreachable MID-run abandons its remainder.** This is
  the case the connectivity gate cannot see, and it is the one that burns the
  hour.
* **Unreachable is no longer counted as recovered.** The shared primitive folds
  `status='unreachable'` into `files_error` and does not raise, so before this
  the loop logged a dead station's whole queue as `recovered`.
* **Reservation is returned, not spent**, or one skipped station starves every
  station behind it.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from receivers.scheduling.long_term_backfill import (
    LongTermGapReport,
    _station_is_offline,
    RunBudget,
    _mark_attempted,
    _recently_attempted,
    _advance_rotation,
    _reset_attempt_history,
    _reset_rotation,
    _rotate,
    run_long_term_backfill_station,
)


class TestRunBudget:
    def test_slots_are_granted_until_the_cap(self):
        b = RunBudget(max_seconds=None, max_slots=10)
        assert b.take(4) == 4
        assert b.take(4) == 4
        assert b.take(4) == 2, "only the remainder is granted"
        assert b.take(1) == 0

    def test_exhaustion_records_which_ceiling(self):
        b = RunBudget(max_seconds=None, max_slots=2)
        b.take(2)
        assert b.exhausted()
        assert "slots" in b.exhausted_reason

    def test_wall_clock_bounds_a_run_slots_cannot(self):
        """Slot cost spans microseconds to minutes, so the clock is primary."""
        b = RunBudget(max_seconds=0.0, max_slots=10_000)
        assert b.take(1) == 0
        assert b.exhausted() and "wall-clock" in b.exhausted_reason

    def test_unused_reservation_is_returned(self):
        b = RunBudget(max_seconds=None, max_slots=10)
        assert b.take(10) == 10
        b.give_back(7)
        assert b.take(5) == 5, "returned slots must be re-grantable"

    def test_give_back_cannot_go_negative(self):
        b = RunBudget(max_seconds=None, max_slots=10)
        b.take(2)
        b.give_back(99)
        assert b.slots_used == 0

    def test_unbounded_budget_grants_everything(self):
        b = RunBudget(max_seconds=None, max_slots=None)
        assert b.take(10_000) == 10_000
        assert not b.exhausted()

    def test_concurrent_takes_never_oversubscribe(self):
        """The daily job shares one budget across worker threads."""
        import threading

        b = RunBudget(max_seconds=None, max_slots=100)
        granted = []
        lock = threading.Lock()

        def worker():
            g = b.take(3)
            with lock:
                granted.append(g)

        threads = [threading.Thread(target=worker) for _ in range(60)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(granted) == 100, "total granted must equal the cap exactly"


def _report(sid="SKDA", n_queued=720):
    r = LongTermGapReport(
        sid=sid, session="1Hz_1hr", start=date(2026, 8, 1), end=date(2026, 8, 31)
    )
    r.queued = [SimpleNamespace(file_date=date(2026, 8, 1)) for _ in range(n_queued)]
    return r


class _Runner:
    """Drives run_long_term_backfill_station with everything external stubbed."""

    def __init__(self, offline=False, statuses=None, n_queued=720):
        self.offline = offline
        self.statuses = statuses or {}
        self.n_queued = n_queued
        self.calls = 0

    def _day(self, sid, d, end, session, immediate_archive=False,
             run_rinex=False, outcome=None):
        self.calls += 1
        status = self.statuses.get(self.calls, "completed")
        if outcome is not None:
            outcome["status"] = status
            outcome["files_downloaded"] = 1 if status == "completed" else 0
        return True

    def run(self, **kw):
        with (
            patch(
                "receivers.scheduling.long_term_backfill.query_long_term_gaps",
                return_value=_report(n_queued=self.n_queued),
            ),
            patch(
                "receivers.scheduling.long_term_backfill._station_is_offline",
                return_value=self.offline,
            ),
            patch(
                "receivers.scheduling.backfill._backfill_station_day_generic",
                side_effect=self._day,
            ),
        ):
            return run_long_term_backfill_station("SKDA", "1Hz_1hr", **kw)


class TestOfflineShortCircuit:
    """SKDA/SVIN/THNA: 720 queued hours, ping-unreachable for 111 days."""

    def test_an_offline_station_does_no_slot_work(self):
        r = _Runner(offline=True)
        report = r.run()
        assert r.calls == 0, "not one ping may be spent on an offline station"
        assert report.skipped_offline

    def test_it_shows_in_the_summary(self):
        assert "SKIPPED: station offline" in _Runner(offline=True).run().summary()

    def test_an_online_station_is_unaffected(self):
        r = _Runner(offline=False, n_queued=3)
        r.run()
        assert r.calls == 3

    def test_unknown_connectivity_does_not_block_recovery(self):
        """A missing/stale row returns None — a monitoring outage must not
        silently halt recovery fleet-wide."""
        r = _Runner(offline=None, n_queued=3)
        r.run()
        assert r.calls == 3

    def test_the_budget_is_not_spent_by_a_skipped_station(self):
        b = RunBudget(max_seconds=None, max_slots=720)
        _Runner(offline=True).run(budget=b)
        assert b.slots_used == 0, "a skipped station must not starve the queue"


class TestMidRunUnreachable:
    """The case the connectivity gate cannot see: the row says online, the
    receiver is not. This is what burns the hour."""

    def test_the_remainder_is_abandoned(self):
        r = _Runner(statuses={3: "unreachable"}, n_queued=720)
        report = r.run()
        assert r.calls == 3, "must stop AT the unreachable slot, not continue"
        assert report.unreachable_slots == 1

    def test_the_abandoned_reservation_is_returned(self):
        b = RunBudget(max_seconds=None, max_slots=720)
        _Runner(statuses={3: "unreachable"}, n_queued=720).run(budget=b)
        assert b.slots_used == 3

    def test_unreachable_is_not_counted_as_recovered(self, caplog):
        """The primitive folds 'unreachable' into files_error and does NOT
        raise, so this used to log a dead station's whole queue as recovered."""
        import logging

        with caplog.at_level(logging.INFO, logger="receivers.scheduler.long_term_backfill"):
            _Runner(statuses={1: "unreachable"}, n_queued=720).run()
        done = [m for m in caplog.messages if "done: recovered=" in m]
        assert done and "recovered=0" in done[0]

    def test_a_plain_failure_does_not_short_circuit(self):
        """Only 'unreachable' means the station is gone. A per-day failure is
        a per-day problem and the rest of the queue is still worth trying."""
        r = _Runner(statuses={2: "failed"}, n_queued=5)
        r.run()
        assert r.calls == 5


class TestBudgetCapsAStation:
    def test_the_queue_is_trimmed_to_the_grant(self):
        b = RunBudget(max_seconds=None, max_slots=10)
        r = _Runner(n_queued=720)
        report = r.run(budget=b)
        assert r.calls == 10
        assert report.budget_capped == 710

    def test_an_exhausted_budget_does_no_work(self):
        b = RunBudget(max_seconds=None, max_slots=1)
        b.take(1)
        r = _Runner(n_queued=720)
        r.run(budget=b)
        assert r.calls == 0

    def test_a_dry_run_spends_nothing(self):
        b = RunBudget(max_seconds=None, max_slots=720)
        r = _Runner(n_queued=720)
        r.run(budget=b, dry_run=True)
        assert r.calls == 0
        assert b.slots_used == 0, "a dry run must leave the budget untouched"


class TestReattemptCooldown:
    """Insurance against a FLAPPING station re-minting `state_since` inside the
    archive-sync lag. Measured 2026-09-14: only 7 reconnections fleet-wide in
    24 h, so this is not a hot path."""

    def setup_method(self):
        _reset_attempt_history()

    def test_a_fresh_station_is_not_suppressed(self):
        assert not _recently_attempted("VMEY", 90)

    def test_an_attempted_station_is_suppressed(self):
        _mark_attempted("VMEY")
        assert _recently_attempted("VMEY", 90)

    def test_only_that_station_is_suppressed(self):
        _mark_attempted("VMEY")
        assert not _recently_attempted("THEY", 90)

    def test_a_zero_cooldown_disables_it(self):
        _mark_attempted("VMEY")
        assert not _recently_attempted("VMEY", 0)


class _FakeCursor:
    def __init__(self, row, raises=False):
        self._row, self._raises = row, raises

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        if self._raises:
            raise RuntimeError("connection pool exhausted")

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row, raises=False):
        self._row, self._raises = row, raises

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self._row, self._raises)


def _offline(row, raises=False):
    with patch(
        "receivers.health.database_factory.DatabaseConnectionFactory.connection",
        return_value=_FakeConn(row, raises),
    ):
        return _station_is_offline("SKDA")


class TestConnectivityGateReadsTheRow:
    """The gate decides whether a station's whole queue runs, so every way the
    row can fail to be evidence must return None, not False."""

    def test_a_fresh_offline_row_gates(self):
        assert _offline((False, False)) is True

    def test_a_fresh_online_row_does_not(self):
        assert _offline((True, False)) is False

    def test_a_STALE_row_is_not_evidence(self):
        """The health job refreshes every 5 min. A row older than the trust
        window means the monitor is not running — its verdict is not evidence,
        and trusting a stale 'offline' would halt that station indefinitely."""
        assert _offline((False, True)) is None, "stale offline must not gate"
        assert _offline((True, True)) is None, "stale online is equally unusable"

    def test_a_missing_station_is_not_evidence(self):
        assert _offline(None) is None

    def test_a_null_is_online_is_not_evidence(self):
        assert _offline((None, False)) is None

    def test_a_db_failure_never_blocks_recovery(self):
        assert _offline((False, False), raises=True) is None


class TestRotationPreventsStarvation:
    """MEASURED, not hypothetical: at the deployed `lookback_days: 90` across
    both sessions, 1,631 slots reach the receiver against a 600-slot budget, so
    1,031 are deferred EVERY run. The daily job's task list is stable
    alphabetical order, so serving it from the front each time would mean a
    station past the cut is never recovered at all."""

    def setup_method(self):
        _reset_rotation()

    def test_the_first_run_starts_at_the_front(self):
        assert _rotate([1, 2, 3, 4]) == [1, 2, 3, 4]

    def test_the_next_run_starts_where_the_last_left_off(self):
        tasks = [1, 2, 3, 4, 5]
        _rotate(tasks)
        _advance_rotation(served=2, total=5)
        assert _rotate(tasks) == [3, 4, 5, 1, 2]

    def test_every_task_is_reached_within_a_few_runs(self):
        """The property that matters: nothing is stranded."""
        tasks = list(range(10))
        seen = set()
        for _ in range(5):
            seen.update(_rotate(tasks)[:3])  # budget serves 3 per run
            _advance_rotation(served=3, total=len(tasks))
        assert seen == set(tasks), f"stranded: {set(tasks) - seen}"

    def test_rotating_does_not_advance_on_its_own(self):
        """Advancing belongs to _advance_rotation, called with the number of
        tasks that actually consumed budget — most classify clean and cost
        nothing, so rotating by the list length would skip them."""
        tasks = [1, 2, 3]
        assert _rotate(tasks) == _rotate(tasks) == [1, 2, 3]

    def test_a_run_that_served_nothing_does_not_move_the_cursor(self):
        tasks = [1, 2, 3]
        _advance_rotation(served=0, total=3)
        assert _rotate(tasks) == [1, 2, 3]

    def test_the_cursor_wraps(self):
        tasks = [1, 2, 3]
        _advance_rotation(served=7, total=3)
        assert _rotate(tasks) == [2, 3, 1]

    def test_an_empty_task_list_is_safe(self):
        assert _rotate([]) == []
        _advance_rotation(served=1, total=0)
