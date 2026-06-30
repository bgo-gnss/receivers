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
- The fingerprint is :func:`tos_access.history_fingerprint` over the *whole* device
  history — every session's header-affecting fields (marker / domes / receiver /
  antenna / radome) plus its period dates. So a change anywhere, including a
  retroactive correction to a closed historical session, alters the fingerprint and
  is detected as CHANGED. (Current-session-only detection used to miss closed-period
  edits, so their historical files were never re-pushed.) The per-component
  ``components`` (current period only) still bound the re-disseminate *range*; a
  purely historical change leaves them unchanged ⇒ full-window re-push, cache-gated.
- ``in_epos`` is the station's EPOS-eligibility flag (TOS ``in_network_epos`` +
  minimum requirements), tracked separately so on/off transitions are explicit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("receivers.dissemination.reactive")

DEFAULT_STATE_PATH = "~/.cache/gps_receivers/epos_reactive_state.json"


@dataclass(frozen=True)
class StationState:
    """The last-seen reactive state of one station."""

    fingerprint: str
    in_epos: bool
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Per-component ``{name: {"fp", "since"}}`` (marker + each header device), for
    the exact-affected-range diff. Empty when no components reader is wired (the
    range then falls back to the full backfill window). Not part of change
    *detection* — :func:`classify` uses ``fingerprint`` only."""


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


# Device components are period-scoped: a change to one affects only that device's
# current period. ``marker`` (station scope) affects the whole history.
_DEVICE_COMPONENTS = ("gnss_receiver", "antenna", "radome")


def affected_floor(change: StationChange) -> Optional[date]:
    """Earliest date a CHANGED station's metadata change affects — or None.

    None means "no usable bound, re-disseminate the full backfill window" — used for
    NEW/ACTIVATED (backfill from install), a station-scope marker/domes change (the
    whole history shifts), or when component data is missing.

    Otherwise diffs the old vs new per-component fingerprints: a device whose
    fingerprint changed affects only its current period, so the floor is that
    device's ``since`` date; with several devices changed it is the earliest
    ``since``. The convert-cache still re-renders only the dates that actually
    differ — this just stops the sweep from iterating dates that *cannot* have
    changed, and never narrows below a real change (a changed device with no known
    ``since`` falls back to the full window)."""
    if change.kind != CHANGED or change.old is None or change.new is None:
        return None
    old_c = change.old.components or {}
    new_c = change.new.components or {}
    if not new_c:
        return None
    if old_c.get("marker", {}).get("fp") != new_c.get("marker", {}).get("fp"):
        return None
    sinces: list[date] = []
    for key in _DEVICE_COMPONENTS:
        if old_c.get(key, {}).get("fp") == new_c.get(key, {}).get("fp"):
            continue
        since = new_c.get(key, {}).get("since")
        if not since:
            return None  # changed device with no known period ⇒ no safe bound
        try:
            sinces.append(date.fromisoformat(since))
        except ValueError:
            return None
    return min(sinces) if sinces else None


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
                components=d.get("components") or {},
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
    history_fn: Optional[Callable[..., str]],
    epos_markers_set: set[str],
    *,
    at: Optional[datetime] = None,
    components_fn: Optional[Callable[..., dict[str, dict[str, Any]]]] = None,
) -> Callable[[str], Optional[StationState]]:
    """Build the production ``station → StationState`` reader from TOS.

    The fingerprint is :func:`tos_access.history_fingerprint` over the station's
    *whole* device history (not just the current session), so a retroactive TOS
    correction to a closed historical period is detected as CHANGED — see that
    function for the historical-session detection rationale. ``in_epos`` is
    membership in the EPOS-eligible marker set. A station with no history gets an
    empty fingerprint (still tracked, so an install later reads as CHANGED).

    ``components_fn`` (``station, when -> components``, e.g.
    :func:`tos_access.make_components_fn`) supplies the per-component (current-period)
    state used by :func:`affected_floor` to bound a CHANGED station's re-disseminate
    range. When omitted, components are empty and the range falls back to the full
    window. (A purely historical change leaves the current-period components
    unchanged ⇒ ``affected_floor`` → None ⇒ full window, which is exactly what we
    want for a closed-period edit.)
    """
    when = at or datetime.now()

    def fn(station: str) -> Optional[StationState]:
        sid = station.upper()
        fingerprint = history_fn(sid, when) if history_fn else ""
        components = components_fn(sid, when) if components_fn else {}
        return StationState(
            fingerprint=fingerprint,
            in_epos=sid in epos_markers_set,
            components=components,
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
