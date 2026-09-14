"""Long-term backfill — recover multi-day/month gaps when a station returns.

DB-driven and **classified**: query ``file_tracking`` / ``file_absence`` /
``receiver_horizon`` to build a per-station worklist bucketed as:

  ``queued``             — not in archive (raw *or* rinex), not confirmed gone
                           → download (full pipeline)
  ``confirmed_gone``     — ``file_absence.terminal`` OR older than ``receiver_horizon``
                           → skip (ran off the receiver auto-delete cycle)
  ``provisional_absent`` — ``file_absence`` row, not yet terminal → low-priority retry
  ``already_ok``         — present in ``archive_catalog`` → skip
  ``not_settled``        — too recent for absence to mean anything yet → skip

This module is the **read-only classification engine** (this file) plus, later,
the worker that runs the full pipeline per ``queued`` day. The scheduler wiring
(reconnection trigger + daily backstop) is added separately — see
``docs/design/long-term-backfill.md``. Track via receivers todo #136.

The classification deliberately distinguishes "couldn't reach" (no signal here)
from "reached and confirmed absent" (``confirmed_gone``): a transient connection
failure never records an absence, so it never pollutes this worklist.

.. note::
   **Where the reachability gate actually is** — this docstring used to say
   "the health oracle gates that in the worker", which was vague enough to be
   misleading in both directions. Verified 2026-09-14:

   * NOTHING in *this module* gates on reachability.
     ``_run_long_term_backfill_job`` filters only on the STATIC cfg fields
     ``station_status``/``health_check``; ``_run_reconnection_backfill_job``
     reads ``station_connectivity`` purely to *find* recently-reconnected
     stations, as a trigger, not a gate.
   * But the gate DOES exist one level down, per day, in every driver's
     ``download_data`` (``polarx5.py``, ``netrs.py``, ``netr9.py``, ``g10.py``:
     ``if not self._quick_ping(): ... skipping download``). So an unreachable
     station is not hammered with real transfers.

   The residual cost is therefore a **ping per queued slot, not a download** —
   measured at 5-9 s each in production. That still matters at LTB's scale:
   a dead station with a full 720-hour queue burns ~1 h of a worker per run,
   and the daily job runs only ``max_workers=2``. Real today — on 2026-09-14
   SKDA, SVIN and THNA each queued 720 hours having been ping-unreachable for
   **111 days** with no ``station_status`` set at all.

   So S3 wants a per-STATION short-circuit (skip the station once, not once
   per slot), and the three dead stations want ``station_status = inactive``
   in gps-config-data. Neither is a download storm; both are waste.

Read-only: never calls ``sync_archive_to_db`` (``sync_first=False``), so it
cannot mutate ``file_tracking``.

The presence oracle is ``archive_catalog``, NOT the filesystem
--------------------------------------------------------------
This module used to ask the local filesystem "is this file on disk?" via
``GapDetector.find_gaps``. That is the wrong question for a *long-term*
backfill: ``local_prune`` empties the local rolling window after 21 days for
1Hz while ``lookback_days`` is 30-90, so every healthy station classified its
own archived hours as gaps, forever. Measured on THOB 2026-09-13:
``queued=206, already_ok=0`` on a station with zero real gaps.

So the oracle is now the long-term archive's index, keyed on
``canonical_key`` and filtered to ``storage_location='imo_archive'``:

* **``canonical_key``, never ``file_date``.** ``archive_catalog.file_date``
  carries known bad values (the imo_archive tier spans ``0012-08-18`` to
  ``2027-01-01``) from the July mis-dating incident and todo #170. The
  canonical key embeds station, date, hour and session letter, so a key
  lookup is immune to all of it. The query hits
  ``archive_catalog_logical_key``, the UNIQUE index on exactly
  ``(storage_location, session_type, file_category, canonical_key)``.
* **``imo_archive`` only.** ``local_raw`` / ``local_rinex`` are the same
  rolling window in DB form; reading them would reintroduce the very bug this
  replaces.
* **Recent is not absent.** The archive lags local production: a file is
  downloaded, converted and pushed, and the hourly ``archive-sync`` sweep
  (:45) reconciles whatever the immediate push missed. So a slot from the last
  couple of hours is routinely on local disk while still absent from
  ``archive_catalog``. Slots newer than ``settle_hours`` are therefore counted
  as ``not_settled`` and never queued — see :func:`_settle_cutoff`.
* **Failure is not a gap.** If the catalog cannot be read, the report comes
  back EMPTY with a warning. Classifying everything as a gap on a failed query
  would queue a whole lookback window per station — the stampede this feature
  was disabled for. There is deliberately no filesystem fallback: silently
  falling back to the oracle being replaced is the worst available outcome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, timedelta
from typing import Any, Optional

logger = logging.getLogger("receivers.scheduler.long_term_backfill")


@dataclass
class LongTermGapReport:
    """Classified long-term gap for one (station, session)."""

    sid: str
    session: str
    start: date
    end: date
    last_archived: Optional[date] = None
    receiver_horizon: Optional[date] = None  # oldest file the receiver still holds
    queued: list[Any] = field(default_factory=list)  # download candidates (GapInfo)
    confirmed_gone: int = 0  # terminal-absent or past the horizon
    provisional_absent: int = 0  # absent, not yet terminal
    already_ok: int = 0  # present in archive within the window
    not_settled: int = 0  # too recent for archive absence to be evidence

    @property
    def total_window_days(self) -> int:
        return (self.end - self.start).days + 1 if self.start <= self.end else 0

    def summary(self) -> str:
        hz = self.receiver_horizon.isoformat() if self.receiver_horizon else "unknown"
        la = self.last_archived.isoformat() if self.last_archived else "none"
        return (
            f"{self.sid} {self.session} [{self.start} → {self.end}] "
            f"({self.total_window_days}d) | last_archived={la} horizon={hz} | "
            f"queued={len(self.queued)} confirmed_gone={self.confirmed_gone} "
            f"provisional_absent={self.provisional_absent} already_ok={self.already_ok} "
            f"not_settled={self.not_settled}"
        )


def _last_archived_date(sid: str, session: str) -> Optional[date]:
    """Most recent file_date with a present status for sid/session."""
    from ..health.database_factory import DatabaseConnectionFactory

    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT max(file_date) FROM file_tracking "
                "WHERE sid=%s AND session_type=%s "
                "AND status IN ('archived','downloaded')",
                (sid, session),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning("last_archived_date %s/%s: %s", sid, session, e)
        return None


def _receiver_horizon(sid: str, session: str) -> Optional[date]:
    """Oldest file date the receiver still holds (the auto-delete frontier)."""
    from ..health.database_factory import DatabaseConnectionFactory

    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT oldest_date FROM receiver_horizon "
                "WHERE sid=%s AND session_type=%s "
                "ORDER BY observed_at DESC LIMIT 1",
                (sid, session),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:  # noqa: BLE001
        logger.debug("receiver_horizon %s/%s: %s", sid, session, e)
        return None


def _absence_counts(sid: str, session: str, start: date, end: date) -> tuple[int, int]:
    """Return (terminal_gone, provisional_absent) counts in [start, end]."""
    from ..health.database_factory import DatabaseConnectionFactory

    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT count(*) FILTER (WHERE terminal), "
                "       count(*) FILTER (WHERE NOT terminal) "
                "FROM file_absence "
                "WHERE sid=%s AND session_type=%s AND file_date BETWEEN %s AND %s",
                (sid, session, start, end),
            )
            term, prov = cur.fetchone()
            return int(term or 0), int(prov or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("absence_counts %s/%s: %s", sid, session, e)
        return 0, 0


def _archived_dates(sid: str, session: str, start: date, end: date) -> set:
    """Set of file_dates with a present status for sid/session in [start, end]."""
    from ..health.database_factory import DatabaseConnectionFactory

    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT file_date FROM file_tracking "
                "WHERE sid=%s AND session_type=%s "
                "AND status IN ('archived','downloaded') "
                "AND file_date BETWEEN %s AND %s",
                (sid, session, start, end),
            )
            return {r[0] for r in cur.fetchall()}
    except Exception as e:  # noqa: BLE001
        logger.warning("archived_dates %s/%s: %s", sid, session, e)
        return set()


# The long-term archive tier in ``archive_catalog``. NOT ``local_raw`` /
# ``local_rinex`` — those mirror the rolling local window that ``local_prune``
# empties, which is the bug this oracle exists to fix. A test pins this.
ARCHIVE_STORAGE_LOCATION = "imo_archive"


@dataclass(frozen=True)
class _Slot:
    """One expected file slot and the canonical keys that would satisfy it."""

    file_date: date
    file_hour: Optional[int]
    raw_key: str
    rinex_keys: tuple[str, ...]
    raw_path: str


def _rinex_keys(sid: str, dt, file_hour: Optional[int]) -> tuple[str, ...]:
    """Canonical keys of the IGS short-name RINEX products for one slot.

    ``ArchiveFileChecker.build_archive_path`` cannot be used for this: given a
    ``"<session>_rinex"`` session it returns the *raw* filename unchanged (its
    extension comes from the receiver type), so a rinex lookup built from it
    can never match anything. Verified on rek-d01 2026-09-13 — THOB's expected
    "rinex" key came back as ``thob202609100000b.sbf`` while the archive holds
    ``THOB253a.26D.Z``. That silently reduced the raw/rinex union to raw only,
    which is invisible on a healthy PolaRX5 station but wrong for a
    stream-acquired one: GONH (mosaic-X5, RTCM3 -> BNC -> RINEX) has almost no
    1Hz raw, so it scored 684 false gaps out of 720 with 3,026 of its RINEX
    hours sitting in the catalog.

    So the name is built from gtimes' ``#Rin2`` directly, hourly-vs-daily by
    frequency: ``1H`` yields ``THOB253a.26D`` (hour letter a-x) and ``1D``
    yields ``THOB2530.26D``.

    Two keys are returned, Hatanaka ``d`` and plain ``o``. ``canonical_key``
    deliberately does not fold that pair (they carry different
    ``content_sha256`` values), but for "does the data product exist?" either
    encoding answers yes.
    """
    from ..utils.canonical_key import canonical_key

    freq = "1H" if file_hour is not None else "1D"
    keys: list[str] = []
    for ext in ("D", "O"):
        try:
            import gtimes.timefunc as gt

            name = gt.datepathlist(f"{sid}#Rin2{ext}", freq, datelist=[dt])[0]
        except Exception:  # noqa: BLE001
            continue
        keys.append(canonical_key(name))
    return tuple(dict.fromkeys(keys))


# How long after a slot ENDS before its absence from the long-term archive is
# evidence of a real gap. Measured on rek-d01 2026-09-14 across 169 online
# stations, newest 1Hz slot present in `imo_archive`:
#
#     1.4 h behind now -> 152 stations   3.4 h -> 3
#     2.4 h behind now ->  12 stations   4.4 h -> 1,  8.4 h -> 1
#
# So 6 h covers all but one outlier, with ~2.5x margin on the common case.
#
# Being generous here is nearly free: the ordinary backfill window (:25-:55)
# already reaches back `files_back=36` HOURS for hourly sessions, so anything
# LTB declines to look at inside that reach is covered by the normal path.
# Raise this toward 36 to stop LTB competing with gap detection at all; do NOT
# lower it below ~4 without re-measuring, or healthy stations resume reporting
# their own most recent hour as a gap.
DEFAULT_SETTLE_HOURS = 6


def _settle_cutoff(settle_hours: int):
    """Slots ENDING after this instant are too recent to classify."""
    from datetime import datetime

    return datetime.now(UTC) - timedelta(hours=settle_hours)


def _slot_end(file_date: date, file_hour: Optional[int]):
    """When the data for this slot finished being recorded (UTC, aware).

    An hourly slot for hour H ends at H+1; a daily slot ends at midnight the
    next day. Absence before this instant is meaningless — the file does not
    exist yet, anywhere.
    """
    from datetime import datetime

    base = datetime.combine(file_date, datetime.min.time(), tzinfo=UTC)
    if file_hour is None:
        return base + timedelta(days=1)
    return base + timedelta(hours=file_hour + 1)


def _expected_slots(
    sid: str,
    session: str,
    start: date,
    end: date,
    receiver_type: Optional[str],
) -> list[_Slot]:
    """Every (date, hour) slot in the window, with its raw + rinex canonical keys.

    The slot list and the RAW filename come from ``GapDetector`` so this
    classifier and ordinary gap detection can never disagree about what a
    station is *supposed* to produce — only about where they look for it. The
    RINEX names come from :func:`_rinex_keys`; see there for why they cannot.
    """
    from datetime import datetime

    from ..health.file_tracker import GapDetector
    from ..utils.canonical_key import canonical_key

    slots: list[_Slot] = []
    with GapDetector() as det:
        # Private, but deliberately: it is the single definition of "daily vs
        # hourly" that find_gaps itself uses. Re-deriving it here would let the
        # two drift. tests/test_ltb_catalog_oracle.py fails if it disappears.
        expected = det._generate_expected_files(sid, session, start, end)
        for file_date, file_hour in expected:
            dt = datetime.combine(file_date, datetime.min.time())
            if file_hour is not None:
                dt = dt.replace(hour=file_hour)
            raw_path = det.archive_checker.build_archive_path(
                sid, session, dt, receiver_type
            )
            slots.append(
                _Slot(
                    file_date=file_date,
                    file_hour=file_hour,
                    raw_key=canonical_key(raw_path),
                    rinex_keys=_rinex_keys(sid, dt, file_hour),
                    raw_path=raw_path,
                )
            )
    return slots


def _catalog_present_keys(
    session: str, file_category: str, keys: list[str]
) -> Optional[set[str]]:
    """Which of ``keys`` the long-term archive catalog holds for this session.

    Keyed purely on ``canonical_key`` — there is deliberately NO ``file_date``
    predicate, because that column carries known-bad values (see the module
    docstring). Hits ``archive_catalog_logical_key``.

    Returns:
        The present subset, or ``None`` if the catalog could not be read.
        ``None`` means "no oracle", NOT "nothing is present": the caller must
        abandon the run rather than classify the whole window as a gap.
    """
    if not keys:
        return set()

    from ..health.database_factory import DatabaseConnectionFactory

    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT canonical_key FROM archive_catalog "
                "WHERE storage_location = %s AND session_type = %s "
                "AND file_category = %s AND canonical_key = ANY(%s)",
                (ARCHIVE_STORAGE_LOCATION, session, file_category, list(keys)),
            )
            return {r[0] for r in cur.fetchall()}
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "archive_catalog lookup failed (%s/%s, %d keys): %s",
            session,
            file_category,
            len(keys),
            e,
        )
        return None


def _known_missing_slots(
    sid: str, session: str, slots: list[_Slot]
) -> set[tuple[date, Optional[int]]]:
    """Slots the receiver is already known not to hold.

    Batched equivalent of the ``skip_missing_on_receiver=True`` branch inside
    ``find_gaps``: same ``is_file_missing()`` SQL function, same
    ``use_terminal_absence`` flag, one round trip instead of one per slot.

    Fails CLOSED-ish by design: on error it returns an empty set, i.e. nothing
    is skipped. That can only make the worklist larger, never silently drop a
    day that really is recoverable, and the horizon floor still bounds it.
    """
    if not slots:
        return set()

    from ..health.file_tracker import FileTracker

    try:
        tracker = FileTracker()
        if not tracker.connect():
            return set()
        use_terminal = tracker._use_terminal_absence()
        with tracker.read_cursor() as cur:
            cur.execute(
                "SELECT t.file_date, t.file_hour "
                "FROM unnest(%s::date[], %s::smallint[]) AS t(file_date, file_hour) "
                "WHERE is_file_missing(%s, %s, t.file_date, t.file_hour, %s)",
                (
                    [sl.file_date for sl in slots],
                    [sl.file_hour for sl in slots],
                    sid,
                    session,
                    use_terminal,
                ),
            )
            return {(r[0], r[1]) for r in cur.fetchall()}
    except Exception as e:  # noqa: BLE001
        logger.debug("known_missing_slots %s/%s: %s", sid, session, e)
        return set()


def query_long_term_gaps(
    sid: str,
    session: str,
    lookback_days: int = 365,
    receiver_type: Optional[str] = None,
    settle_hours: int = DEFAULT_SETTLE_HOURS,
) -> LongTermGapReport:
    """Classify the long-term gap for one station/session.

    Scans the FULL ``lookback_days`` window (not just trailing) so mid-range
    holes — e.g. a month missing between present data, like SARP's June — are
    found. Read-only; safe to run any time.

    Presence comes from ``archive_catalog`` (see the module docstring), so a
    station whose local rolling window has been pruned no longer false-gaps.
    If the catalog cannot be read the report comes back empty — never full.

    Slots that ended less than ``settle_hours`` ago are reported as
    ``not_settled`` rather than queued: the long-term archive lags local
    production, so their absence is not yet evidence. Without this every
    healthy station reports its own most recent hour as a gap — measured
    2026-09-14, THOB/OLKE/GONH each queued exactly ``(2026-09-13, 23)``, a
    file collected ~50 min earlier and sitting on local disk.

    The raw/rinex intersection is per SLOT, i.e. ``(file_date, file_hour)``.
    It used to be ``{g.file_date for g in rinex_gaps}``, a set of *dates*, so
    for an hourly session a single missing rinex hour made every raw-missing
    hour that day a download candidate. Harmless while the feature was off and
    only exercised on daily ``15s_24hr``; wrong for the 1Hz window #174 needs.

    Args:
        sid: Station id, e.g. ``"SARP"``.
        session: Session type, e.g. ``"15s_24hr"``.
        lookback_days: Hard cap on how far back to look.
        receiver_type: Optional, used to build the correct archive
            path/extension per receiver family. Auto-looked-up from station
            config when None (required — the PolaRX5 default path would
            false-gap every NetRS/NetR9 day).
        settle_hours: How long after a slot ends before its absence counts.
            See :data:`DEFAULT_SETTLE_HOURS` for the measurement behind the
            default. ``0`` disables the guard (tests only — in production it
            re-creates the trailing-edge false gap).
    """
    sid = sid.upper()
    last_archived = _last_archived_date(sid, session)
    horizon = _receiver_horizon(sid, session)

    if receiver_type is None:
        try:
            from ..cli.main import get_station_config

            receiver_type = (get_station_config(sid) or {}).get("receiver_type")
        except Exception:  # noqa: BLE001
            pass

    end = date.today() - timedelta(days=1)  # yesterday
    start = end - timedelta(days=lookback_days - 1)

    report = LongTermGapReport(
        sid=sid,
        session=session,
        start=start,
        end=end,
        last_archived=last_archived,
        receiver_horizon=horizon,
    )
    if start > end:
        return report  # fully up to date

    # Download candidates: absent from the LONG-TERM ARCHIVE and not
    # known-missing-on-receiver. A slot counts as satisfied when EITHER the raw
    # OR the rinex product is catalogued — a rinex file whose raw was pruned is
    # still the data product, so it is not a real gap.
    try:
        slots = _expected_slots(sid, session, start, end, receiver_type)
    except Exception as e:  # noqa: BLE001
        logger.warning("expected_slots %s/%s: %s", sid, session, e)
        return report

    raw_present = _catalog_present_keys(session, "raw", [sl.raw_key for sl in slots])
    rinex_present = _catalog_present_keys(
        session, "rinex", [k for sl in slots for k in sl.rinex_keys]
    )
    if raw_present is None or rinex_present is None:
        # No oracle. Returning the empty report leaves queued=[] and
        # already_ok=0, so the run is a no-op and the next one retries. Falling
        # back to the filesystem here would restore the exact bug this replaces.
        logger.warning(
            "Long-term backfill %s/%s: archive_catalog unavailable — "
            "skipping classification rather than queueing the whole window",
            sid,
            session,
        )
        return report

    term, prov = _absence_counts(sid, session, start, end)
    report.confirmed_gone = term
    report.provisional_absent = prov

    known_missing = _known_missing_slots(sid, session, slots)
    horizon_floor = horizon or date.min

    from ..health.file_tracker import GapInfo

    queued: list[Any] = []
    cutoff = _settle_cutoff(settle_hours)
    for sl in slots:
        if sl.raw_key in raw_present or rinex_present.intersection(sl.rinex_keys):
            report.already_ok += 1
            continue
        # Absence is only evidence once the archive has had time to catch up.
        # Checked AFTER presence so a slot already in the catalog still counts
        # as already_ok rather than being masked as "too recent".
        if _slot_end(sl.file_date, sl.file_hour) > cutoff:
            report.not_settled += 1
            continue
        if (sl.file_date, sl.file_hour) in known_missing:
            continue
        if sl.file_date < horizon_floor:
            continue
        queued.append(
            GapInfo(
                station_id=sid,
                session_type=session,
                file_date=sl.file_date,
                file_hour=sl.file_hour,
                reason="not_in_archive_catalog",
                expected_path=sl.raw_path,
            )
        )
    report.queued = queued

    return report


def format_report(report: LongTermGapReport, max_queued: int = 12) -> str:
    """Human-readable rendering of a :class:`LongTermGapReport`."""
    lines = [report.summary()]
    if report.queued:
        lines.append(f"  queued (download candidates), first {max_queued}:")
        for g in report.queued[:max_queued]:
            hr = "" if g.file_hour is None else f" {g.file_hour:02d}h"
            lines.append(f"    {g.file_date}{hr}  ({g.reason})")
        rest = len(report.queued) - max_queued
        if rest > 0:
            lines.append(f"    … and {rest} more")
    else:
        lines.append("  queued: (none — nothing recoverable in window)")
    return "\n".join(lines)


def run_long_term_backfill_station(
    sid: str,
    session: str,
    lookback_days: int = 365,
    run_rinex: bool = True,
    dry_run: bool = False,
    max_days: Optional[int] = None,
    receiver_type: Optional[str] = None,
    settle_hours: int = DEFAULT_SETTLE_HOURS,
) -> LongTermGapReport:
    """Recover one station's classified gaps through the full per-day pipeline.

    Classify via :func:`query_long_term_gaps` (skips ``confirmed_gone``), then for
    each ``queued`` day run download → RINEX → archive → DB via the shared
    per-day backfill primitive. The download client records a receiver-absence
    on a verified ``not_found`` (never on a transient unreachable), so days the
    receiver has aged out get confirmed rather than re-attempted forever.

    Gateway push (archive_sync / EPOS) is deliberately downstream — see
    ``docs/design/long-term-backfill.md`` §6 — not in this per-day call.

    Args:
        sid, session, lookback_days, receiver_type: as for :func:`query_long_term_gaps`.
        run_rinex: run RINEX conversion after download (default True).
        dry_run: classify only, no downloads.
        max_days: cap how many queued days to process this run (throttle).
    """
    report = query_long_term_gaps(
        sid,
        session,
        lookback_days,
        receiver_type=receiver_type,
        settle_hours=settle_hours,
    )
    n = len(report.queued)
    if n == 0:
        logger.info(
            "Long-term backfill %s/%s: nothing queued — %s",
            sid,
            session,
            report.summary(),
        )
        return report

    queued = report.queued if not max_days else report.queued[:max_days]
    logger.info(
        "Long-term backfill %s/%s: %d/%d day(s) to recover%s",
        sid,
        session,
        len(queued),
        n,
        " [DRY RUN]" if dry_run else "",
    )
    if dry_run:
        return report

    from .backfill import _backfill_station_day_generic

    recovered = failed = 0
    for gap in queued:
        try:
            _backfill_station_day_generic(
                sid,
                gap.file_date,
                gap.file_date,  # backfill_end = this day → marks the day complete
                session,
                immediate_archive=True,
                run_rinex=run_rinex,
            )
            recovered += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning(
                "Long-term backfill %s/%s/%s failed: %s", sid, session, gap.file_date, e
            )
    logger.info(
        "Long-term backfill %s/%s done: recovered=%d failed=%d",
        sid,
        session,
        recovered,
        failed,
    )
    return report


def _load_monitor_overloaded() -> bool:
    """True if the system load monitor says RT is under pressure (yield signal).

    Lazy-imports the module-level monitor from bulk_scheduler; any failure is
    treated as "not overloaded" so a missing monitor never blocks recovery.
    """
    try:
        from .bulk_scheduler import _load_monitor

        if _load_monitor is None:
            return False
        load = _load_monitor.get_load()
        return load.cpu_load_1m > _load_monitor.max_cpu_load
    except Exception:  # noqa: BLE001
        return False


def _run_long_term_backfill_job(
    sessions=None,
    lookback_days: int = 365,
    max_workers: int = 2,
    max_days_per_station: Optional[int] = None,
    run_rinex: bool = True,
    settle_hours: int = DEFAULT_SETTLE_HOURS,
) -> None:
    """APScheduler **daily backstop**: classify every active station, recover gaps.

    Throttled (low ``max_workers``) and yields to RT via :func:`_load_monitor_overloaded`.
    The worker is idempotent (sync-skips present files), so a daily run is safe.
    This is the backstop; the reconnection trigger is the primary path.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from ..cli.main import get_all_station_configs

    sessions = sessions or ["15s_24hr", "1Hz_1hr"]
    active = [
        sid
        for sid, cfg in get_all_station_configs().items()
        if cfg.get("enabled", True)
        and cfg.get("station_status") not in ("discontinued", "inactive")
        and cfg.get("health_check") != "passive"
    ]
    logger.info(
        "Long-term backfill(daily): %d stations, sessions=%s, lookback=%dd",
        len(active),
        sessions,
        lookback_days,
    )

    def _one(sid: str, session: str):
        if _load_monitor_overloaded():
            logger.info("Long-term backfill: load high — deferring %s/%s", sid, session)
            return None
        try:
            report = run_long_term_backfill_station(
                sid,
                session,
                lookback_days=lookback_days,
                run_rinex=run_rinex,
                max_days=max_days_per_station,
                settle_hours=settle_hours,
            )
            return len(report.queued)
        except Exception as e:  # noqa: BLE001
            logger.warning("Long-term backfill %s/%s: %s", sid, session, e)
            return None

    tasks = [(sid, s) for sid in active for s in sessions]
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = [
            f.result()
            for f in as_completed(ex.submit(_one, sid, s) for sid, s in tasks)
        ]
    logger.info(
        "Long-term backfill(daily) done: %d station/session had queued gaps",
        sum(1 for r in results if r),
    )


