"""Per-run metrics for the long-term backfill — the row behind the log line.

Until this module, an LTB run reported itself ONLY as a log line::

    Long-term backfill(daily) done: 38 station/session had queued gaps,
    budget 217/600 slots, 1480s/3600s

Nothing consumed it: getting a morning's numbers meant grepping a 182 MB log,
and nothing trended the run or would notice a regression. That matters for
the one failure mode this job has that the pipeline dashboards cannot see —
**hitting the wall-clock cap**. Live downloads are unaffected when the LTB
stops early, so every dashboard stays green while the job quietly recovers
less each night. `scheduler.yaml` records that the first three fleet runs all
stopped on the 3600 s clock. Raising the daily lookback 30 -> 90 days makes
that the main risk, so it has to be a row, not a grep.

One row per run goes to ``ltb_runs`` (migration 075). The columns are the
budget ceilings and what was used of them, WHICH ceiling stopped the run
(``stop_reason``, NULL = ran to completion), and the per-station outcomes
summed across the run.

Two rules, both non-negotiable:

* **Recording must never break or slow a run.** This job moves real
  scientific data; observability must not become a new failure mode. Every
  step here — building the row, opening the connection, the INSERT — is
  wrapped, degrades to a WARNING, and the job returns exactly as it would
  have. Cost on the happy path is one INSERT after the work is done; on a
  dead DB it is bounded by ``connect_timeout`` (10 s) on the connect, and
  even that is paid AFTER the run's work.
* **Observation only.** Nothing here feeds back into classification,
  throttling or ordering. The row is a record of what the run did.

Why NOT ``single_host=True``
----------------------------
Every other query in ``long_term_backfill.py`` passes ``single_host=True``
because it is read-only. This one is a write that the production Grafana
needs: grafana.vedur.is reads ``gps_health`` on **pgdev**, while the LTB
runs on rek-d01 and writes rek-d01's DB. The existing bridge is the
``mirror_host`` dual-write in ``database_factory`` (rek-d01's
``database.cfg`` names ``pgdev.vedur.is``; install.sh patches it in), which
fans out every statement on a non-``single_host`` connection. The INSERT is
keyed on a natural key (``run_kind, started_at``), never on ``id``, so the
id-keyed-DML guard does not refuse it and both hosts get the same row. The
mirror leg is itself best-effort (a failure is logged, never raised), so a
pgdev outage costs the panel a point, not the run anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger("receivers.scheduler.long_term_backfill.metrics")

#: The two jobs that share the ``long_term_backfill:`` config section but run
#: against different ceilings (1800 s daily vs 600 s reconnect by default).
RUN_KINDS = ("daily", "reconnect")

#: Categorical form of ``RunBudget.exhausted_reason``. The budget renders
#: "wall-clock 3601s >= 3600s" / "slots 600 >= 600"; the first token is the
#: ceiling. "other" exists so a future third ceiling still records a row
#: instead of tripping the CHECK constraint — the verbatim text is kept in
#: ``stop_detail`` regardless.
STOP_REASONS = ("wall-clock", "slots", "other")

# Explicit column list, shared by the INSERT and the tests, so the SQL and the
# dataclass cannot drift apart silently.
_COLUMNS = (
    "run_kind",
    "started_at",
    "finished_at",
    "duration_seconds",
    "max_run_seconds",
    "slots_used",
    "max_slots",
    "stop_reason",
    "stop_detail",
    "stations",
    "sessions",
    "lookback",
    "queued_station_sessions",
    "recovered",
    "nothing_on_receiver",
    "failed",
    "unreachable",
    "budget_capped",
    "skipped_offline",
    "errors",
)

# ON CONFLICT on the natural key, NOT the surrogate id: that is what keeps the
# mirror fan-out safe (ids differ per host) and a re-run of the same row a
# no-op instead of a duplicate.
INSERT_SQL = (
    f"INSERT INTO ltb_runs ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(_COLUMNS))}) "
    "ON CONFLICT (run_kind, started_at) DO NOTHING"
)


@dataclass
class LTBRunMetrics:
    """One row of ``ltb_runs``."""

    run_kind: str
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    max_run_seconds: Optional[float]
    slots_used: int
    max_slots: Optional[int]
    stop_reason: Optional[str]  # None = ran to completion
    stop_detail: Optional[str]  # RunBudget.exhausted_reason verbatim
    stations: int
    sessions: list[str] = field(default_factory=list)
    lookback: str = ""
    # --- outcomes, summed over every per-station report the run produced ---
    queued_station_sessions: int = 0
    recovered: int = 0
    nothing_on_receiver: int = 0
    failed: int = 0
    unreachable: int = 0
    budget_capped: int = 0
    skipped_offline: int = 0
    errors: int = 0  # station/session calls that raised (no report at all)

    def as_row(self) -> tuple:
        return tuple(getattr(self, c) for c in _COLUMNS)

    def describe(self) -> str:
        cap_s = f"{self.max_run_seconds:.0f}s" if self.max_run_seconds else "unbounded"
        cap_n = str(self.max_slots) if self.max_slots else "unbounded"
        return (
            f"{self.run_kind}: {self.slots_used}/{cap_n} slots, "
            f"{self.duration_seconds:.0f}s/{cap_s}, "
            f"stop={self.stop_reason or 'completed'}, "
            f"recovered={self.recovered} nothing_on_receiver="
            f"{self.nothing_on_receiver} failed={self.failed}"
        )


def stop_reason_from(exhausted_reason: Optional[str]) -> Optional[str]:
    """Map ``RunBudget.exhausted_reason`` to its categorical column value."""
    if not exhausted_reason:
        return None
    head = exhausted_reason.strip().split(" ", 1)[0]
    return head if head in STOP_REASONS else "other"


def aggregate_reports(reports: Iterable[Any]) -> dict[str, int]:
    """Sum the per-station outcomes into the run's totals.

    Tolerant of partial report objects (``getattr`` with defaults): the job
    tests drive the job with ``SimpleNamespace`` reports carrying only
    ``queued``/``skipped_offline``, and a report that lacks a field must count
    as zero, not blow up the run.
    """
    tot = {
        "queued_station_sessions": 0,
        "recovered": 0,
        "nothing_on_receiver": 0,
        "failed": 0,
        "unreachable": 0,
        "budget_capped": 0,
        "skipped_offline": 0,
    }
    for r in reports:
        if r is None:
            continue
        if getattr(r, "queued", None):
            tot["queued_station_sessions"] += 1
        tot["recovered"] += int(getattr(r, "recovered", 0) or 0)
        tot["nothing_on_receiver"] += int(getattr(r, "nothing_on_receiver", 0) or 0)
        tot["failed"] += int(getattr(r, "failed", 0) or 0)
        tot["unreachable"] += int(getattr(r, "unreachable_slots", 0) or 0)
        tot["budget_capped"] += int(getattr(r, "budget_capped", 0) or 0)
        if getattr(r, "skipped_offline", False):
            tot["skipped_offline"] += 1
    return tot


def build_run_metrics(
    run_kind: str,
    budget: Any,
    started_at: datetime,
    reports: Iterable[Any],
    stations: int,
    sessions: Sequence[str],
    lookback: str,
    errors: int = 0,
    finished_at: Optional[datetime] = None,
) -> LTBRunMetrics:
    """Assemble the row from the run's ``RunBudget`` and per-station reports."""
    if run_kind not in RUN_KINDS:
        raise ValueError(f"run_kind must be one of {RUN_KINDS}, got {run_kind!r}")
    reason = getattr(budget, "exhausted_reason", None)
    return LTBRunMetrics(
        run_kind=run_kind,
        started_at=started_at,
        finished_at=finished_at or datetime.now(UTC),
        duration_seconds=float(budget.elapsed()),
        max_run_seconds=(
            float(budget.max_seconds) if budget.max_seconds is not None else None
        ),
        slots_used=int(budget.slots_used),
        max_slots=int(budget.max_slots) if budget.max_slots is not None else None,
        stop_reason=stop_reason_from(reason),
        stop_detail=reason,
        stations=int(stations),
        sessions=list(sessions),
        lookback=str(lookback),
        errors=int(errors),
        **aggregate_reports(reports),
    )


