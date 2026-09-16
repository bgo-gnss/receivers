"""Stop re-attempting a slot that has already failed N times.

MEASURED on rek-d01 2026-09-16, across all sessions:

    attempts   now present   still missing   cumulative recoveries
      1            666,326          72,375        87.4 %
      2             74,204          33,623        97.1 %
      3-5           12,491          49,085        98.8 %
      6-9              738          48,961        98.8 %

Attempts 6-9 recover 738 slots while re-attempting ~49,000 — a ~1.5 % hit rate.
The 04:00 run that prompted this spent 249 of 258 slots on files that were not
there (`recovered=9 nothing_on_receiver=249`).

**Why a cap and NOT `use_terminal_absence`.** That flag is a PERMANENT lockout
keyed on a signal already proven to give false positives: 4 of 5 probed terminal
rows still had the file, because of the unfinalised-filename bug. Since that fix
went live, 4,731 previously-failed slots became present — SEY9 alone recovered
699. A bounded attempt count keeps that: it is a config knob, raising it
re-opens every slot it cut off, and the receiver horizon still ages slots out
independently.
"""

from __future__ import annotations

from datetime import date

import pytest

from receivers.scheduling.long_term_backfill import (
    DEFAULT_MAX_DOWNLOAD_ATTEMPTS,
    _attempts_exhausted_slots,
)


class _Cur:
    def __init__(self, rows, raises=False):
        self.rows, self.raises, self.params = rows, raises, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.raises:
            raise RuntimeError("db down")
        self.sql, self.params = sql, params

    def fetchall(self):
        return self.rows


class _Tracker:
    def __init__(self, rows, connect_ok=True, raises=False):
        self._rows, self._ok, self._raises = rows, connect_ok, raises
        self.cur = _Cur(rows, raises)

    def connect(self):
        return self._ok

    def read_cursor(self):
        return self.cur


def _slots(n=3):
    from receivers.scheduling.long_term_backfill import _Slot

    return [
        _Slot(
            file_date=date(2026, 8, 1 + i),
            file_hour=None,
            raw_key="k%d" % i,
            rinex_keys=(),
            raw_path="/p%d" % i,
        )
        for i in range(n)
    ]


def _run(monkeypatch, tracker, slots=None, max_attempts=5):
    import receivers.health.file_tracker as ft

    monkeypatch.setattr(ft, "FileTracker", lambda: tracker)
    return _attempts_exhausted_slots("BALD", "15s_24hr", slots or _slots(), max_attempts)


class TestItIdentifiesExhaustedSlots:
    def test_returned_rows_become_the_skip_set(self, monkeypatch):
        t = _Tracker([(date(2026, 8, 1), None), (date(2026, 8, 2), None)])
        got = _run(monkeypatch, t)
        assert got == {(date(2026, 8, 1), None), (date(2026, 8, 2), None)}

    def test_the_threshold_is_passed_through(self, monkeypatch):
        t = _Tracker([])
        _run(monkeypatch, t, max_attempts=7)
        assert 7 in t.cur.params, t.cur.params

    def test_it_only_counts_MISSING_rows(self, monkeypatch):
        """download_count increments on a 'downloaded' write too, so a healthy
        file re-verified often would otherwise look exhausted."""
        t = _Tracker([])
        _run(monkeypatch, t)
        assert "status = 'missing'" in " ".join(t.cur.sql.split())

    def test_it_uses_IS_NOT_DISTINCT_FROM_for_the_hour(self, monkeypatch):
        """A plain = never matches NULL and would skip every DAILY slot."""
        t = _Tracker([])
        _run(monkeypatch, t)
        assert "IS NOT DISTINCT FROM" in " ".join(t.cur.sql.split())


