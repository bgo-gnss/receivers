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
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, timedelta
from typing import Any, Optional

logger = logging.getLogger("receivers.scheduler.long_term_backfill")


# ---------------------------------------------------------------------------
# S3 — the throttle that gates re-enabling this feature
# ---------------------------------------------------------------------------
#
# scheduler.yaml's `long_term_backfill:` block says re-enable needs the
# idempotence bug fixed AND "either load_monitoring is back or another throttle
# exists". S1 closed the first half — recovery now sticks, because the
# classifier reads `archive_catalog`, so a recovered day comes back
# `already_ok` (verified on rek-d01 2026-09-14: GJFV's days backfilled 09-12
# all classify `already_ok`, queued=0). This section is the second half.
#
# It does NOT route through `_load_monitor_overloaded()`. That predicate
# returns False whenever `_load_monitor is None`, and `load_monitoring` is
# still disabled in scheduler.yaml — deliberately, because enabling it makes
# the reconciler gate on process-wide thread count and self-block live
# downloads. **Every existing `if _load_monitor_overloaded(): defer` call site
# in this module is therefore still dead code.** They are left in place for the
# day load_monitoring returns; nothing here depends on them.

# Wall-clock ceilings, per job — they cannot share one number.
#
# The daily backstop fires once at 04:00 and may take 30 min. The reconnection
# trigger fires every 15 MINUTES (`reconnection_schedule: "15m"`), so a ceiling
# has to sit comfortably under 900 s or a run is still going when its successor
# is due. That does not pile up — `max_instances=1, coalesce=True` means the
# intervening firings are silently DROPPED — which is worse than piling up,
# because the trigger then quietly stops being a 15-minute trigger.
#
# 600 s leaves 5 min of margin. An earlier revision used 1800 s for both and
# described it as "half the reconnection interval"; it is twice.
DEFAULT_MAX_RUN_SECONDS = 1800.0
DEFAULT_RECONNECT_MAX_RUN_SECONDS = 600.0
DEFAULT_MAX_SLOTS_PER_RUN = 600