def record_ltb_run(metrics: LTBRunMetrics) -> bool:
    """INSERT one row, best-effort. **Never raises.**

    Returns True when the primary accepted the row. A failure is a WARNING
    with the whole row rendered inline, so the log still carries what the
    table missed.
    """
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        # Deliberately NOT single_host=True — see the module docstring: the
        # mirror fan-out is how this row reaches pgdev for grafana.vedur.is.
        with (
            DatabaseConnectionFactory.connection() as conn,
            conn.cursor() as cur,
        ):
            cur.execute(INSERT_SQL, metrics.as_row())
        logger.debug("LTB run recorded: %s", metrics.describe())
        return True
    except Exception as e:  # noqa: BLE001 — observability must not fail the run
        logger.warning(
            "LTB run metrics NOT recorded (%s) — the run itself is unaffected; "
            "row was: %s",
            e,
            metrics.describe(),
        )
        return False


def record_run(
    run_kind: str,
    budget: Any,
    started_at: datetime,
    reports: Iterable[Any],
    stations: int,
    sessions: Sequence[str],
    lookback: str,
    errors: int = 0,
) -> Optional[LTBRunMetrics]:
    """The one call a job tail makes: build the row and record it.

    Building is wrapped separately from writing so that a bug in aggregation
    (a report missing a field, a bad budget object) is ALSO just a warning —
    the job returns normally either way. Returns the row for the caller's
    log, or None if it could not even be built.
    """
    try:
        metrics = build_run_metrics(
            run_kind,
            budget,
            started_at,
            list(reports),
            stations,
            sessions,
            lookback,
            errors=errors,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "LTB run metrics could not be built (%s run) — run unaffected: %s",
            run_kind,
            e,
        )
        return None
    record_ltb_run(metrics)
    return metrics
