"""Load-aware parallel execution of independent batch chunks.

Generic harness for CLI batch verbs (re-rinex today; fix-headers,
archive-verify, … can adopt it): split the work into independent chunks
(usually calendar years), run them on a thread pool sized to what the host
can take *right now*, and keep re-checking the load so workers hold off
picking up their next chunk while the box is busy.

Threads, not processes: receivers batch work is dominated by subprocess
calls (teqc/runpkr00/RNX2CRX/compress) and network I/O (TOS, NFS), all of
which release the GIL — N workers give ~N× throughput without the
complexity of multiprocessing.

Capacity policy (two layers):
  * ``auto_workers()`` — one-shot sizing at start: unused cores
    (``cpu × TARGET_LOAD_FRAC − loadavg1``), clamped to [1, cap, n_chunks].
  * load gate — on-the-fly assessment: before starting each next chunk a
    worker re-reads loadavg1 and waits (poll every 15 s, bounded) while it
    exceeds ``cpu × GATE_LOAD_FRAC``. Already-running chunks are never
    interrupted; the pool just stops taking on more until pressure drops.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Optional, Sequence, Tuple, Type

DEFAULT_CAP = 8  # hard ceiling — beyond this, NFS/TOS become the bottleneck
TARGET_LOAD_FRAC = 0.75  # initial sizing: leave a quarter of the cores free
GATE_LOAD_FRAC = 1.2  # hold new chunks while loadavg1 > cpu * this
GATE_POLL_S = 15
GATE_MAX_WAIT_S = 600  # never wedge a batch on a permanently-busy host


def split_year_ranges(
    start: datetime, end: datetime
) -> List[Tuple[datetime, datetime]]:
    """``[start, end)`` split on calendar-year boundaries (clipped).

    Year chunks match the archive layout (YYYY/mon/...) and — critically —
    are date-disjoint, so parallel workers can never race on the same
    output file.
    """
    out: List[Tuple[datetime, datetime]] = []
    cur = start
    while cur < end:
        nxt = min(datetime(cur.year + 1, 1, 1), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def auto_workers(
    n_chunks: int,
    *,
    cap: int = DEFAULT_CAP,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Worker count from current host capacity: free cores now, clamped."""
    cpu = os.cpu_count() or 2
    try:
        load1 = os.getloadavg()[0]
    except OSError:  # pragma: no cover - non-POSIX
        load1 = 0.0
    headroom = max(1.0, cpu * TARGET_LOAD_FRAC - load1)
    workers = max(1, min(int(headroom), cap, n_chunks))
    if logger:
        logger.info(
            "auto parallel: %d worker(s) (%d cores, loadavg %.1f, %d chunks)",
            workers,
            cpu,
            load1,
            n_chunks,
        )
    return workers


def resolve_workers(
    par: Any,
    n_chunks: int,
    logger: logging.Logger,
) -> int:
    """Resolve a ``--parallel`` argument value into a worker count.

    ``None``/falsy → 1 (sequential); ``"auto"`` → :func:`auto_workers`;
    an integer string → that many, clamped to ``n_chunks``; anything
    unparsable → 1 with an error log. Shared by every batch verb that
    exposes ``--parallel``.
    """
    if not par or n_chunks <= 1:
        return 1
    if str(par).lower() == "auto":
        return auto_workers(n_chunks, logger=logger)
    try:
        return max(1, min(int(par), n_chunks))
    except (TypeError, ValueError):
        logger.error(
            "--parallel must be a number or 'auto' — got %r; running sequentially",
            par,
        )
        return 1


def _load_gate(logger: logging.Logger) -> None:
    """Block (bounded) while the host is over the gate threshold."""
    cpu = os.cpu_count() or 2
    threshold = cpu * GATE_LOAD_FRAC
    waited = 0
    while waited < GATE_MAX_WAIT_S:
        try:
            load1 = os.getloadavg()[0]
        except OSError:  # pragma: no cover - non-POSIX
            return
        if load1 <= threshold:
            return
        if waited == 0:
            logger.info(
                "load gate: loadavg %.1f > %.1f — holding next chunk", load1, threshold
            )
        time.sleep(GATE_POLL_S)
        waited += GATE_POLL_S


@dataclass
class ChunkOutcome:
    """Result of one chunk: exactly one of value/error is meaningful."""

    chunk: Any
    value: Any = None
    error: Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class BatchCancelledError(RuntimeError):
    """Chunk skipped because an abort_on exception cancelled the batch."""


def run_chunks(
    chunks: Sequence[Any],
    fn: Callable[[Any], Any],
    *,
    workers: int,
    logger: logging.Logger,
    abort_on: Tuple[Type[BaseException], ...] = (),
    load_gate: bool = True,
) -> List[ChunkOutcome]:
    """Run ``fn(chunk)`` for every chunk on ``workers`` threads.

    * Outcomes are returned in input order.
    * An exception of a type in ``abort_on`` (e.g. NetworkUnavailableError)
      cancels every not-yet-started chunk and is RE-RAISED after all running
      chunks finish — batch-level abort semantics (resume by re-running).
    * Any other exception is captured on that chunk's outcome (``.error``)
      and the batch continues.
    """
    outcomes = [ChunkOutcome(c) for c in chunks]
    abort = threading.Event()
    first_abort: List[BaseException] = []

    def _run(i: int) -> None:
        oc = outcomes[i]
        if abort.is_set():
            oc.error = BatchCancelledError("batch aborted before this chunk started")
            return
        if load_gate:
            _load_gate(logger)
        try:
            oc.value = fn(oc.chunk)
        except abort_on as e:
            oc.error = e
            abort.set()
            first_abort.append(e)  # GIL-atomic append; first one wins on raise
        except Exception as e:  # noqa: BLE001 — per-chunk isolation is the point
            oc.error = e
            logger.error("batch chunk %s failed: %s", oc.chunk, e)

    with ThreadPoolExecutor(
        max_workers=max(1, workers), thread_name_prefix="batch"
    ) as pool:
        futures = [pool.submit(_run, i) for i in range(len(outcomes))]
        for fut in futures:
            fut.result()  # _run never raises; this just joins

    if first_abort:
        raise first_abort[0]
    return outcomes