@dataclass
class RunBudget:
    """Wall-clock and slot ceiling for ONE run of an LTB job.

    The per-station `max_days` cap does not bound a RUN: at 180 stations x 2
    sessions, `max_days_per_station=30` still authorises 10,800 slots. With the
    reachability gate costing a 5-9 s ping per slot (see the module docstring),
    that is a run measured in days, against `max_workers=2`. This bounds the
    run itself.

    **Wall-clock is the primary bound, slots the secondary.** A slot's cost
    varies by two orders of magnitude — an `already_ok` classification is
    microseconds, a real download is minutes — so a slot count alone cannot
    bound duration, and the slot cap is NOT what protects against the observed
    worst case ("one THEY pass ran 49 minutes", scheduler.yaml). The clock is.
    Each job passes its own ceiling; see the constants above for why they
    differ.

    Shared across the daily job's worker threads, so every accessor takes the
    lock. `take()` is the only way to reserve work: it hands back how much the
    caller may actually do, which is what lets a station shorten its own queue
    instead of checking the budget mid-loop and abandoning work half-done.
    """

    max_seconds: Optional[float] = DEFAULT_MAX_RUN_SECONDS
    max_slots: Optional[int] = DEFAULT_MAX_SLOTS_PER_RUN
    _started: float = field(default_factory=time.monotonic)
    _slots_used: int = 0
    _lock: Any = field(default_factory=threading.Lock, repr=False)
    exhausted_reason: Optional[str] = None

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def slots_used(self) -> int:
        with self._lock:
            return self._slots_used

    def exhausted(self) -> bool:
        """True once either ceiling is reached; records WHICH one, once."""
        with self._lock:
            return self._exhausted_locked()

    def _exhausted_locked(self) -> bool:
        if self.max_seconds is not None and self.elapsed() >= self.max_seconds:
            if self.exhausted_reason is None:
                self.exhausted_reason = (
                    f"wall-clock {self.elapsed():.0f}s >= {self.max_seconds:.0f}s"
                )
            return True
        if self.max_slots is not None and self._slots_used >= self.max_slots:
            if self.exhausted_reason is None:
                self.exhausted_reason = f"slots {self._slots_used} >= {self.max_slots}"
            return True
        return False

    def take(self, want: int) -> int:
        """Reserve up to `want` slots; returns how many were granted (>= 0).

        Reserving up front rather than checking per slot is what keeps a
        station's work coherent: it trims its queue to what it may finish
        instead of stopping mid-way through one.
        """
        if want <= 0:
            return 0
        with self._lock:
            if self._exhausted_locked():
                return 0
            if self.max_slots is None:
                granted = want
            else:
                granted = max(0, min(want, self.max_slots - self._slots_used))
            self._slots_used += granted
            return granted

    def give_back(self, n: int) -> None:
        """Return slots reserved but not used (a station that short-circuited).

        Without this, a station skipped for being offline would still spend its
        whole reservation and starve the stations behind it — the opposite of
        what the short-circuit is for.
        """
        if n <= 0:
            return
        with self._lock:
            self._slots_used = max(0, self._slots_used - n)

    def describe(self) -> str:
        cap_s = f"{self.max_seconds:.0f}s" if self.max_seconds else "unbounded"
        cap_n = str(self.max_slots) if self.max_slots else "unbounded"
        return (
            f"budget {self.slots_used}/{cap_n} slots, " f"{self.elapsed():.0f}s/{cap_s}"
        )


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
    # --- S3 throttle outcomes (set by the worker, not the classifier) ---
    skipped_offline: bool = False  # connectivity says down: whole queue skipped
    unreachable_slots: int = 0  # slots that hit the driver ping gate this run
    budget_capped: int = 0  # queued slots the run budget declined to start

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
            + (" | SKIPPED: station offline" if self.skipped_offline else "")
            + (
                f" | unreachable={self.unreachable_slots}"
                if self.unreachable_slots
                else ""
            )
            + (f" | budget_capped={self.budget_capped}" if self.budget_capped else "")
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


# How stale a `station_connectivity` row may be and still gate a whole queue.
# The health job refreshes every 5 min, so anything older than this means the
# monitor itself is not running and its verdict is not evidence.
CONNECTIVITY_TRUST_MINUTES = 30

# Reconnection re-attempt cooldown.
#
# The historical symptom ("VMEY's same 3 days recovered 5x in one day", the
# scheduler.yaml block) was the IDEMPOTENCE bug: recovery did not stick, so
# every tick re-queued the same days. S1 fixed that — a recovered day is
# `already_ok` on the next pass.
#
# What remains is narrower and bounded: `archive_catalog` lags a successful
# recovery by roughly the archive-sync interval (measured ~1.4 h for most
# stations), while the reconnection job runs every 15 min. A station that
# FLAPS therefore mints a fresh `state_since` each time and can be re-picked
# several times inside that lag window, re-attempting days already recovered.
#
# This is insurance, not a hot path: measured on rek-d01 2026-09-14, only 7
# stations reconnected in 24 h fleet-wide, and a non-flapping station's
# `state_since` does not change, so it naturally falls out of the 20-minute
# candidate window after ~2 ticks.
#
# In-process on purpose. A restart clears it, which is correct — the run
# budget still bounds the damage, and persisting it would mean a DB write per
# station per tick to suppress work that is already cheap when it does recur.
DEFAULT_REATTEMPT_COOLDOWN_MINUTES = 90

_attempt_lock = threading.Lock()
_last_attempt: dict[str, float] = {}


def _recently_attempted(sid: str, cooldown_minutes: int) -> bool:
    """True if `sid` was attempted within the cooldown (never blocks on error)."""
    if cooldown_minutes <= 0:
        return False
    with _attempt_lock:
        last = _last_attempt.get(sid)
    return last is not None and (time.monotonic() - last) < cooldown_minutes * 60


