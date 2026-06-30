"""Reactive TOS-fingerprint diff for EPOS dissemination (T6, detection half).

The dissemination layer must react to TOS changes with no manual intervention:
a daily scan compares each station's current TOS state to the last-seen state and
classifies what changed, so the acting layer can re-ETL / re-disseminate / stop
exactly the affected stations.

This module is the *detection* half — a persistent per-station fingerprint store
and the on/off/changed state machine. It is pure and offline-testable (the TOS
read is an injected ``fingerprint_fn``). The acting half (retroactive header
re-push over the affected date range, ``in_epos`` on→backfill / off→stop-only)
consumes :class:`StationChange` records.

Fingerprint semantics:
- The fingerprint is :func:`tos_access.session_fingerprint` over the *current*
  device session — it already covers exactly the header-affecting fields
  (marker / domes / receiver / antenna / radome), so a change here is precisely a
  change that alters the disseminated RINEX header.
- ``in_epos`` is the station's EPOS-eligibility flag (TOS ``in_network_epos`` +
  minimum requirements), tracked separately so on/off transitions are explicit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("receivers.dissemination.reactive")

DEFAULT_STATE_PATH = "~/.cache/gps_receivers/epos_reactive_state.json"


@dataclass(frozen=True)
class StationState:
    """The last-seen reactive state of one station."""

    fingerprint: str
    in_epos: bool


# Change kinds (the state-machine outcomes).
NEW = "new"  # first time we see an EPOS-eligible station
CHANGED = "changed"  # header-affecting TOS change while in EPOS
ACTIVATED = "activated"  # in_epos off → on  ⇒ full backfill
DEACTIVATED = "deactivated"  # in_epos on → off ⇒ stop-only (no purge)
UNCHANGED = "unchanged"


@dataclass(frozen=True)
class StationChange:
    """One station's classification for this scan."""

    station: str
    kind: str
    old: Optional[StationState]
    new: Optional[StationState]

    @property
    def actionable(self) -> bool:
        return self.kind in (NEW, CHANGED, ACTIVATED, DEACTIVATED)


def classify(
    station: str,
    prev: Optional[StationState],
    cur: Optional[StationState],
) -> StationChange:
    """Classify one station given its previous and current state.

    ``cur`` is None when the station is not currently EPOS-eligible *and* we have
    no current fingerprint for it. The transitions:

    - no prev, cur in_epos        → NEW (backfill from install)
    - prev in_epos, cur not       → DEACTIVATED (stop-only, keep rows)
    - not prev in_epos, cur is    → ACTIVATED (backfill)
    - both in_epos, fp differs     → CHANGED (retro re-push affected range)
    - both in_epos, fp same        → UNCHANGED
    - neither in_epos              → UNCHANGED (nothing to do)
    """
    prev_in = bool(prev and prev.in_epos)
    cur_in = bool(cur and cur.in_epos)

    if not prev_in and cur_in:
        return StationChange(station, NEW if prev is None else ACTIVATED, prev, cur)
    if prev_in and not cur_in:
        return StationChange(station, DEACTIVATED, prev, cur)
    if prev_in and cur_in:
        assert prev is not None and cur is not None
        if prev.fingerprint != cur.fingerprint:
            return StationChange(station, CHANGED, prev, cur)
        return StationChange(station, UNCHANGED, prev, cur)
    return StationChange(station, UNCHANGED, prev, cur)


class FingerprintStore:
    """JSON-file persistence of ``{station: StationState}``.

    A flat file (not a DB table) keeps T6 self-contained; it can graduate to a
    table later without changing the diff/state-machine. Writes are atomic
    (temp + replace) so a crash mid-write can't corrupt the store.
    """

    def __init__(self, path: str | Path = DEFAULT_STATE_PATH):
        self.path = Path(path).expanduser()

    def load(self) -> dict[str, StationState]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("reactive state unreadable (%s) — treating as empty", exc)
            return {}
        return {
            sid: StationState(
                fingerprint=str(d.get("fingerprint", "")),
                in_epos=bool(d.get("in_epos", False)),
            )
            for sid, d in raw.items()
        }

    def save(self, states: dict[str, StationState]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({s: asdict(st) for s, st in states.items()}, indent=2)
        )
        tmp.replace(self.path)


