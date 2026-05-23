"""Field-workflow orchestration: install / move / visit operations.

This module is the single source of truth for receiver field operations.
The ``receivers cfg`` CLI subcommands and the standalone ``field_visit.py``
script both call into these Python functions — no logic duplication.

Each operation combines:

* a TOS state change via :class:`tostools.api.tos_writer.TOSWriter`
  (join close+open for moves, vitjun create for visits), and
* an optional ``stations.cfg`` update for installs (the destination
  station gets new ``receiver_serial`` / ``receiver_type`` /
  ``receiver_firmware_version`` / ``rinex_config_valid_from``).

All three operations accept a ``date`` parameter that defaults to *now*
but accepts an arbitrary past date — field work happens first, computer
entry follows, sometimes days later.

The operations default to ``dry_run=True`` (same convention as
:class:`TOSWriter`). The CLI flips the default with ``--no-dry-run`` /
``--live`` after the operator has reviewed the dry-run output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from tostools.api.tos_writer import TOSWriter

logger = logging.getLogger(__name__)

# Default warehouse for retired devices — matches TOS station name + the
# memory note `reference_tos_warehouse_locations`.
DEFAULT_WAREHOUSE = "B9 - Kjallari - Jörð"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class OperationResult:
    """Summary returned by :func:`install_device` / :func:`move_device` /
    :func:`add_visit`.

    Attributes:
        operation: ``"install"``, ``"move"``, or ``"visit"``.
        station_id: 4-char marker the operation targets (or for move,
            the device's source station if any).
        serial: Device serial number, when applicable.
        date: ISO date string used for the operation.
        tos_changes: Per-step TOS responses keyed by step name. Values
            are :class:`tostools.api.tos_writer.DryRunResult` in dry-run
            mode.
        cfg_changes: Map of ``stations.cfg`` keys that were updated to
            their new values. Empty for move/visit and dry-run.
        vitjun_id: ``id_maintenance`` of any vitjun created, or
            ``"<dry-run>"`` when dry-run skipped the real POST.
        dry_run: Whether the operation was a dry run.
    """

    operation: str
    station_id: Optional[str] = None
    serial: Optional[str] = None
    date: Optional[str] = None
    tos_changes: Dict[str, Any] = field(default_factory=dict)
    cfg_changes: Dict[str, str] = field(default_factory=dict)
    vitjun_id: Optional[Union[int, str]] = None
    dry_run: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CfgOperationError(RuntimeError):
    """Raised by operations when a precondition cannot be satisfied."""


def _visit_default_time(date_arg: Optional[str]) -> str:
    """Resolve a ``date`` arg to an ISO datetime.

    Two distinct operator intents map to two different defaults:

      * ``None`` (operator typed no ``--date`` flag) → **right now**
        (current timestamp, seconds precision). Means "I'm entering
        this at the moment the field event is happening."
      * Bare ``"YYYY-MM-DD"`` (operator typed a date but no time) →
        ``"YYYY-MM-DDT12:00:00"`` (noon). Means "this happened on
        that day during the workday" — backdated entry.
      * Any string containing ``'T'`` (full ISO datetime) → preserved.
        Means "this happened at this specific moment" (e.g. the
        ``T23:00:00`` HRAC swap).

    Used for both the join transition_date and the vitjun start_time
    inside :func:`move_device`, so a single resolved value flows
    through every TOS write per invocation.
    """
    if date_arg is None:
        return datetime.now().replace(microsecond=0).isoformat()
    if "T" not in date_arg:
        return f"{date_arg}T12:00:00"
    return date_arg


def _resolve_writer(writer: Optional[TOSWriter], dry_run: bool) -> TOSWriter:
    """Return the caller's writer or build a default one in the requested
    dry-run mode. The caller still owns the writer's lifecycle when they
    pass one in."""
    if writer is not None:
        return writer
    return TOSWriter(dry_run=dry_run)


def _resolve_cfg_path(cfg_path: Optional[Path]) -> Path:
    """Locate ``stations.cfg`` for write operations.

    Order:
        1. Caller-supplied ``cfg_path`` (CLI ``--cfg-path``).
        2. ``GPS_CONFIG_DATA_REPO`` env var → ``$REPO/stations.cfg``
           when the file exists (the gps-config-data source-of-truth
           clone).
        3. :func:`gps_parser.ConfigParser.get_stations_config_path` —
           the runtime-deployed copy (usually ``~/.config/gpsconfig/``).
    """
    if cfg_path is not None:
        return cfg_path

    import os

    repo = os.environ.get("GPS_CONFIG_DATA_REPO")
    if repo:
        candidate = Path(repo).expanduser() / "stations.cfg"
        if candidate.exists():
            return candidate

    try:
        import gps_parser as _gps  # type: ignore
    except ImportError as exc:
        raise CfgOperationError(
            "gps_parser not importable and no cfg_path / "
            "GPS_CONFIG_DATA_REPO given — cannot locate stations.cfg"
        ) from exc
    return Path(_gps.ConfigParser().get_stations_config_path())


def _resolve_station(writer: TOSWriter, station_id: str) -> int:
    """Resolve a 4-char marker to a TOS station ``id_entity``."""
    eid = writer.find_station_by_marker(station_id)
    if eid is None:
        raise CfgOperationError(
            f"No TOS station matches marker {station_id!r}. "
            f"Check spelling or the station's marker attribute in TOS."
        )
    return eid


def _find_open_gnss_receiver_child(
    writer: TOSWriter, station_eid: int
) -> Optional[int]:
    """Return the id_entity of the open gnss_receiver child of a station.

    Used by :func:`install_device` for the destination-displacement
    constraint: a station should have at most one open receiver join at
    a time. If one exists, the operator must retire/transfer it before
    installing a new one.

    Returns ``None`` when no open receiver join exists at the station.
    """
    history = writer.get_entity_history(station_eid)
    if not isinstance(history, dict):
        return None
    children = history.get("children_connections") or []
    open_children = [c for c in children if c.get("time_to") is None]
    for child in open_children:
        cid = child.get("id_entity_child")
        if cid is None:
            continue
        child_hist = writer.get_entity_history(int(cid))
        if (
            isinstance(child_hist, dict)
            and child_hist.get("code_entity_subtype") == "gnss_receiver"
        ):
            return int(cid)
    return None


def _device_attribute(device_hist: Dict[str, Any], code: str) -> Optional[str]:
    """Pluck the currently-open value of one attribute from a device payload.

    TOS returns ``attributes`` as a flat denormalised list — each item IS
    an attribute_value row (not a wrapper). Multiple rows with the same
    ``code`` are temporal periods. Prefer the row where ``date_to is None``
    (the open period); fall back to the most recent ``date_from`` if none
    is open.
    """
    candidates = [
        a for a in (device_hist.get("attributes") or [])
        if a.get("code") == code
    ]
    if not candidates:
        return None
    open_rows = [a for a in candidates if a.get("date_to") is None]
    pool = open_rows or candidates
    latest = max(pool, key=lambda a: a.get("date_from") or "")
    return latest.get("value")


def _canonical_receiver_type(igs_name: Optional[str]) -> Optional[str]:
    """Map a TOS IGS-style receiver name to the stations.cfg short form.

    e.g. ``"SEPT POLARX5"`` → ``"PolaRX5"``. Returns the input
    unchanged if no canonical mapping is known, so unfamiliar models
    aren't silently corrupted.
    """
    if not igs_name:
        return igs_name
    try:
        from ..health.receiver_fingerprint import identify_receiver_type
    except ImportError:
        return igs_name
    canonical = identify_receiver_type({"receiver_model": igs_name})
    return canonical if canonical is not None else igs_name


def _apply_cfg_updates(
    cfg_path: Path,
    station_id: str,
    updates: Dict[str, Optional[str]],
) -> Dict[str, str]:
    """Apply a batch of ``key=value`` updates to one station section.

    Returns the subset of ``updates`` that actually changed the file
    (skipped keys had a matching value already). ``None`` values in
    ``updates`` are skipped entirely.
    """
    from ..config.receivers_config import _update_cfg_field

    applied: Dict[str, str] = {}
    for key, value in updates.items():
        if value is None:
            continue
        if _update_cfg_field(cfg_path, station_id, key, value):
            applied[key] = value
    return applied


def _find_receiver_at_station(
    writer: TOSWriter,
    station_eid: int,
) -> Optional[int]:
    """Find the gnss_receiver associated with a station for ``--serial``
    inference.

    Used by :func:`move_device` when the caller passes ``--from-station``
    instead of ``--serial``. The user's mental model is usually "the
    receiver at SAVI" — either currently physically there (open join)
    or recently closed off (still nearby, e.g. just brought to the
    workshop).

    Resolution order:

    1. The **currently open** gnss_receiver child of ``station_eid``.
       Covers the "move what's there" workflow ("the receiver at SAVI
       is broken, send it to B9").
    2. The **most recently closed** gnss_receiver child. Covers the
       transfer case ("move the receiver that just came off HRAC to
       SAVI") *only while HRAC has no fresh open receiver*. If HRAC
       has already been refilled, this picks up the new device — pass
       ``--serial`` explicitly in that situation.

    Returns the device's ``id_entity`` or None.
    """
    history = writer.get_entity_history(station_eid)
    if not isinstance(history, dict):
        return None
    children = history.get("children_connections") or []

    # Prefer the currently-open receiver.
    for c in children:
        if c.get("time_to") is not None:
            continue
        cid = c.get("id_entity_child")
        if cid is None:
            continue
        chist = writer.get_entity_history(int(cid))
        if (
            isinstance(chist, dict)
            and chist.get("code_entity_subtype") == "gnss_receiver"
        ):
            return int(cid)

    # Fall back to most recently closed.
    closed = [c for c in children if c.get("time_to") is not None]
    closed.sort(key=lambda c: c.get("time_to") or "", reverse=True)
    for c in closed:
        cid = c.get("id_entity_child")
        if cid is None:
            continue
        chist = writer.get_entity_history(int(cid))
        if (
            isinstance(chist, dict)
            and chist.get("code_entity_subtype") == "gnss_receiver"
        ):
            return int(cid)
    return None


def _find_recently_left_receiver(
    writer: TOSWriter,
    station_eid: int,
    on_or_before: Optional[str] = None,
) -> Optional[int]:
    """Find the gnss_receiver that left ``station_eid`` at or just before
    ``on_or_before``. Returns the device's ``id_entity`` or None.

    Used for two purposes:

    * Auto-vitjun text on a station install — pass the install date
      as ``on_or_before`` so the helper picks the receiver whose join
      was just closed by a prior :func:`move_device`.
    * ``--serial`` inference when only ``--from-station`` is given —
      pass ``None`` to find the absolute most recent closed join.
    """
    history = writer.get_entity_history(station_eid)
    if not isinstance(history, dict):
        return None
    cap = on_or_before
    candidates = [
        c for c in (history.get("children_connections") or [])
        if c.get("time_to") is not None
        and (cap is None or c.get("time_to") <= cap)
    ]
    candidates.sort(key=lambda c: c.get("time_to") or "", reverse=True)
    for c in candidates:
        cid = c.get("id_entity_child")
        if cid is None:
            continue
        child_hist = writer.get_entity_history(int(cid))
        if (
            isinstance(child_hist, dict)
            and child_hist.get("code_entity_subtype") == "gnss_receiver"
        ):
            return int(cid)
    return None


def _auto_vitjun_text(
    writer: TOSWriter,
    station_eid: int,
    new_device: Dict[str, Any],
    transition_date: str,
    from_station: Optional[str] = None,
) -> str:
    """Build a default vitjun work text from the operation context.

    Derives "old" from the receiver that left ``station_eid`` at or
    just before ``transition_date`` (closed children_connections);
    "new" from the device payload's attributes. Produces:

      - ``"Skipt um móttakara: <old> → <new>"`` for a swap
      - ``"Móttakari fluttur frá <from_station>: <new> (skipt um <old>)"`` for a transfer
      - ``"Móttekinn móttakari frá <from_station>: <new>"`` for transfer with empty dest
      - ``"Settur upp móttakari: <new>"`` for a fresh deploy
    """
    new_serial = _device_attribute(new_device, "serial_number") or "?"
    new_model = _canonical_receiver_type(
        _device_attribute(new_device, "model")
    ) or "?"
    new_label = f"{new_model} {new_serial}".strip()

    old_id = _find_recently_left_receiver(
        writer, station_eid, transition_date
    )
    if old_id is None:
        if from_station:
            return f"Móttekinn móttakari frá {from_station}: {new_label}"
        return f"Settur upp móttakari: {new_label}"

    old_device = writer.get_entity_history(old_id)
    if not isinstance(old_device, dict):
        return f"Settur upp móttakari: {new_label}"

    old_serial = _device_attribute(old_device, "serial_number") or "?"
    old_model = _canonical_receiver_type(
        _device_attribute(old_device, "model")
    ) or "?"
    old_label = f"{old_model} {old_serial}".strip()

    if from_station:
        return (
            f"Móttakari fluttur frá {from_station}: {new_label} "
            f"(skipt um {old_label})"
        )
    return f"Skipt um móttakara: {old_label} → {new_label}"


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def _default_rinex_valid_from(install_iso: str) -> str:
    """Compute the default rinex_config_valid_from date from an install dt.

    Convention: stations.cfg ``rinex_config_valid_from`` is the *first
    full day* of the new equipment configuration. If install happened
    exactly at midnight, that day is fully under the new config
    → same date. If install happened later in the day (e.g. 23:00),
    that day is *split* between old and new equipment, so the first
    full day is the next one.

    Args:
        install_iso: ISO datetime string the install happened at.

    Returns:
        ``YYYY-MM-DD`` date string for ``rinex_config_valid_from``.
    """
    from datetime import datetime as _dt, timedelta as _td
    try:
        dt = _dt.fromisoformat(install_iso)
    except ValueError:
        # Bare YYYY-MM-DD already — treat as midnight, return as-is
        return install_iso.split("T", 1)[0]
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return dt.date().isoformat()
    return (dt.date() + _td(days=1)).isoformat()


def move_device(
    serial: Optional[str] = None,
    *,
    to: str = DEFAULT_WAREHOUSE,
    date: Optional[str] = None,
    from_station: Optional[str] = None,
    firmware: Optional[str] = None,
    rinex_valid_from: Optional[str] = None,
    vitjun: Optional[str] = None,
    vitjun_remaining: Optional[str] = None,
    participants: str = "",
    device_status: Optional[str] = None,
    device_comment: Optional[str] = None,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
    cfg_path: Optional[Path] = None,
    skip_vitjun: bool = False,
    skip_cfg: bool = False,
    _assume_cleared_device_id: Optional[int] = None,
) -> OperationResult:
    """Move a receiver to a new parent — station OR warehouse.

    Auto-detects ``to`` by type:

    * **Station marker** (4-char, e.g. ``"HRAC"``) — runs the full
      install workflow:

      1. Destination-displacement check: refuse if the station already
         has an open ``gnss_receiver`` child. Move the old one out first.
      2. TOS Pattern 2 move: close the device's current parent join at
         ``date``, open a new join to the station at the same date.
      3. Vitjun ("Breyting") on the destination station with auto-text
         derived from the receiver that just left (``Skipt um móttakara:
         <old> → <new>``); override via ``vitjun``.
      4. Update ``stations.cfg`` (``receiver_serial`` / ``receiver_type``
         / ``receiver_firmware_version`` / ``rinex_config_valid_from``)
         from the device's TOS attributes.

    * **Location name** (e.g. ``"B9 - Kjallari - Jörð"``, default) —
      bookkeeping-only move:

      1. TOS Pattern 2 move to the warehouse.
      2. Optional vitjun on the *source* station — only when ``vitjun``
         is given (no default text).
      3. No ``stations.cfg`` update (the source station's
         ``receiver_*`` fields will be overwritten by the next station
         move into it, or hand-edited if it's being decommissioned).

    Args:
        serial: Device serial number (must exist in TOS — warehouse new
            arrivals via ``receivers cfg add-receiver`` first).
        to: Destination — a 4-char station marker OR a location name
            as recorded in TOS. Defaults to the B9 warehouse.
        date: ISO date/datetime the move happened. Accepts
            ``YYYY-MM-DD`` (promoted to midnight). Default: today.
            Backdating freely supported.
        from_station: 4-char marker of the source station (transfer
            case). When given on a station→station transfer, sanity-
            checks the device is currently at this station.
        firmware: Optional override of the firmware string written to
            stations.cfg. Does not modify the TOS firmware_version
            attribute. Station destinations only.
        rinex_valid_from: Optional override of the
            ``rinex_config_valid_from`` cfg field (YYYY-MM-DD). Default:
            :func:`_default_rinex_valid_from` applied to ``date``.
            Station destinations only.
        vitjun: Free-text override for the vitjun "Framkvæmt" field.
            Default for station destinations: auto-derived from
            context. For location destinations: no vitjun unless this
            is set.
        vitjun_remaining: Optional "Útistandandi" text for the vitjun.
        participants: Comma-separated emails for the vitjun
            ``participants`` field.
        device_status: When given, runs Pattern-2 on the device's
            ``status`` attribute — closes the current open period at
            ``date`` and opens a new period with this value. Use to
            mark a unit broken (``"bilað"``) when moving it to a
            workshop, or active (``"virkt"``) when redeploying after
            repair. Old devices without an existing ``status`` get
            the value added (no close).
        device_comment: Same Pattern-2 transition for the device's
            ``comment`` attribute — preserves the old comment in
            history. Pass the full new comment text.
        dry_run: When True (default), TOS writes use
            :class:`DryRunResult` and stations.cfg is left alone.
        writer: Optional pre-built TOSWriter.
        cfg_path: Override the stations.cfg location.
        skip_vitjun: When True, skip the vitjun step entirely.
        skip_cfg: When True, skip the stations.cfg update (station
            destinations only).

    Returns:
        :class:`OperationResult`.

    Raises:
        CfgOperationError: When ``to`` resolves to neither a station
            marker nor a warehouse, when the destination station has
            an open receiver child, or when the serial is unknown.
    """
    w = _resolve_writer(writer, dry_run)
    # Default to noon (field-work convention): bare YYYY-MM-DD →
    # YYYY-MM-DDT12:00:00, None → today noon. Joins, attribute
    # transitions, and the vitjun all share this resolved timestamp
    # so a single --date applies consistently. Explicit
    # YYYY-MM-DDTHH:MM:SS lets the operator pin a specific time
    # (e.g. HRAC's swap at 23:00).
    eff_date = _visit_default_time(date)

    # If --serial omitted, infer from --from-station's most recently
    # closed gnss_receiver child. Workflow case: user removed a unit
    # from STATION_A on day 1 (it's now at B9), and the next day
    # transfers it to STATION_B without typing the serial — they only
    # remember "the receiver that came off STATION_A".
    if serial is None:
        if from_station is None:
            raise CfgOperationError(
                "move_device: --serial or --from-station is required."
            )
        from_eid_for_infer = _resolve_station(w, from_station)
        # Prefer currently-open receiver, fall back to most recently closed.
        inferred = _find_receiver_at_station(w, from_eid_for_infer)
        if inferred is None:
            raise CfgOperationError(
                f"--from-station {from_station}: no gnss_receiver is "
                f"currently joined to this station and none has ever "
                f"been closed off it — cannot infer --serial. Pass "
                f"--serial X explicitly or check the station marker."
            )
        device_hist = w.get_entity_history(inferred)
        inferred_serial = (
            _device_attribute(device_hist, "serial_number")
            if isinstance(device_hist, dict)
            else None
        )
        if not inferred_serial:
            raise CfgOperationError(
                f"--from-station {from_station}: most recent receiver to "
                f"leave (id_entity={inferred}) has no serial_number "
                f"attribute readable — pass --serial explicitly."
            )
        serial = inferred_serial
        logger.info(
            "move_device: inferred --serial %s from --from-station %s",
            serial,
            from_station,
        )

    # Auto-detect target type: station marker first, then location name.
    station_eid = w.find_station_by_marker(to)
    if station_eid is not None:
        return _move_to_station(
            w,
            serial=serial,
            station_id=to,
            station_eid=station_eid,
            eff_date=eff_date,
            from_station=from_station,
            firmware=firmware,
            rinex_valid_from=rinex_valid_from,
            vitjun=vitjun,
            vitjun_remaining=vitjun_remaining,
            participants=participants,
            device_status=device_status,
            device_comment=device_comment,
            dry_run=dry_run,
            cfg_path=cfg_path,
            skip_vitjun=skip_vitjun,
            skip_cfg=skip_cfg,
            assume_cleared_device_id=_assume_cleared_device_id,
        )

    location_eid = w.find_location_by_name(to, type_filter="vöruhús")
    if location_eid is not None:
        # Suppress the source-station cfg-clear when chained from
        # replace_receiver (its install-new step writes the new cfg).
        # Also suppress when the caller passed --no-cfg (umbrella).
        skip_clear = (
            skip_cfg
            or _assume_cleared_device_id is not None
        )
        return _move_to_location(
            w,
            serial=serial,
            location_name=to,
            location_eid=location_eid,
            eff_date=eff_date,
            vitjun=vitjun,
            vitjun_remaining=vitjun_remaining,
            participants=participants,
            device_status=device_status,
            device_comment=device_comment,
            dry_run=dry_run,
            skip_vitjun=skip_vitjun,
            cfg_path=cfg_path,
            skip_clear_cfg=skip_clear,
        )

    raise CfgOperationError(
        f"--to {to!r} resolves to neither a station marker (type 'stöð') "
        f"nor a warehouse (type 'vöruhús'). Check spelling, or use the "
        f"full TOS-recorded name for warehouses."
    )


def _move_to_station(
    w: TOSWriter,
    *,
    serial: str,
    station_id: str,
    station_eid: int,
    eff_date: str,
    from_station: Optional[str],
    firmware: Optional[str],
    rinex_valid_from: Optional[str],
    vitjun: Optional[str],
    vitjun_remaining: Optional[str],
    participants: str,
    device_status: Optional[str],
    device_comment: Optional[str],
    dry_run: bool,
    cfg_path: Optional[Path],
    skip_vitjun: bool,
    skip_cfg: bool,
    assume_cleared_device_id: Optional[int] = None,
) -> OperationResult:
    """Station-destination path of :func:`move_device`.

    ``assume_cleared_device_id`` is an internal escape hatch for
    chained orchestration (:func:`replace_receiver`): when the caller
    has just (or is about to) close an open receiver join at this
    station, pass the device id so the displacement check treats it
    as already-resolved. Lets dry-run mode preview the install step
    without falsely reporting a conflict.
    """
    open_existing = _find_open_gnss_receiver_child(w, station_eid)
    if open_existing is not None and open_existing != assume_cleared_device_id:
        raise CfgOperationError(
            f"{station_id} already has an open gnss_receiver child "
            f"(id_entity={open_existing}). Move the old receiver out "
            f"first: `receivers cfg move-device --serial <SERIAL>` "
            f"(defaults to B9 warehouse) or "
            f"`receivers cfg move-device --serial <SERIAL> --to <ELSE>`."
        )

    device = w.find_device_by_serial("gnss_receiver", serial)
    if device is None:
        raise CfgOperationError(
            f"No gnss_receiver in TOS with serial {serial!r}. "
            f"If this is a new unit, warehouse it first with "
            f"`receivers cfg add-receiver`."
        )
    device_id = int(device["id_entity"])
    new_model = _device_attribute(device, "model")
    new_firmware = _device_attribute(device, "firmware_version")

    # NB: --from-station is metadata at this layer (used for serial
    # inference and the auto-vitjun "from X" wording). We deliberately
    # do NOT pass it as from_id_entity to TOSWriter.move_device — that
    # would fail the in-transit case (receiver already at B9 between
    # the swap-out and swap-in). TOSWriter auto-detects the actual
    # current parent and closes that join correctly.
    move = w.move_device(device_id, station_eid, eff_date)

    result = OperationResult(
        operation="move",
        station_id=station_id,
        serial=serial,
        date=eff_date,
        tos_changes={"move": move},
        dry_run=dry_run,
    )

    if not skip_vitjun:
        work = vitjun or _auto_vitjun_text(
            w, station_eid, device, eff_date, from_station=from_station
        )
        vit = w.add_maintenance_visit(
            station_eid,
            start_time=eff_date,
            maintenance_type="on_site",
            participants=participants,
            reasons=["change"],
            work=work,
            remaining=vitjun_remaining,
        )
        result.tos_changes["vitjun"] = vit
        result.vitjun_id = vit.get("id_maintenance")

    _apply_device_attribute_transitions(
        w,
        device_id,
        eff_date,
        device_status=device_status,
        device_comment=device_comment,
        result=result,
    )

    if not skip_cfg and not dry_run:
        target_cfg = _resolve_cfg_path(cfg_path)
        cfg_updates: Dict[str, Optional[str]] = {
            "receiver_serial": serial,
            "receiver_type": _canonical_receiver_type(new_model),
            "receiver_firmware_version": firmware or new_firmware,
            "rinex_config_valid_from": (
                rinex_valid_from or _default_rinex_valid_from(eff_date)
            ),
        }
        result.cfg_changes = _apply_cfg_updates(
            target_cfg, station_id, cfg_updates
        )
    return result


def _move_to_location(
    w: TOSWriter,
    *,
    serial: str,
    location_name: str,
    location_eid: int,
    eff_date: str,
    vitjun: Optional[str],
    vitjun_remaining: Optional[str],
    participants: str,
    device_status: Optional[str],
    device_comment: Optional[str],
    dry_run: bool,
    skip_vitjun: bool,
    cfg_path: Optional[Path] = None,
    skip_clear_cfg: bool = False,
) -> OperationResult:
    """Location-destination (bookkeeping) path of :func:`move_device`."""
    device = w.find_device_by_serial("gnss_receiver", serial)
    if device is None:
        raise CfgOperationError(
            f"No gnss_receiver in TOS with serial {serial!r}."
        )
    device_id = int(device["id_entity"])

    open_join = w.get_open_parent_join(device_id)
    source_eid = open_join.get("id_entity_parent") if open_join else None

    move = w.move_device(device_id, location_eid, eff_date)

    result = OperationResult(
        operation="move",
        serial=serial,
        date=eff_date,
        tos_changes={"move": move, "to_location": location_name},
        dry_run=dry_run,
    )

    if not skip_vitjun and source_eid is not None and vitjun is not None:
        # Only write vitjun on a location move when caller explicitly
        # supplied --vitjun text (location moves don't auto-write).
        vit = w.add_maintenance_visit(
            int(source_eid),
            start_time=eff_date,
            maintenance_type="on_site",
            participants=participants,
            reasons=["change"],
            work=vitjun,
            remaining=vitjun_remaining,
        )
        result.tos_changes["vitjun"] = vit
        result.vitjun_id = vit.get("id_maintenance")

    _apply_device_attribute_transitions(
        w,
        device_id,
        eff_date,
        device_status=device_status,
        device_comment=device_comment,
        result=result,
    )

    # Auto-clear stations.cfg when the device just left a station with no
    # immediate replacement. Suppressed when chained from replace_receiver
    # (the install-new step will overwrite the cfg anyway).
    if (
        not skip_clear_cfg
        and not dry_run
        and source_eid is not None
        and source_eid != location_eid  # not a warehouse-to-warehouse move
    ):
        source_marker = _marker_for_entity(w, int(source_eid))
        if source_marker:
            target_cfg = _resolve_cfg_path(cfg_path)
            cleared = _clear_station_receiver_cfg(
                target_cfg, source_marker, eff_date
            )
            if cleared:
                result.cfg_changes = cleared

    return result


def _marker_for_entity(w: TOSWriter, eid: int) -> Optional[str]:
    """Look up the ``marker`` attribute value on a station entity.

    Returns the 4-char RINEX marker if present, else None (the entity
    isn't a station, or has no marker).
    """
    hist = w.get_entity_history(eid)
    if not isinstance(hist, dict):
        return None
    marker = _device_attribute(hist, "marker")
    return marker.upper() if marker else None


def _clear_station_receiver_cfg(
    cfg_path: Path,
    station_id: str,
    eff_date: str,
) -> Dict[str, str]:
    """Set the four receiver_* keys on a station section to the canonical
    "empty" sentinel so Grafana / scheduler auto-detection picks up the
    station as inactive.

    Uses ``NONE`` (uppercase) — matches the existing ``antenna_radome =
    NONE`` convention. The receivers scheduler's "None/empty/unknown"
    auto-inactive check (per receivers CLAUDE.md) accepts this.

    Returns the subset of fields that actually changed (skipping no-ops
    where the value was already NONE).
    """
    cfg_updates: Dict[str, Optional[str]] = {
        "receiver_type": "NONE",
        "receiver_serial": "NONE",
        "receiver_firmware_version": "NONE",
        "rinex_config_valid_from": eff_date.split("T", 1)[0],
    }
    return _apply_cfg_updates(cfg_path, station_id, cfg_updates)


def _apply_device_attribute_transitions(
    w: TOSWriter,
    device_id: int,
    eff_date: str,
    *,
    device_status: Optional[str],
    device_comment: Optional[str],
    result: OperationResult,
) -> None:
    """Apply optional Pattern-2 transitions on device attributes.

    Used after a move to record a status change (e.g. ``virkt`` →
    ``bilað`` when a broken unit goes to a workshop) and/or a comment
    update. The transition closes any existing open period at
    ``eff_date`` and opens a new one with the new value. When no open
    period exists (some older fleet devices have no ``status``
    attribute at all), the new value is simply added with the same
    date.

    Writes the responses into ``result.tos_changes[...]`` keys
    ``device_status`` / ``device_comment`` so the caller's
    OperationResult reflects the work.
    """
    if device_status is not None:
        result.tos_changes["device_status"] = w.transition_attribute_value(
            device_id, "status", device_status, eff_date
        )
    if device_comment is not None:
        result.tos_changes["device_comment"] = w.transition_attribute_value(
            device_id, "comment", device_comment, eff_date
        )


def delete_join(
    id_connection: int,
    *,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
) -> OperationResult:
    """Delete a single ``entity_connection`` row by id.

    Admin-level destructive operation — no undo on TOS. Use only to
    clean up known-bad rows such as zero-duration orphans left over
    from historical add-device workflows.

    To find the right id, query ``/entity/parent_history/{id_child}``
    and pick the row whose ``time_from == time_to`` (or whatever shape
    you've decided is junk). Never delete a row without inspecting it
    first.

    Args:
        id_connection: ``id`` of the join row to delete.
        dry_run: When True (default), logs the DELETE without sending.
        writer: Optional pre-built TOSWriter.

    Returns:
        :class:`OperationResult` with ``operation='delete-join'`` and
        ``tos_changes={'id_connection': N, 'deleted': <response>}``.
    """
    w = _resolve_writer(writer, dry_run)
    resp = w.delete_entity_connection(id_connection)
    return OperationResult(
        operation="delete-join",
        date=None,
        tos_changes={"id_connection": id_connection, "deleted": resp},
        dry_run=dry_run,
    )


def add_visit(
    station_id: str,
    *,
    work: str,
    date: Optional[str] = None,
    end_time: Optional[str] = None,
    maintenance_type: str = "on_site",
    reasons: Optional[List[str]] = None,
    comment: Optional[str] = None,
    remaining: Optional[str] = None,
    participants: str = "",
    completed: bool = True,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
) -> OperationResult:
    """Add a standalone vitjun on ``station_id`` — no equipment change.

    Wraps :meth:`TOSWriter.add_maintenance_visit` after resolving the
    station marker to an ``id_entity``. Used for maintenance visits
    that don't trigger a join change: antenna-cable repair, environment
    cleanup, remote configuration tweak, etc.

    Args:
        station_id: 4-char marker of the station visited.
        work: "Framkvæmt" / "Vinna" — what was done. Required (a vitjun
            without a work description is rarely useful).
        date: ISO start time. Default: today midnight.
        end_time: ISO end time. Default: same as ``date``.
        maintenance_type: ``"on_site"`` (Staðarvitjun) or ``"remote"``
            (Fjarvitjun).
        reasons: Subset of
            ``{"change", "repairs", "inspection", "improvements",
              "other"}``. Default: ``["repairs"]`` (Viðgerð).
        comment: "Athugasemdir".
        remaining: "Útistandandi".
        participants: Comma-separated emails.
        completed: Whether the visit is closed. Default True.
        dry_run / writer: As :func:`install_device`.

    Returns:
        :class:`OperationResult` with ``vitjun_id`` set on live writes
        (or ``"<dry-run>"`` in dry-run).
    """
    w = _resolve_writer(writer, dry_run)
    eff_date = _visit_default_time(date)
    eff_end = _visit_default_time(end_time) if end_time is not None else None

    station_eid = _resolve_station(w, station_id)

    vit = w.add_maintenance_visit(
        station_eid,
        start_time=eff_date,
        end_time=eff_end,
        maintenance_type=maintenance_type,
        participants=participants,
        reasons=reasons or ["repairs"],
        work=work,
        comment=comment,
        remaining=remaining,
        completed=completed,
    )
    return OperationResult(
        operation="visit",
        station_id=station_id,
        date=eff_date,
        tos_changes={"vitjun": vit},
        vitjun_id=vit.get("id_maintenance"),
        dry_run=dry_run,
    )


def show_visit(
    id_maintenance: int,
    *,
    writer: Optional[TOSWriter] = None,
) -> Dict[str, Any]:
    """Return the full detail of a single vitjun record.

    Read-only; no dry-run distinction. Returns the raw TOS dict from
    :meth:`TOSWriter.get_maintenance_visit`, which includes
    ``maintenance_attribute_values`` rows with their per-attribute IDs.

    Args:
        id_maintenance: ``id_maintenance`` of the visit to fetch.
        writer: Optional pre-built TOSWriter.

    Raises:
        CfgOperationError: If the id is unknown to TOS.
    """
    w = _resolve_writer(writer, dry_run=True)
    detail = w.get_maintenance_visit(id_maintenance)
    if not detail:
        raise CfgOperationError(
            f"No vitjun in TOS with id_maintenance={id_maintenance}."
        )
    return detail


def list_visits(
    station_id: str,
    *,
    writer: Optional[TOSWriter] = None,
) -> List[Dict[str, Any]]:
    """List all vitjun records on a station, oldest-first.

    Read-only; no dry-run distinction. Returns the flat web-UI shape
    used by :meth:`TOSWriter.list_maintenance_visits` (``id``,
    ``maintenance_type``, ``maintenance_type_is``, ``reason``,
    ``start_time``, ``end_time``, ``participants``,
    ``participants_names``, ``work``, ``remaining``, ``completed``).

    Args:
        station_id: 4-char RINEX marker.
        writer: Optional pre-built TOSWriter.

    Raises:
        CfgOperationError: When the station marker doesn't resolve.
    """
    w = _resolve_writer(writer, dry_run=True)
    station_eid = _resolve_station(w, station_id)
    return w.list_maintenance_visits(station_eid)


def update_visit(
    id_maintenance: int,
    *,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    participants: Optional[str] = None,
    completed: Optional[bool] = None,
    reasons: Optional[List[str]] = None,
    work: Optional[str] = None,
    comment: Optional[str] = None,
    remaining: Optional[str] = None,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
) -> OperationResult:
    """Edit an existing vitjun in place; preserve fields you don't pass.

    Wraps :meth:`TOSWriter.update_maintenance_visit`. Any argument
    left as ``None`` keeps the current TOS value; an explicit empty
    string (``""``) clears the field.

    Args:
        id_maintenance: ``id_maintenance`` of the visit to edit.
        start_time / end_time / participants / completed / reasons /
        work / comment / remaining: New values; ``None`` preserves.
            ``reasons`` is a *replacement* set (passing it overwrites
            all reason booleans, not just one).
        dry_run / writer: As :func:`move_device`.

    Returns:
        :class:`OperationResult` with ``vitjun_id=id_maintenance`` and
        ``tos_changes={'update': <writer_response>}``.
    """
    w = _resolve_writer(writer, dry_run)
    # Promote bare YYYY-MM-DD dates to noon to match the create-mode
    # convention (a visit happens during the workday, not at midnight).
    norm_start = (
        _visit_default_time(start_time) if start_time is not None else None
    )
    norm_end = (
        _visit_default_time(end_time) if end_time is not None else None
    )
    resp = w.update_maintenance_visit(
        id_maintenance,
        start_time=norm_start,
        end_time=norm_end,
        participants=participants,
        completed=completed,
        reasons=reasons,
        work=work,
        comment=comment,
        remaining=remaining,
    )
    return OperationResult(
        operation="visit-edit",
        date=None,
        tos_changes={"update": resp},
        vitjun_id=id_maintenance,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# replace_receiver — one-shot warehouse + retire + install
# ---------------------------------------------------------------------------


REPLACE_STEPS = ("warehouse", "move-old", "install-new")


def _b9_eid(w: TOSWriter) -> int:
    """Resolve the B9 warehouse id_entity once per replace operation."""
    eid = w.find_location_by_name(DEFAULT_WAREHOUSE, type_filter="vöruhús")
    if eid is None:
        raise CfgOperationError(
            f"Could not resolve the B9 warehouse ({DEFAULT_WAREHOUSE!r}) "
            "in TOS — replace-receiver assumes B9 exists as the default "
            "transit location."
        )
    return int(eid)


def _resolve_old_receiver(w: TOSWriter, station_eid: int) -> tuple[int, str]:
    """Find the currently-open gnss_receiver at a station; return (id, serial).

    Raises ``CfgOperationError`` when no open receiver exists (nothing to
    replace) or when its serial is unreadable.
    """
    old_id = _find_open_gnss_receiver_child(w, station_eid)
    if old_id is None:
        raise CfgOperationError(
            "Station has no currently-open gnss_receiver child — there is "
            "nothing to replace. Did the swap already happen, or did the "
            "previous unit already get moved out?"
        )
    old_hist = w.get_entity_history(old_id)
    old_serial = (
        _device_attribute(old_hist, "serial_number")
        if isinstance(old_hist, dict)
        else None
    )
    if not old_serial:
        raise CfgOperationError(
            f"Old receiver (id_entity={old_id}) has no readable serial_number "
            f"attribute — refusing to replace without identifying it first."
        )
    return old_id, old_serial


def _validate_marker_match(
    probed_marker: Optional[str],
    station_id: str,
) -> None:
    """Ensure the probed receiver's marker matches the destination station.

    Acceptable: ``None`` (probe couldn't read marker), the literal
    ``"TEST"`` (bench default that should be auto-corrected — handled
    later), or an exact case-insensitive match for ``station_id``.

    Any other value indicates a potential misinstall (the receiver was
    configured for a different station) — refuse the replace.
    """
    if probed_marker is None:
        return
    pm = probed_marker.strip().upper()
    if pm in ("TEST", station_id.upper()):
        return
    raise CfgOperationError(
        f"Receiver's RINEX marker_name is {probed_marker!r} — expected "
        f"{station_id.upper()!r} (the destination) or 'TEST' (the "
        f"bench default). Refusing to replace: the unit may be "
        f"configured for a different station, or you typed the wrong "
        f"--station. Verify physically or pass --skip-marker-check to "
        f"override."
    )


def replace_receiver(
    station_id: str,
    new_type: str,
    *,
    date: Optional[str] = None,
    host: Optional[str] = None,
    new_serial: Optional[str] = None,
    new_model: Optional[str] = None,
    new_firmware: Optional[str] = None,
    new_marker: Optional[str] = None,
    owner: str = "Jarðeðlismælihópur",
    old_status: Optional[str] = "bilað",
    old_comment: Optional[str] = "can't connect to the receiver",
    vitjun: Optional[str] = None,
    participants: str = "",
    continue_from: Optional[str] = None,
    skip_marker_check: bool = False,
    dry_run: bool = True,
    writer: Optional[TOSWriter] = None,
    cfg_path: Optional[Path] = None,
) -> OperationResult:
    """One-shot receiver replacement on a station: warehouse + retire + install.

    Encodes the canonical 3-step field workflow as a single operation:

      1. **Warehouse intake** — if the new receiver's serial is not yet
         in TOS, ``create_device`` + B9 join at ``eff_date``. If it's
         already in TOS and parked at B9 (or has no open parent),
         reuse the existing entity. If it's deployed elsewhere, refuse.
      2. **Move OLD** — close the station→OLD join at ``eff_date``,
         open B9→OLD join at the same date, apply
         ``device_status='bilað'`` + comment defaults so the unit
         shows up as out-of-service in B9.
      3. **Install NEW** — close B9→NEW join (if any), open
         station→NEW at ``eff_date``, write the Breyting vitjun on
         the station with auto-derived text, update ``stations.cfg``
         (``receiver_serial``/``receiver_type``/
         ``receiver_firmware_version``/``rinex_config_valid_from``).

    All three steps share the same ``eff_date`` — accepted UI-bug
    trade-off in exchange for a single coherent timestamp.

    Args:
        station_id: 4-char RINEX marker of the destination station.
        new_type: Probe-type for the new receiver — one of the
            entries in :data:`receivers.cfg.device_probe.PROBE_STRATEGIES`.
            Required for the probe protocol selection.
        date: When the swap happened. Default: now. Bare date →
            noon. Used identically for all three transitions.
        host: ``IP[:PORT]`` for the probe. Default: derived from
            ``stations.cfg[station_id].router_ip`` plus the
            probe-type's default port.
        new_serial / new_model / new_firmware: Manual override —
            when all three are given, the probe step is skipped.
            Use for offline entry days after the field visit.
        new_marker: Override the probed marker (when probe doesn't
            return one or the operator wants to assert it).
        owner: TOS owner attribute for the new device entity.
            Default ``"Jarðeðlismælihópur"``.
        old_status: ``status`` attribute value for the OLD device.
            Default ``"bilað"``. Pass ``None`` to leave unchanged.
        old_comment: ``comment`` attribute value for the OLD device.
            Default ``"can't connect to the receiver"``. Pass ``None``
            to skip.
        vitjun: Override the auto-derived vitjun work text on the
            destination station's install record.
        participants: Comma-separated emails for the vitjun.
        continue_from: Skip to step ``"warehouse"`` / ``"move-old"`` /
            ``"install-new"`` for recovery from partial failure.
        skip_marker_check: Bypass the probed-marker-vs-station check
            (use when the receiver's marker is intentionally weird).
        dry_run / writer / cfg_path: As :func:`move_device`.

    Returns:
        :class:`OperationResult` with ``operation="replace"`` and
        per-step responses in ``tos_changes``.

    Raises:
        CfgOperationError: For any precondition failure — unknown
            station, no open receiver to replace, marker mismatch,
            probed serial equals old serial, new device already
            joined to a non-B9 parent, missing required identity
            (without probe and without --new-serial/model/firmware).
    """
    from .device_probe import (
        ProbeError,
        parse_host_port,
        probe_receiver,
    )

    if continue_from is not None and continue_from not in REPLACE_STEPS:
        raise CfgOperationError(
            f"--continue-from must be one of {REPLACE_STEPS}, "
            f"got {continue_from!r}"
        )

    w = _resolve_writer(writer, dry_run)
    station_eid = _resolve_station(w, station_id)

    # Identify the OLD device (currently-open receiver at the station)
    old_id, old_serial = _resolve_old_receiver(w, station_eid)

    # Identify the NEW device — probe unless full manual data given
    manual = all(v is not None for v in (new_serial, new_model, new_firmware))
    probed_marker: Optional[str] = new_marker
    if not manual:
        # Resolve probe host:port — operator override > stations.cfg
        probe_host: str
        probe_port: Optional[int]
        if host:
            probe_host, probe_port = parse_host_port(host)
        else:
            cfg_host = _station_router_ip(station_id, cfg_path)
            if cfg_host is None:
                raise CfgOperationError(
                    f"No --host given and stations.cfg[{station_id}] has "
                    f"no router_ip — pass --host IP[:PORT] explicitly."
                )
            probe_host, probe_port = cfg_host, None
        try:
            identity = probe_receiver(
                probe_host,
                probe_port,
                probe_type=new_type,
                station_id_hint=station_id,
            )
        except ProbeError as exc:
            raise CfgOperationError(
                f"Probe of {probe_host}:{probe_port} ({new_type}) failed: "
                f"{exc}. If you have the new receiver's identity from "
                f"field notes, pass --new-serial X --new-model Y "
                f"--new-firmware Z to skip the probe."
            ) from exc
        new_serial = new_serial or identity.serial
        new_model = new_model or identity.model_raw
        new_firmware = new_firmware or identity.firmware_version
        if probed_marker is None:
            probed_marker = identity.marker_name

    if not new_serial or not new_model:
        raise CfgOperationError(
            "replace_receiver: serial and model are required (either via "
            "probe or via --new-serial / --new-model)."
        )

    if new_serial == old_serial:
        raise CfgOperationError(
            f"Probed/given new serial {new_serial!r} matches the old "
            f"receiver at {station_id}. Did the physical swap actually "
            f"happen? Aborting to avoid creating a no-op TOS history."
        )

    if not skip_marker_check:
        _validate_marker_match(probed_marker, station_id)

    eff_date = _visit_default_time(date)

    # Pre-check: if the new serial is already in TOS, sanity-check its parent
    existing_new = w.find_device_by_serial("gnss_receiver", new_serial)
    new_device_id: Optional[int] = None
    needs_warehouse_intake = True
    if existing_new is not None:
        new_device_id = int(existing_new["id_entity"])
        open_join = w.get_open_parent_join(new_device_id)
        current_parent = open_join.get("id_entity_parent") if open_join else None
        b9_eid = _b9_eid(w)
        if current_parent is None or current_parent == b9_eid:
            needs_warehouse_intake = False  # already warehoused or floating — reuse
        else:
            raise CfgOperationError(
                f"Device with serial {new_serial!r} (id_entity="
                f"{new_device_id}) is already joined to TOS entity "
                f"{current_parent}, not B9. Either it's still deployed "
                f"on a station, or someone moved it manually. Use the "
                f"atomic verbs (cfg move-device --serial {new_serial} "
                f"...) instead of replace-receiver."
            )

    # Build a unified result aggregating the three steps
    result = OperationResult(
        operation="replace",
        station_id=station_id,
        serial=new_serial,
        date=eff_date,
        tos_changes={
            "plan": {
                "old_serial": old_serial,
                "new_serial": new_serial,
                "new_model": new_model,
                "new_firmware": new_firmware,
                "needs_warehouse_intake": needs_warehouse_intake,
            }
        },
        dry_run=dry_run,
    )

    start_step = continue_from or "warehouse"

    # --- Step 1: Warehouse intake -----------------------------------------
    if start_step == "warehouse":
        if needs_warehouse_intake:
            from tostools.device import build_required_attributes
            from tostools.standards.igs_equipment import to_igs_receiver

            igs_model = to_igs_receiver(new_model) or new_model
            attrs = build_required_attributes(
                serial=new_serial,
                model=igs_model,
                owner=owner,
                date_start=eff_date,
            )
            if new_firmware:
                attrs.append({
                    "code": "firmware_version",
                    "value": new_firmware,
                    "date_from": eff_date,
                    "date_to": None,
                })
            created = w.create_device(
                entity_subtype="gnss_receiver",
                attributes=attrs,
                force=False,
            )
            result.tos_changes["warehouse_create"] = created
            new_device_id = (
                created.get("id_entity") if isinstance(created, dict) else None
            )
            if new_device_id is None and not dry_run:
                raise RuntimeError(
                    "warehouse step: create_device returned no id_entity"
                )
            connect = w.connect_device_to_location(
                int(new_device_id) if new_device_id else 0,
                location_name=DEFAULT_WAREHOUSE,
                date_start=eff_date,
                type_filter="vöruhús",
            )
            result.tos_changes["warehouse_connect"] = connect
        else:
            result.tos_changes["warehouse_create"] = "skipped (already in TOS)"
        start_step = "move-old"

    # --- Step 2: Move OLD station → B9 with status/comment ---------------
    if start_step == "move-old":
        move_old = move_device(
            old_serial,
            to=DEFAULT_WAREHOUSE,
            date=eff_date,
            device_status=old_status,
            device_comment=old_comment,
            participants=participants,
            dry_run=dry_run,
            writer=w,
            skip_vitjun=True,
        )
        result.tos_changes["move_old"] = move_old.tos_changes
        start_step = "install-new"

    # --- Step 3: Install NEW B9 → station with auto-vitjun + cfg ---------
    if start_step == "install-new":
        # Tell move_device "the open receiver at the station is the one we
        # just moved out" — needed for dry-run preview, and harmless in
        # live mode (the join is already closed by step 2 there).
        install_new = move_device(
            new_serial,
            to=station_id,
            from_station=None,
            date=eff_date,
            firmware=new_firmware,
            vitjun=vitjun,
            participants=participants,
            dry_run=dry_run,
            writer=w,
            cfg_path=cfg_path,
            _assume_cleared_device_id=old_id,
        )
        result.tos_changes["install_new"] = install_new.tos_changes
        result.cfg_changes = install_new.cfg_changes
        result.vitjun_id = install_new.vitjun_id

    return result


def _station_router_ip(
    station_id: str,
    cfg_path: Optional[Path] = None,
) -> Optional[str]:
    """Read ``router_ip`` from ``stations.cfg`` for use as the probe host."""
    import configparser

    path = _resolve_cfg_path(cfg_path)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    if not parser.has_section(station_id):
        return None
    return parser.get(station_id, "router_ip", fallback=None) or None