def _mark_attempted(sid: str) -> None:
    with _attempt_lock:
        _last_attempt[sid] = time.monotonic()


def _reset_attempt_history() -> None:
    """Clear the cooldown map. For tests; never called in production."""
    with _attempt_lock:
        _last_attempt.clear()


# Where the next run starts in the task list.
#
# MEASURED, not hypothetical: at the deployed `lookback_days: 90` across both
# sessions, 1,631 slots reach the receiver against a 600-slot budget, so 1,031
# are deferred every run. The daily job builds its task list as
# `[(sid, session) for sid in active for s in sessions]` — a STABLE
# alphabetical order. Serving it from the front each time means everything past
# the cut is never served at all, so a station late in the alphabet with a real
# gap would never be recovered while an earlier one is re-checked daily.
#
# Rotating the start point makes the deferral a queue instead of a cliff: over
# ~3 runs every station gets its turn. Kept in-process; a restart resets to the
# front, which costs at most one run's worth of fairness.
_rotation_lock = threading.Lock()
_rotation_cursor = 0


def _rotate(tasks: list) -> list:
    """Return `tasks` rotated to this run's start. Does NOT advance the cursor.

    Advancing is :func:`_advance_rotation`, called AFTER the run with the
    number of tasks that actually consumed budget. It cannot be done here: the
    cursor must move by tasks *served*, and most tasks classify to zero queued
    slots and cost nothing — advancing by the slot cap, or by the list length,
    would skip over stations that were never reached.
    """
    if not tasks:
        return tasks
    with _rotation_lock:
        start = _rotation_cursor % len(tasks)
    return tasks[start:] + tasks[:start]


def _advance_rotation(served: int, total: int) -> None:
    """Move the start point on by the number of tasks that did work."""
    global _rotation_cursor
    if total <= 0 or served <= 0:
        return
    with _rotation_lock:
        # `_rotate` re-applies this modulo, so it is belt-and-braces —
        # it keeps the stored cursor bounded rather than growing forever.
        _rotation_cursor = (_rotation_cursor + served) % total


def _reset_rotation() -> None:
    """For tests; never called in production."""
    global _rotation_cursor
    with _rotation_lock:
        _rotation_cursor = 0