class TestItFailsOpen:
    """Any failure must make the worklist LARGER, never silently drop a
    recoverable day — same contract as _known_missing_slots."""

    def test_a_db_error_skips_nothing(self, monkeypatch):
        assert _run(monkeypatch, _Tracker([], raises=True)) == set()

    def test_a_failed_connect_skips_nothing(self, monkeypatch):
        assert _run(monkeypatch, _Tracker([], connect_ok=False)) == set()

    def test_no_slots_skips_nothing(self, monkeypatch):
        assert _run(monkeypatch, _Tracker([]), slots=[]) == set()

    def test_a_zero_or_negative_cap_disables_it(self, monkeypatch):
        """0 means OFF, matching the other knobs' documented semantics."""
        t = _Tracker([(date(2026, 8, 1), None)])
        assert _run(monkeypatch, t, max_attempts=0) == set()
        assert _run(monkeypatch, t, max_attempts=-1) == set()


class TestTheDefaultMatchesTheEvidence:
    def test_default_is_five(self):
        """98.8 % of recoveries land by attempt 5; 6-9 yield ~1.5 %."""
        assert DEFAULT_MAX_DOWNLOAD_ATTEMPTS == 5

    def test_default_is_above_the_bulk_of_recoveries(self):
        """97.1 % land by attempt 2 — a cap of 1 or 2 would cost real data."""
        assert DEFAULT_MAX_DOWNLOAD_ATTEMPTS > 2


class TestTheClassifierActuallyConsultsIt:
    """The helper being correct is worthless if `query_long_term_gaps` never
    looks at it. A mutation that stubbed the classifier's check to `if False`
    went UNDETECTED until these were added."""

    @pytest.fixture
    def neutral(self, monkeypatch):
        import receivers.scheduling.long_term_backfill as ltb

        monkeypatch.setattr(ltb, "_last_archived_date", lambda *a, **k: None)
        monkeypatch.setattr(ltb, "_receiver_horizon", lambda *a, **k: None)
        monkeypatch.setattr(ltb, "_absence_counts", lambda *a, **k: (0, 0))
        monkeypatch.setattr(ltb, "_known_missing_slots", lambda *a, **k: set())
        monkeypatch.setattr(ltb, "_catalog_present_keys", lambda *a, **k: set())
        return ltb

    def _slots(self, ltb, n=3):
        return [
            ltb._Slot(
                file_date=date(2026, 1, 1 + i),
                file_hour=None,
                raw_key="r%d" % i,
                rinex_keys=("k%d" % i,),
                raw_path="/a/r%d" % i,
            )
            for i in range(n)
        ]

    def test_an_exhausted_slot_is_not_queued(self, neutral, monkeypatch):
        ltb = neutral
        slots = self._slots(ltb)
        monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
        monkeypatch.setattr(
            ltb, "_attempts_exhausted_slots",
            lambda *a, **k: {(date(2026, 1, 1), None), (date(2026, 1, 2), None)},
        )
        rep = ltb.query_long_term_gaps("BALD", "15s_24hr", lookback_days=3)
        queued = {(g.file_date, g.file_hour) for g in rep.queued}
        assert (date(2026, 1, 1), None) not in queued
        assert (date(2026, 1, 3), None) in queued, "a fresh slot must still queue"
        assert rep.attempts_exhausted == 2

    def test_with_an_empty_set_nothing_is_skipped(self, neutral, monkeypatch):
        ltb = neutral
        slots = self._slots(ltb)
        monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
        monkeypatch.setattr(ltb, "_attempts_exhausted_slots", lambda *a, **k: set())
        rep = ltb.query_long_term_gaps("BALD", "15s_24hr", lookback_days=3)
        assert len(rep.queued) == 3
        assert rep.attempts_exhausted == 0

    def test_the_summary_surfaces_the_count(self, neutral, monkeypatch):
        ltb = neutral
        slots = self._slots(ltb)
        monkeypatch.setattr(ltb, "_expected_slots", lambda *a, **k: slots)
        monkeypatch.setattr(
            ltb, "_attempts_exhausted_slots", lambda *a, **k: {(date(2026, 1, 1), None)}
        )
        rep = ltb.query_long_term_gaps("BALD", "15s_24hr", lookback_days=3)
        assert "attempts_exhausted=1" in rep.summary()
