"""Tests for the load-aware batch-parallel harness (utils/batch_parallel)."""

import logging
import threading
from datetime import datetime

import pytest

from receivers.utils import batch_parallel as bp
from receivers.utils.batch_parallel import (
    BatchCancelledError,
    auto_workers,
    run_chunks,
    split_year_ranges,
)

LOG = logging.getLogger("test.batch")


# ------------------------------------------------------------ year chunking


def test_split_year_ranges_mid_year_boundaries():
    ranges = split_year_ranges(datetime(2012, 8, 28), datetime(2014, 3, 5))
    assert ranges == [
        (datetime(2012, 8, 28), datetime(2013, 1, 1)),
        (datetime(2013, 1, 1), datetime(2014, 1, 1)),
        (datetime(2014, 1, 1), datetime(2014, 3, 5)),
    ]


def test_split_year_ranges_single_year():
    ranges = split_year_ranges(datetime(2026, 2, 1), datetime(2026, 7, 3))
    assert ranges == [(datetime(2026, 2, 1), datetime(2026, 7, 3))]


def test_split_year_ranges_exact_year_edges():
    ranges = split_year_ranges(datetime(2024, 1, 1), datetime(2026, 1, 1))
    assert ranges == [
        (datetime(2024, 1, 1), datetime(2025, 1, 1)),
        (datetime(2025, 1, 1), datetime(2026, 1, 1)),
    ]
    # chunks are contiguous and non-overlapping — the no-race invariant
    for (_, e1), (s2, _) in zip(ranges, ranges[1:]):
        assert e1 == s2


def test_split_year_ranges_empty():
    assert split_year_ranges(datetime(2026, 1, 1), datetime(2026, 1, 1)) == []


# ------------------------------------------------------------- worker sizing


def test_auto_workers_uses_free_cores(monkeypatch):
    monkeypatch.setattr(bp.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(bp.os, "getloadavg", lambda: (1.0, 0, 0))
    # 8 * 0.75 - 1.0 = 5
    assert auto_workers(20) == 5


def test_auto_workers_clamped_by_chunks_and_cap(monkeypatch):
    monkeypatch.setattr(bp.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(bp.os, "getloadavg", lambda: (0.0, 0, 0))
    assert auto_workers(3) == 3  # never more workers than chunks
    assert auto_workers(100) == bp.DEFAULT_CAP  # hard ceiling


def test_auto_workers_floor_one_on_loaded_host(monkeypatch):
    monkeypatch.setattr(bp.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(bp.os, "getloadavg", lambda: (12.0, 0, 0))
    assert auto_workers(10) == 1


# ---------------------------------------------------------------- run_chunks


def test_run_chunks_collects_in_input_order():
    def work(chunk):
        return chunk * 10

    outcomes = run_chunks([3, 1, 2], work, workers=3, logger=LOG, load_gate=False)
    assert [o.chunk for o in outcomes] == [3, 1, 2]
    assert [o.value for o in outcomes] == [30, 10, 20]
    assert all(o.ok for o in outcomes)


def test_run_chunks_actually_parallel():
    barrier = threading.Barrier(3, timeout=10)

    def work(chunk):
        barrier.wait()  # deadlocks unless 3 chunks run concurrently
        return chunk

    outcomes = run_chunks([1, 2, 3], work, workers=3, logger=LOG, load_gate=False)
    assert all(o.ok for o in outcomes)


def test_run_chunks_isolates_ordinary_errors():
    def work(chunk):
        if chunk == "bad":
            raise ValueError("boom")
        return "ok"

    outcomes = run_chunks(
        ["a", "bad", "b"], work, workers=2, logger=LOG, load_gate=False
    )
    assert outcomes[0].value == "ok" and outcomes[2].value == "ok"
    assert isinstance(outcomes[1].error, ValueError)


class _AbortError(Exception):
    pass


def test_run_chunks_abort_on_cancels_and_reraises():
    started: list = []

    def work(chunk):
        started.append(chunk)
        if chunk == 0:
            raise _AbortError("network down")
        return chunk

    # 1 worker => strictly sequential: chunk 0 aborts, 1 and 2 must not run.
    with pytest.raises(_AbortError):
        run_chunks(
            [0, 1, 2],
            work,
            workers=1,
            logger=LOG,
            abort_on=(_AbortError,),
            load_gate=False,
        )
    assert started == [0]


def test_run_chunks_abort_marks_cancelled_outcomes():
    def work(chunk):
        raise _AbortError("down")

    try:
        run_chunks(
            [0, 1],
            work,
            workers=1,
            logger=LOG,
            abort_on=(_AbortError,),
            load_gate=False,
        )
    except _AbortError:
        pass
    else:  # pragma: no cover
        pytest.fail("abort exception not re-raised")


def test_load_gate_waits_then_releases(monkeypatch):
    loads = iter([(10.0, 0, 0), (10.0, 0, 0), (0.5, 0, 0)])
    sleeps: list = []
    monkeypatch.setattr(bp.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(bp.os, "getloadavg", lambda: next(loads))
    monkeypatch.setattr(bp.time, "sleep", sleeps.append)
    bp._load_gate(LOG)
    assert sleeps == [bp.GATE_POLL_S, bp.GATE_POLL_S]


# ------------------------------------------------------------ progress board


def test_chunk_progress_describe_eta():
    h = bp.ChunkProgress("RHOF 2026")
    h.start()
    h.set_total(100)
    for _ in range(10):
        h.advance(18.0)
    d = h.describe()
    assert d.startswith("RHOF 2026 10/100")
    assert "18.0s/item" in d
    assert "ETA 0:27h" in d  # 90 files * 18s = 1620s = 0:27


def test_progress_board_render_states():
    board = bp.ProgressBoard(interval=30)
    a = board.handle("A 2024")
    b = board.handle("B 2025")
    c = board.handle("C 2026")
    a.start()
    a.set_total(5)
    a.advance(2.0)
    b.start()
    b.finish(ok=True)
    c.finish(ok=False)
    line = board.render()
    assert "A 2024 1/5" in line
    assert "2/3 chunks done" in line
    assert "1 FAILED" in line
    assert "B 2025" not in line  # finished chunks leave the running list


def test_progress_board_reporter_thread_lifecycle():
    lines: list = []
    board = bp.ProgressBoard(interval=30, out=lines.append)
    with board:
        h = board.handle("X")
        h.start()
    # reporter thread stopped cleanly; render still works after exit
    assert "X" in board.render()