def _run_reconnection_backfill_job(
    min_outage_days: int = 3,
    lookback_days: int = 365,
    run_rinex: bool = True,
    max_days_per_station: Optional[int] = None,
    reconnection_window_minutes: int = 20,
    settle_hours: int = DEFAULT_SETTLE_HOURS,
) -> None:
    """APScheduler **reconnection trigger**: recover stations that just came online.

    Queries ``station_connectivity`` for stations whose ``state_since`` is in the
    last window (recently reconnected); for each with a real outage (a queued gap
    older than ``min_outage_days``), runs the worker. This is the primary trigger;
    the daily job is the backstop. Yields to RT between stations.
    """
    from datetime import datetime, timedelta, timezone

    from ..health.database_factory import DatabaseConnectionFactory

    since = datetime.now(UTC) - timedelta(minutes=reconnection_window_minutes)
    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT sid FROM station_connectivity "
                "WHERE is_online = TRUE AND state_since > %s ORDER BY sid",
                (since,),
            )
            candidates = [r[0] for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        logger.warning("reconnection_backfill: connectivity query failed: %s", e)
        return

    if not candidates:
        return
    logger.info(
        "Long-term backfill(reconnect): %d station(s) came online recently: %s",
        len(candidates),
        ",".join(candidates),
    )

    floor = date.today() - timedelta(days=min_outage_days)
    for sid in candidates:
        if _load_monitor_overloaded():
            logger.info(
                "Long-term backfill(reconnect): load high — deferring remaining"
            )
            break
        try:
            report = query_long_term_gaps(
                sid, "15s_24hr", lookback_days=lookback_days, settle_hours=settle_hours
            )
            # only act on a real outage: a queued day older than min_outage_days,
            # so a brief flap doesn't trigger a heavy multi-month recovery.
            if report.queued and any(g.file_date <= floor for g in report.queued):
                run_long_term_backfill_station(
                    sid,
                    "15s_24hr",
                    lookback_days=lookback_days,
                    run_rinex=run_rinex,
                    max_days=max_days_per_station,
                    settle_hours=settle_hours,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Long-term backfill(reconnect) %s: %s", sid, e)
