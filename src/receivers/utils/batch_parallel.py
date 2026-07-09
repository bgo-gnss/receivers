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
    if not par:
        return 1
    if n_chunks <= 1:
        logger.info("--parallel: only one chunk — running sequentially")
        return 1
    if str(par).lower() == "auto":
        w = auto_workers(n_chunks, logger=logger)
        if w == 1:
            logger.info(
                "--parallel: host too loaded for extra workers right now — "
                "running sequentially"
            )
        return w
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


# Nerd-font "timer" glyph for the time-left indicator.
TIME_LEFT_ICON = "󰥽"


def fmt_duration(seconds: float) -> str:
    """Human-friendly duration — the general batch-progress time convention:
    ``1.3 h`` at/above one hour, else ``42 min`` (never below ``1 min``).

    >>> fmt_duration(4680), fmt_duration(660), fmt_duration(20)
    ('1.3 h', '11 min', '1 min')
    """
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s / 3600:.1f} h"
    return f"{max(1, round(s / 60))} min"


def fmt_time_left(seconds: float) -> str:
    """Standard time-left indicator: ``󰥽 1.3 h left`` / ``󰥽 42 min left``."""
    return f"{TIME_LEFT_ICON} {fmt_duration(seconds)} left"


def _fmt_eta(seconds: float) -> str:
    """Back-compat alias — compact duration for ETA displays."""
    return fmt_duration(seconds)


class ChunkProgress:
    """Live progress handle for one chunk (updates are GIL-atomic).

    Duck-typed and deliberately tiny so worker code (converters,
    header-fix loops, hashers) can call it without importing this module:
    ``start() / set_total(n) / advance(seconds=None) / finish(ok=)``.
    """

    def __init__(self, label: str):
        self.label = label
        self.state = "pending"  # pending | running | done | failed
        self.done = 0
        self.total: Optional[int] = None
        self.work_done = 0  # units that did real work (converted/fixed/hashed)
        self._dur_sum = 0.0
        self._dur_n = 0
        self._t0: Optional[float] = None

    def start(self) -> None:
        self.state = "running"
        self._t0 = time.monotonic()

    def set_total(self, n: int) -> None:
        self.total = n

    def advance(self, seconds: Optional[float] = None) -> None:
        self.done += 1
        if seconds is not None:
            self.work_done += 1
            self._dur_sum += seconds
            self._dur_n += 1

    def finish(self, ok: bool = True) -> None:
        self.state = "done" if ok else "failed"

    def _per_item_s(self) -> Optional[float]:
        """Seconds per item: the recorded convert-time average when the caller
        passes per-item durations to :meth:`advance`, otherwise a wall-clock
        estimate (elapsed / items done). The fallback means any batch verb gets
        a rate + ETA for free, without having to time each item itself."""
        if self._dur_n:
            return self._dur_sum / self._dur_n
        if self._t0 is not None and self.done > 0:
            return (time.monotonic() - self._t0) / self.done
        return None

    def eta_s(self) -> Optional[float]:
        """Estimated seconds until this chunk finishes (None if unknown)."""
        per = self._per_item_s()
        if per is None or self.total is None:
            return None
        return max(0.0, (self.total - self.done) * per)

    def describe(self, *, timing: bool = True) -> str:
        tot = str(self.total) if self.total is not None else "?"
        s = f"{self.label} {self.done}/{tot}"
        if timing:
            per = self._per_item_s()
            if per is not None:
                s += f" ({per:.1f}s/item"
                eta = self.eta_s()
                if eta is not None:
                    s += f", {fmt_time_left(eta)}"
                s += ")"
        return s


class ProgressBoard:
    """Periodic status for a parallel batch — log-friendly.

    Appends a status block every ``interval`` seconds (no ANSI, no carriage
    returns — safe for terminals, logs, and systemd journals). A summary
    header (chunks done + overall ETA), then one indented line per running
    chunk with its own rate + ETA:

        ⏳ 0/4 chunks done · ETA ~0:24h
              RHOF 2024 19/366 (4.3s/item, ETA 0:24h)
              RHOF 2025 32/365 (3.9s/item, ETA 0:21h)
              RHOF 2026 39/187 (3.5s/item, ETA 0:08h)
              RHOF 2023 19/22  (4.0s/item, ETA 0:00h)

    Once ≤3 chunks are still running it compacts them onto one line (the
    overall ETA stays in the header):

        ⏳ 1/4 chunks done · ETA ~0:24h
              RHOF 2024 81/366 | RHOF 2025 89/365 | RHOF 2026 88/187

    Rates/ETAs come from wall-clock by default, so every batch verb gets them
    without timing each item. Use as a context manager around
    :func:`run_chunks`; create one handle per chunk up front.
    """

    def __init__(self, interval: int = 30, out: Callable[[str], None] = print):
        self.interval = max(5, interval)
        self.out = out
        self.handles: List[ChunkProgress] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def handle(self, label: str) -> ChunkProgress:
        h = ChunkProgress(label)
        self.handles.append(h)
        return h

    def render(self) -> str:
        running = [h for h in self.handles if h.state == "running"]
        done = sum(1 for h in self.handles if h.state in ("done", "failed"))
        failed = sum(1 for h in self.handles if h.state == "failed")
        header = f"⏳ {done}/{len(self.handles)} chunks done" + (
            f", {failed} FAILED" if failed else ""
        )
        # Overall ETA = the slowest still-running chunk (the batch finishes with
        # it). Approximate: ignores not-yet-started chunks when chunks > workers.
        etas = [e for e in (h.eta_s() for h in running) if e is not None]
        if etas:
            header += f" · {fmt_time_left(max(etas))}"
        if not running:
            return header
        # Few chunks left: fit them on one line (counts only; ETA is in the
        # header). Many: one per line, each with its own rate + ETA.
        if len(running) <= 3:
            joined = " | ".join(h.describe(timing=False) for h in running)
            return f"{header}\n      {joined}"
        lines = [f"      {h.describe()}" for h in running[:6]]
        if len(running) > 6:
            lines.append(f"      (+{len(running) - 6} more running)")
        return "\n".join([header, *lines])

    def _report_loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.out(self.render())

    def __enter__(self) -> ProgressBoard:
        self._thread = threading.Thread(
            target=self._report_loop, name="progress-board", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)


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