def make_fingerprint_fn(
    session_provider: Optional[Callable[..., Optional[dict]]],
    epos_markers_set: set[str],
    *,
    at: Optional[datetime] = None,
) -> Callable[[str], Optional[StationState]]:
    """Build the production ``station → StationState`` reader from TOS.

    The fingerprint is :func:`tos_access.session_fingerprint` over the station's
    *current* device session (the one covering ``at``, default now); ``in_epos`` is
    membership in the EPOS-eligible marker set. A station with no current session
    gets an empty fingerprint (still tracked, so an install later reads as CHANGED).
    """
    from .tos_access import session_fingerprint

    when = at or datetime.now()

    def fn(station: str) -> Optional[StationState]:
        session = session_provider(station.upper(), when) if session_provider else None
        return StationState(
            fingerprint=session_fingerprint(session),
            in_epos=station.upper() in epos_markers_set,
        )

    return fn


def scan(
    markers: list[str],
    fingerprint_fn: Callable[[str], Optional[StationState]],
    store: FingerprintStore,
) -> list[StationChange]:
    """Classify every station in ``markers`` against the persisted store.

    ``fingerprint_fn(station)`` returns the current :class:`StationState` (or None
    when TOS gives nothing). Does NOT persist — the caller advances the store only
    after the acting layer succeeds, so a failed action is retried next scan.
    """
    prev = store.load()
    changes: list[StationChange] = []
    for sid in markers:
        cur = fingerprint_fn(sid)
        changes.append(classify(sid, prev.get(sid), cur))
    return changes


@dataclass
class ReactiveActions:
    """The side-effecting callbacks the orchestrator dispatches (injectable).

    Production wires these to the metadata ETL, the dissemination sweep over the
    affected range, and site-log generation; tests pass fakes. Each returns/raises
    so the orchestrator can mark a station succeeded (→ advance the store) only when
    its whole action chain worked.
    """

    refresh_metadata: Callable[[str], None]
    """Re-run the TOS→EPOS station ETL for a (re)activated / changed station."""
    disseminate: Callable[[StationChange], bool]
    """Re-disseminate the affected range; True on success. NEW/ACTIVATED ⇒ backfill
    from install, CHANGED ⇒ the header-affected range (cache auto-invalidates)."""
    regenerate_sitelog: Callable[[str], None]
    """Regenerate + (later) submit the IGS/M3G site log."""
    stop: Callable[[str], None]
    """Stop-only on DEACTIVATED — mark inactive, never purge EPOS rows."""


def run_reactive_sync(
    markers: list[str],
    fingerprint_fn: Callable[[str], Optional[StationState]],
    store: FingerprintStore,
    actions: ReactiveActions,
) -> dict[str, int]:
    """Scan → act on each changed station → advance the store. Never raises out.

    A station advances in the store only when its full action chain succeeds, so a
    transient failure is retried on the next scan rather than silently lost.
    """
    changes = scan(markers, fingerprint_fn, store)
    summary = {
        NEW: 0,
        CHANGED: 0,
        ACTIVATED: 0,
        DEACTIVATED: 0,
        UNCHANGED: 0,
        "failed": 0,
    }
    succeeded: set[str] = set()
    for ch in changes:
        summary[ch.kind] = summary.get(ch.kind, 0) + 1
        if not ch.actionable:
            continue
        try:
            if ch.kind == DEACTIVATED:
                actions.stop(ch.station)
            else:
                actions.refresh_metadata(ch.station)
                if not actions.disseminate(ch):
                    raise RuntimeError("dissemination reported failure")
                actions.regenerate_sitelog(ch.station)
            succeeded.add(ch.station)
        except Exception:
            logger.exception("reactive: action failed for %s (%s)", ch.station, ch.kind)
            summary["failed"] += 1
    advance(store, changes, succeeded)
    logger.info(
        "reactive sweep: new=%d changed=%d activated=%d deactivated=%d "
        "unchanged=%d failed=%d",
        summary[NEW],
        summary[CHANGED],
        summary[ACTIVATED],
        summary[DEACTIVATED],
        summary[UNCHANGED],
        summary["failed"],
    )
    return summary


def advance(
    store: FingerprintStore,
    changes: list[StationChange],
    succeeded: set[str],
) -> None:
    """Persist the new state for stations whose action succeeded.

    Only ``succeeded`` stations advance; everything else keeps its old state so a
    transient TOS/convert failure is re-detected and retried on the next scan.
    UNCHANGED stations are always carried forward (no action needed).
    """
    states = store.load()
    for ch in changes:
        if ch.kind == UNCHANGED and ch.new is not None:
            states[ch.station] = ch.new
        elif ch.station in succeeded and ch.new is not None:
            states[ch.station] = ch.new
        elif ch.kind == DEACTIVATED and ch.station in succeeded:
            # record the off state (keep rows; just stop pushing)
            states[ch.station] = ch.new or StationState("", False)
    store.save(states)