def _station_is_offline(sid: str) -> Optional[bool]:
    """Is `sid` down, per `station_connectivity`? ``None`` when we cannot say.

    This is the cheap half of the per-station short-circuit: the health job
    already pings every station every 5 minutes and stores the verdict, so
    asking the DB costs nothing and saves up to 720 pings for a dead station.

    **Absence of evidence is never treated as "offline".** A missing row, a
    stale row, or a failed query all return ``None``, and the caller proceeds —
    a monitoring outage must not silently stop recovery fleet-wide. The
    expensive half (reacting to a live ping failure mid-run) is what covers
    the case this one misses: a station that is down while its row still says
    online.

    `station_connectivity` already requires two consecutive failed pings before
    reporting offline, so this does not fire on a single lossy-link blip.
    """
    from ..health.database_factory import DatabaseConnectionFactory

    try:
        with (
            DatabaseConnectionFactory.connection(single_host=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT is_online, last_check < now() - make_interval(mins => %s) "
                "FROM station_connectivity WHERE sid = %s",
                (CONNECTIVITY_TRUST_MINUTES, sid),
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 - never block recovery on a DB blip
        logger.debug("connectivity lookup failed for %s: %s", sid, e)
        return None
    if not row:
        return None
    is_online, is_stale = row[0], row[1]
    if is_online is None or is_stale:
        return None
    return not is_online


def run_long_term_backfill_station(
    sid: str,
    session: str,
    lookback_days: int = 365,
    run_rinex: bool = True,
    dry_run: bool = False,
    max_days: Optional[int] = None,
    receiver_type: Optional[str] = None,
    settle_hours: int = DEFAULT_SETTLE_HOURS,
    budget: Optional[RunBudget] = None,
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
        budget: shared per-RUN ceiling (see :class:`RunBudget`). When given,
            this station trims its queue to what the budget still allows and
            returns unused reservation, so one station cannot starve the rest.

    Throttles, in the order they apply (all reported in ``report.summary()``):

    1. **Station offline** → skip the whole queue, one DB read, zero pings.
    2. **Run budget** → trim the queue to what this run may still start.
    3. **Live unreachable** → abandon the remainder the moment the driver's own
       ping gate refuses a slot, which covers the station whose connectivity
       row is wrong.
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

    # (1) Station-level short-circuit, BEFORE any slot work. One DB read
    # replaces up to `len(queued)` pings at 5-9 s each.
    if _station_is_offline(sid):
        report.skipped_offline = True
        logger.info(
            "Long-term backfill %s/%s: station offline — skipping %d queued slot(s) "
            "(saved ~%d ping(s))",
            sid,
            session,
            n,
            n,
        )
        return report

    queued = report.queued if not max_days else report.queued[:max_days]

    # (2) Run budget. Reserve up front so the queue is trimmed coherently
    # rather than abandoned mid-slot, and so unused reservation goes back.
    reserved = len(queued)
    if budget is not None:
        granted = budget.take(reserved)
        if granted < reserved:
            report.budget_capped = reserved - granted
            logger.info(
                "Long-term backfill %s/%s: run budget allows %d of %d slot(s) (%s)",
                sid,
                session,
                granted,
                reserved,
                budget.describe(),
            )
        queued = queued[:granted]
        reserved = granted

    logger.info(
        "Long-term backfill %s/%s: %d/%d day(s) to recover%s",
        sid,
        session,
        len(queued),
        n,
        " [DRY RUN]" if dry_run else "",
    )
    if dry_run:
        if budget is not None:
            budget.give_back(reserved)
        return report

    from .backfill import _backfill_station_day_generic

    recovered = failed = 0
    for i, gap in enumerate(queued):
        # (3) Live reachability. `download_data` returns status='unreachable'
        # when its own ping gate refuses, which the shared primitive folds into
        # files_error and does NOT raise on — so before this, an unreachable
        # station drained its whole queue AND logged every slot as `recovered`.
        # The out-param surfaces it without changing the primitive's return
        # type, which three other callers depend on for cursor advancement.
        outcome: dict = {}
        try:
            _backfill_station_day_generic(
                sid,
                gap.file_date,
                gap.file_date,  # backfill_end = this day → marks the day complete
                session,
                immediate_archive=True,
                run_rinex=run_rinex,
                outcome=outcome,
            )
            if outcome.get("status") == "unreachable":
                report.unreachable_slots += 1
                remaining = len(queued) - i - 1
                logger.info(
                    "Long-term backfill %s/%s: unreachable at %s — abandoning "
                    "%d remaining slot(s) this run",
                    sid,
                    session,
                    gap.file_date,
                    remaining,
                )
                if budget is not None:
                    budget.give_back(remaining)
                break
            recovered += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning(
                "Long-term backfill %s/%s/%s failed: %s", sid, session, gap.file_date, e
            )
    logger.info(
        "Long-term backfill %s/%s done: recovered=%d failed=%d%s",
        sid,
        session,
        recovered,
        failed,
        f" unreachable={report.unreachable_slots}" if report.unreachable_slots else "",
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
    max_run_seconds: Optional[float] = DEFAULT_MAX_RUN_SECONDS,
    max_slots_per_run: Optional[int] = DEFAULT_MAX_SLOTS_PER_RUN,
) -> None:
    """APScheduler **daily backstop**: classify every active station, recover gaps.

    Throttled by a shared :class:`RunBudget` (wall-clock + slots) and a
    per-station offline short-circuit. The ``_load_monitor_overloaded()`` yield
    below is **dead code** while ``load_monitoring`` is disabled — see the S3
    note at the top of this module; the budget is what actually bounds a run.

    The worker is idempotent (the classifier reads ``archive_catalog``, so a
    recovered day comes back ``already_ok``), so a daily run is safe.
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
    budget = RunBudget(max_seconds=max_run_seconds, max_slots=max_slots_per_run)
    logger.info(
        "Long-term backfill(daily): %d stations, sessions=%s, lookback=%dd, %s",
        len(active),
        sessions,
        lookback_days,
        budget.describe(),
    )

    served = 0
    served_lock = threading.Lock()

    def _one(sid: str, session: str):
        nonlocal served
        if budget.exhausted():
            return None
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
                budget=budget,
            )
            # "Served" = consumed budget. A station that classified clean, was
            # skipped offline, or was capped to zero did not have its turn and
            # must not move the cursor past the stations behind it.
            if report.queued and not report.skipped_offline:
                with served_lock:
                    served += 1
            return len(report.queued)
        except Exception as e:  # noqa: BLE001
            logger.warning("Long-term backfill %s/%s: %s", sid, session, e)
            return None

    # Rotate the start point: the budget is measured to bind at the deployed
    # config, so a stable order would strand everything past the cut forever.
    tasks = _rotate([(sid, s) for sid in active for s in sessions])
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = [
            f.result()
            for f in as_completed(ex.submit(_one, sid, s) for sid, s in tasks)
        ]
    _advance_rotation(served, len(tasks))
    logger.info(
        "Long-term backfill(daily) done: %d station/session had queued gaps, %s%s",
        sum(1 for r in results if r),
        budget.describe(),
        (
            f" — STOPPED EARLY: {budget.exhausted_reason}"
            if budget.exhausted_reason
            else ""
        ),
    )


def _run_reconnection_backfill_job(
    min_outage_days: int = 3,
    lookback_days: int = 365,
    run_rinex: bool = True,
    max_days_per_station: Optional[int] = None,
    reconnection_window_minutes: int = 20,
    settle_hours: int = DEFAULT_SETTLE_HOURS,
    max_run_seconds: Optional[float] = DEFAULT_RECONNECT_MAX_RUN_SECONDS,
    max_slots_per_run: Optional[int] = DEFAULT_MAX_SLOTS_PER_RUN,
    reattempt_cooldown_minutes: int = DEFAULT_REATTEMPT_COOLDOWN_MINUTES,
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
    budget = RunBudget(max_seconds=max_run_seconds, max_slots=max_slots_per_run)
    for sid in candidates:
        if budget.exhausted():
            logger.info(
                "Long-term backfill(reconnect): %s — deferring remaining station(s)",
                budget.exhausted_reason,
            )
            break
        if _load_monitor_overloaded():
            logger.info(
                "Long-term backfill(reconnect): load high — deferring remaining"
            )
            break
        if _recently_attempted(sid, reattempt_cooldown_minutes):
            logger.debug(
                "Long-term backfill(reconnect): %s attempted < %dm ago — skipping",
                sid,
                reattempt_cooldown_minutes,
            )
            continue
        try:
            report = query_long_term_gaps(
                sid, "15s_24hr", lookback_days=lookback_days, settle_hours=settle_hours
            )
            # only act on a real outage: a queued day older than min_outage_days,
            # so a brief flap doesn't trigger a heavy multi-month recovery.
            if report.queued and any(g.file_date <= floor for g in report.queued):
                _mark_attempted(sid)
                run_long_term_backfill_station(
                    sid,
                    "15s_24hr",
                    lookback_days=lookback_days,
                    run_rinex=run_rinex,
                    max_days=max_days_per_station,
                    settle_hours=settle_hours,
                    budget=budget,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Long-term backfill(reconnect) %s: %s", sid, e)
