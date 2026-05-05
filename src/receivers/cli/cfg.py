"""``receivers cfg`` — three-way reconciliation CLI.

Compares values in ``stations.cfg`` against:

* the live receiver (via the health pipeline)
* TOS (via :mod:`tostools`)

The intended workflow is **TOS → cfg**: TOS is authoritative; the live
receiver is a validation source that flags issues for human review. The
silent auto-write that ``receivers health`` used to perform has been
removed in favour of this explicit, reviewable workflow.

Subcommands:

* ``reconcile`` — show diffs and (optionally) write fixes to stations.cfg
* ``list``      — show currently-open discrepancies (queryable audit log)
* ``history``   — show the full detection/resolution history for a station or field
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..cfg.field_manifest import (
    FIELDS,
    all_keys,
    fields_by_key,
    with_position_tolerance,
)
from ..cfg.reconciler import (
    FieldDiff,
    SourceUnavailableError,
    Verdict,
    apply_diff,
    compare_station,
    remove_diff,
)

logger = logging.getLogger(__name__)


def _progress(message: str, *, json_mode: bool, **kwargs) -> None:
    """Print progress lines to stderr in JSON mode, stdout otherwise.

    Without this, every "↳ STATION: querying TOS…" line lands on stdout
    and corrupts the JSON document the caller expects.
    """
    stream = sys.stderr if json_mode else sys.stdout
    print(message, file=stream, **kwargs)


# ---------------------------------------------------------------------------
# Source acquisition
# ---------------------------------------------------------------------------


def _load_station_configs(
    station_ids: Sequence[str], *, json_mode: bool = False
) -> Dict[str, Dict[str, Any]]:
    """Return ``{station_id: station_config}`` for the requested stations.

    ``get_station_config()`` already merges raw stations.cfg keys into
    the typed dict (via ``setdefault``), so flat fields like
    ``receiver_serial`` and ``latitude`` are readable here.

    Skip warnings go to stderr when ``json_mode`` is set so they don't
    contaminate the JSON document on stdout.
    """
    from ..config_utils import get_station_config

    configs: Dict[str, Dict[str, Any]] = {}
    for sid in station_ids:
        cfg = get_station_config(sid)
        if cfg is None:
            _progress(
                f"⚠️  {sid}: not found in stations.cfg — skipping",
                json_mode=json_mode,
            )
            continue
        configs[sid] = cfg
    return configs


def _all_station_ids() -> List[str]:
    """Return all 4-letter uppercase station IDs from stations.cfg."""
    try:
        import gps_parser  # type: ignore
    except ImportError:
        print("❌ gps_parser not available — cannot enumerate stations")
        return []
    cp = gps_parser.ConfigParser()
    excluded = {"DEFAULT", "DEFAULTS", "Configs", "PATHS", "FILES"}
    return sorted(
        s
        for s in cp.config.sections()
        if s not in excluded and s.isupper() and len(s) == 4
    )


def _query_receiver_identity(
    station_id: str, station_config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Run a receiver health probe and return the identity dict, or None.

    The probe skips file/NTRIP checks since we only need identity. Returns
    ``None`` on any failure (unreachable, auth error, parse failure).
    """
    try:
        from ..base.receiver_factory import create_receiver

        receiver = create_receiver(station_id, station_config)
        # Identity-only path: bypass NTRIP/file checks. Most extractors
        # populate receiver_identity inside get_health_status() itself.
        health = receiver.get_health_status()
        if not isinstance(health, dict):
            return None

        identity = dict(health.get("receiver_identity") or {})

        # Enrich identity with position from PVT solution — receiver coordinates
        # are reconcilable QC values, but the extractor stores them under
        # metrics.position rather than receiver_identity. Promote them here so
        # the cfg reconcile field manifest can read everything from one dict.
        position = (health.get("metrics") or {}).get("position") or {}
        for key in ("latitude", "longitude", "height"):
            val = position.get(key)
            if val is not None:
                identity[key] = val

        # Antenna metadata (type/serial/radome/height delta) is only useful
        # for cfg reconcile, so the extractor doesn't probe it during routine
        # 5-min health checks. Run the dedicated ASCII probe here. Best-effort:
        # failure leaves the antenna fields blank in the diff, which the
        # reconciler renders as NO_DATA.
        antenna_info = _query_antenna_info(station_id, station_config)
        if antenna_info:
            identity.update(antenna_info)

        if not identity:
            logger.debug("[%s] receiver returned no identity dict", station_id)
            return None
        return identity
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] receiver probe failed: %s", station_id, exc)
        return None


def _query_antenna_info(
    station_id: str, station_config: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Probe antenna metadata via the PolaRX5 ASCII control channel.

    Currently only PolaRX5 exposes antenna config over the control port. For
    other receiver types this is a no-op — the cfg reconcile flow falls back
    to TOS as the only authoritative source, which is correct.
    """
    try:
        receiver_type = (station_config.get("receiver_type") or "").lower()
        if "polarx" not in receiver_type:
            return None
        from ..health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        host = station_config.get("router_ip") or station_config.get("ip_number")
        if not host:
            return None
        control_port = int(
            station_config.get("receiver_controlport")
            or station_config.get("control_port")
            or 28784
        )
        extractor = PolaRX5TCPExtractor(host, station_id, port=control_port)
        return extractor.query_antenna_info()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] antenna probe failed: %s", station_id, exc)
        return None


def _query_tos(station_id: str) -> Optional[Dict[str, Any]]:
    try:
        from tostools.api.tos_client import TOSClient
    except ImportError:
        return None
    try:
        client = TOSClient()
        data = client.get_complete_station_metadata(station_id)
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] TOS query failed: %s", station_id, exc)
        return None


# ---------------------------------------------------------------------------
# Per-station probe (parallelisable I/O — no side effects, no prompts)
# ---------------------------------------------------------------------------


def _probe_station(
    station_id: str,
    station_config: Dict[str, Any],
    sources: List[str],
    json_mode: bool,
    verbose: bool = True,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Query receiver and TOS for one station.

    Pure I/O — creates its own network connections, returns data only.
    Safe to call from a thread.  When ``verbose=False`` (parallel mode)
    per-station progress lines are suppressed so interleaved output doesn't
    corrupt the terminal.

    Returns ``(receiver_identity, tos_data)``.
    """
    receiver_identity: Optional[Dict[str, Any]] = None
    tos_data: Optional[Dict[str, Any]] = None

    if "receiver" in sources:
        if station_config.get("_adhoc"):
            if verbose:
                _progress(
                    f"   ↳ {station_id}: ad-hoc config, skipping receiver probe",
                    json_mode=json_mode,
                )
        else:
            if verbose:
                _progress(
                    f"   ↳ {station_id}: probing receiver…",
                    json_mode=json_mode,
                    flush=True,
                )
            receiver_identity = _query_receiver_identity(station_id, station_config)
            if receiver_identity is None and verbose:
                _progress(
                    f"   ↳ {station_id}: receiver unreachable or no identity",
                    json_mode=json_mode,
                )

    if "tos" in sources:
        if verbose:
            _progress(
                f"   ↳ {station_id}: querying TOS…",
                json_mode=json_mode,
                flush=True,
            )
        tos_data = _query_tos(station_id)
        if tos_data is None and verbose:
            _progress(
                f"   ↳ {station_id}: not in TOS or TOS unavailable",
                json_mode=json_mode,
            )

    return receiver_identity, tos_data


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_VERDICT_GLYPH = {
    Verdict.OK: "✓",
    Verdict.MISSING: "?",
    Verdict.CONFLICT: "✗",
    Verdict.SOURCES_DISAGREE: "!",
    Verdict.NO_DATA: "·",
    Verdict.NOT_QUERYABLE: "·",
}


def _render(value: Optional[str], width: int = 22) -> str:
    if value is None:
        s = "—"
    else:
        s = value
    if len(s) > width:
        s = s[: width - 1] + "…"
    return s.ljust(width)


def _print_setup_header(station_id: str, station_config: Dict[str, Any]) -> None:
    """One-screen overview of the station's deployment setup.

    These are non-reconcilable, contextual fields the operator needs in
    order to decide whether a flagged value is a real discrepancy or a
    stale TOS entry (e.g. is the unit on a Teltonika modem? what's the
    install date? which port is the FTP daemon on?).
    """
    name = station_config.get("station_name") or ""
    header = f"\n=== {station_id}"
    if name:
        header += f" — {name}"
    header += " ==="
    print(header)

    rows = [
        ("Owner", station_config.get("station_owner")),
        ("Connection", station_config.get("connection_type")),
        ("Power", station_config.get("power_type")),
        ("Router IP", station_config.get("router_ip")),
        ("Router type", station_config.get("router_type")),
        (
            "Receiver ports",
            ", ".join(
                f"{label}={station_config.get(key)}"
                for label, key in (
                    ("ftp", "receiver_ftpport"),
                    ("http", "receiver_httpport"),
                    ("ctl", "receiver_controlport"),
                )
                if station_config.get(key)
            )
            or None,
        ),
        ("Config valid from", station_config.get("rinex_config_valid_from")),
        ("Lifecycle status", station_config.get("station_status")),
        ("Health check", station_config.get("health_check")),
    ]
    for label, val in rows:
        if val:
            print(f"  {label:<20} {val}")


def _print_diff_table(
    diffs: List[FieldDiff],
    show_ok: bool = True,
) -> None:
    print()
    print(f"   {'Field':<24} {'stations.cfg':<22} {'Receiver':<22} {'TOS':<22}")
    print(f"   {'-' * 24} {'-' * 22} {'-' * 22} {'-' * 22}")
    for d in diffs:
        # Always show format_mismatch and cfg_placeholder rows — they need cleanup
        if not show_ok and d.verdict == Verdict.OK and not d.format_mismatch:
            continue
        if not show_ok and d.verdict == Verdict.NO_DATA and not d.cfg_placeholder:
            continue
        if d.cfg_placeholder:
            glyph = "~"
        elif d.format_mismatch:
            glyph = "≈"
        else:
            glyph = _VERDICT_GLYPH.get(d.verdict, "?")
        cfg_display = d.cfg_raw if d.cfg_raw is not None else d.cfg_value
        print(
            f" {glyph} {d.label:<24} {_render(cfg_display)} "
            f"{_render(d.receiver_value)} {_render(d.tos_value)}"
        )
        if d.note:
            print(f"     ↳ {d.note}")


def _summary_counts(diffs: List[FieldDiff]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for d in diffs:
        counts[d.verdict.value] = counts.get(d.verdict.value, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Interactive prompt
# ---------------------------------------------------------------------------


_HELP = """
Actions:
  s   set the field to the suggested value (when shown)
  r   set to the receiver-reported value (cfg only)
  t   set to the TOS value (cfg only, no TOS push)
  T   push the receiver value to TOS — use only after confirming this is a
      data-entry error (Pattern 1: correct the open value in-place).
      If the discrepancy reflects an instrument change (new receiver, fw
      upgrade, antenna swap), close the old TOS period and add a new one
      manually — do NOT use T in that case.
  C   push the cfg value to TOS (when cfg is correct but TOS is wrong)
  e   enter a custom value
  k   keep the existing cfg value (skip)
  q   quit reconciliation for this station
  ?   show this help

Receiver-primary fields (type, serial, firmware) show a warning when TOS
disagrees. Check the TOS history first:
  data-entry error (wrong value typed) → fix cfg with r, then T to correct TOS in-place.
  instrument change (swap/upgrade not logged) → k to keep cfg, then add new period in TOS manually.

For antenna fields (type, serial, radome): TOS is canonical but the operator
may have a correct value in cfg that TOS lacks. Use C to push cfg → TOS
without changing cfg itself.
""".rstrip()


def _interactive_prompt(
    diff: FieldDiff, *, receiver_primary_active: bool = True
) -> Tuple[str, Optional[str]]:
    """Ask the user what to do for one field.

    Returns ``(action, value)`` where action is one of
    ``set``, ``push_tos``, ``push_cfg_to_tos``, ``skip``, ``quit``
    and ``value`` is the chosen value (for write actions).

    When ``receiver_primary_active`` is True and ``diff.spec.receiver_primary``
    is set, a warning is shown reminding the operator to check whether the
    TOS discrepancy is a data-entry error (fix with T) or an unlogged
    instrument change (requires manual TOS period management).
    """
    is_primary = receiver_primary_active and diff.spec.receiver_primary and diff.receiver_value is not None

    options: List[str] = []
    if diff.suggestion is not None:
        src = diff.suggestion_source or "?"
        options.append(f"[s]et to {diff.suggestion!r} ({src})")
    if diff.receiver_value is not None and diff.receiver_value != diff.suggestion:
        options.append(f"[r]eceiver={diff.receiver_value!r}")
    if diff.tos_value is not None and diff.tos_value != diff.suggestion:
        options.append(f"[t]os={diff.tos_value!r}")
    if diff.spec.tos_writable and diff.receiver_value is not None:
        options.append("[T]push-receiver-to-TOS")
    if diff.spec.tos_writable and diff.cfg_value is not None:
        options.append("[C]push-cfg-to-TOS")
    options.extend(["[e]dit", "[k]eep", "[q]uit", "[?]help"])

    if is_primary:
        print(
            "     ⚠  TOS discrepancy on hardware-identity field — check TOS history first:\n"
            "        data-entry error (wrong value typed) → fix cfg with [r], then [T] to correct TOS in-place.\n"
            "        instrument change (swap/upgrade not logged) → [k] keep cfg, then add new period in TOS manually."
        )

    if diff.note:
        print(f"     ↳ {diff.note}")

    while True:
        print(f"     {' · '.join(options)}")
        try:
            raw = input("     > ").strip()
            choice = raw.lower()
        except EOFError:
            return ("quit", None)

        if choice == "?" or choice == "help":
            print(_HELP)
            continue
        if choice in ("k", "keep"):
            return ("skip", None)
        if choice in ("q", "quit"):
            return ("quit", None)
        if choice in ("r", "receiver", ""):
            if diff.receiver_value is None:
                print("     (receiver value not available)")
                continue
            return ("set", diff.receiver_value)
        if choice in ("s", "set"):
            if diff.suggestion is None:
                print("     (no suggestion available — pick r/t/e)")
                continue
            return ("set", diff.suggestion)
        if raw == "T":  # case-sensitive: uppercase T = push receiver value to TOS
            if not diff.spec.tos_writable:
                print(f"     (field {diff.cfg_key!r} is not TOS-writable)")
                continue
            if diff.receiver_value is None:
                print("     (no receiver value to push — use C to push cfg value)")
                continue
            return ("push_tos", diff.receiver_value)
        if raw == "C":  # case-sensitive: uppercase C = push cfg value to TOS
            if not diff.spec.tos_writable:
                print(f"     (field {diff.cfg_key!r} is not TOS-writable)")
                continue
            if diff.cfg_value is None:
                print("     (no cfg value to push)")
                continue
            return ("push_cfg_to_tos", diff.cfg_value)
        if choice in ("t", "tos"):
            if diff.tos_value is None:
                print("     (TOS value not available)")
                continue
            return ("set", diff.tos_value)
        if choice in ("e", "edit"):
            try:
                custom = input("     value: ").strip()
            except EOFError:
                return ("quit", None)
            if not custom:
                print("     (empty — skipping)")
                return ("skip", None)
            return ("set", custom)
        print(f"     unknown action {choice!r}")


# ---------------------------------------------------------------------------
# TOS push helper
# ---------------------------------------------------------------------------


def _effective_date_for(args: argparse.Namespace) -> str:
    """Return an ISO-8601 date_from for TOS attribute writes.

    Uses ``args.effective_date`` when set, otherwise falls back to current UTC
    with a warning — correct for serial/firmware corrections where the operator
    knows the actual change date only approximately.
    """
    ed = getattr(args, "effective_date", None)
    if ed:
        return ed
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    logger.debug("--effective-date not set; defaulting to now (%s)", now)
    return now


def _do_push_tos(
    station_id: str,
    diff: FieldDiff,
    value: str,
    tos_data: Optional[Dict[str, Any]],
    args: argparse.Namespace,
    silent: bool,
) -> None:
    """Push *value* for *diff.spec* to TOS; handles errors and dry-run."""
    if tos_data is None:
        if not silent:
            print(f"     ❌ cannot push to TOS: no TOS data for {station_id}")
        return

    try:
        from tostools.api.tos_writer import TOSWriter

        from ..cfg.tos_push import push_field_to_tos
    except ImportError as exc:
        if not silent:
            print(f"     ❌ tostools not available: {exc}")
        return

    dry_run: bool = getattr(args, "dry_run", True)
    writer = TOSWriter(dry_run=dry_run)
    date_from = _effective_date_for(args)

    if not silent:
        mode = "[DRY-RUN] " if dry_run else ""
        print(
            f"     {mode}→ push to TOS: {diff.cfg_key} = {value!r} "
            f"(attr={diff.spec.tos_attribute_code!r}, "
            f"entity={diff.spec.tos_target_entity}, date_from={date_from})"
        )

    try:
        result = push_field_to_tos(
            writer=writer,
            spec=diff.spec,
            value=value,
            tos_data=tos_data,
            date_from=date_from,
        )
        if not silent:
            if hasattr(result, "method"):  # DryRunResult
                print(f"     ✅ [dry-run] would {result.method} {result.endpoint}")
            else:
                print(f"     ✅ TOS updated: {diff.cfg_key} = {value!r}")
    except Exception as exc:  # noqa: BLE001
        if not silent:
            print(f"     ❌ TOS push failed: {exc}")
        logger.warning("[%s] TOS push failed for %s: %s", station_id, diff.cfg_key, exc)


# ---------------------------------------------------------------------------
# Position sanity gate
# ---------------------------------------------------------------------------


def _position_sanity_check(
    diffs: List[FieldDiff],
    abort_m: float,
) -> Optional[str]:
    """Return an error message if the receiver position is suspiciously far from TOS.

    Computes the receiver-vs-TOS delta for each position field in metres and
    returns a message when any single axis exceeds ``abort_m``.  Returns None
    when the position looks sane or when position data is unavailable.

    This is intentionally a per-axis check rather than a 3-D distance so that
    a single bad axis (e.g. PVT height spike) is still caught even when
    horizontal looks fine.
    """
    import math

    _DEG_PER_M_LAT = 1.0 / 111111.0
    _DEG_PER_M_LON = 1.0 / (111111.0 * math.cos(math.radians(64.0)))

    for d in diffs:
        if d.cfg_key not in ("latitude", "longitude", "height"):
            continue
        if d.receiver_value is None or d.tos_value is None:
            continue
        try:
            delta = abs(float(d.receiver_value) - float(d.tos_value))
        except (ValueError, TypeError):
            continue
        if d.cfg_key == "latitude":
            delta_m = delta / _DEG_PER_M_LAT
        elif d.cfg_key == "longitude":
            delta_m = delta / _DEG_PER_M_LON
        else:
            delta_m = delta

        if delta_m > abort_m:
            return (
                f"⛔  POSITION SANITY FAIL: {d.label} differs by {delta_m:.0f} m "
                f"(receiver={d.receiver_value}, TOS={d.tos_value}, threshold={abort_m:.0f} m). "
                f"Receiver may be at the wrong station — "
                f"hardware-identity auto-push blocked."
            )
    return None


# ---------------------------------------------------------------------------
# Per-station reconciliation
# ---------------------------------------------------------------------------


def _reconcile_one(
    station_id: str,
    station_config: Dict[str, Any],
    sources: List[str],
    fields: Optional[List[str]],
    args: argparse.Namespace,
    receiver_identity: Optional[Dict[str, Any]] = None,
    tos_data: Optional[Dict[str, Any]] = None,
) -> Tuple[List[FieldDiff], int, int]:
    """Reconcile one station given pre-fetched probe data.

    ``receiver_identity`` and ``tos_data`` must already be fetched by the
    caller (via :func:`_probe_station`). This function only handles
    comparison, display, and writes — no network I/O.

    Returns ``(diffs, n_written, n_skipped)``.
    """
    if not args.json:
        _print_setup_header(station_id, station_config)

    tolerance_m = getattr(args, "position_tolerance_m", 2.0)
    field_specs = with_position_tolerance(tolerance_m) if tolerance_m else None

    diffs = compare_station(
        station_id=station_id,
        station_config=station_config,
        receiver_identity=receiver_identity,
        tos_data=tos_data,
        fields=fields,
        queried_sources=set(sources) | {"cfg"},
        field_specs=field_specs,
    )

    silent = args.json
    show_ok = not (args.only_diffs or getattr(args, "open", False))
    if not silent:
        _print_diff_table(diffs, show_ok=show_ok)

    # Position sanity gate — must run before any write logic so the warning
    # is visible even in dry-run mode.
    abort_m: float = getattr(args, "position_abort_m", 50.0)
    position_warn = _position_sanity_check(diffs, abort_m)
    if position_warn and not silent:
        print(f"\n   {position_warn}")

    n_written = 0
    n_skipped = 0
    actionable = [d for d in diffs if d.needs_attention]
    canonicalize_on = getattr(args, "canonicalize", False)
    fmt_mismatches = [d for d in diffs if d.format_mismatch] if canonicalize_on else []
    # cfg_placeholder rows are always collected — cleaned in --canonicalize or
    # interactively, independent of whether other fields need attention.
    cfg_placeholders = [d for d in diffs if d.cfg_placeholder]
    if not actionable and not fmt_mismatches and not cfg_placeholders:
        return diffs, 0, 0

    if args.dry_run:
        if not silent and actionable:
            print(f"\n   {len(actionable)} field(s) need attention (dry-run, no writes)")
        if not silent and cfg_placeholders:
            keys = ", ".join(d.cfg_key for d in cfg_placeholders)
            print(f"\n   {len(cfg_placeholders)} placeholder value(s) to remove: {keys} (dry-run)")
        # canonicalize dry-run section handled below; fall through
        if not canonicalize_on:
            return diffs, 0, len(actionable)

    # In JSON mode without an auto-resolution flag we can't make decisions —
    # nothing to do beyond returning diffs for the caller to report.
    push_to_tos_on = getattr(args, "push_tos", False)
    if silent and not (args.auto_fill or args.yes or push_to_tos_on):
        return diffs, 0, 0

    # --push-tos batch mode: push receiver values to TOS for all writable fields
    # that have a receiver value, independent of the cfg reconciliation loop below.
    if push_to_tos_on and "tos" in sources and tos_data is not None:
        if not silent:
            writable = [
                d for d in actionable
                if d.spec.tos_writable and d.receiver_value is not None
            ]
            if writable:
                print(f"\n   Pushing {len(writable)} field(s) to TOS…")
        for d in actionable:
            if not d.spec.tos_writable or d.receiver_value is None:
                continue
            if args.yes or not silent:
                _do_push_tos(
                    station_id=station_id,
                    diff=d,
                    value=d.receiver_value,
                    tos_data=tos_data,
                    args=args,
                    silent=silent,
                )
    elif push_to_tos_on and "tos" not in sources:
        if not silent:
            print("   ⚠️  --push-tos requires --source tos or --source both")

    field_specs_by_key = fields_by_key()
    no_receiver_primary: bool = getattr(args, "no_receiver_primary", False) or getattr(
        args, "interactive", False
    )
    # Position sanity failure disables receiver-primary auto-push regardless of flags.
    receiver_primary_active = not no_receiver_primary and position_warn is None

    if not silent:
        print()
    for idx, d in enumerate(actionable, start=1):
        if not silent:
            header = f"  [{idx}/{len(actionable)}] {d.cfg_key} ({d.verdict.value})"
            if d.spec.receiver_primary and receiver_primary_active:
                header += "  [receiver-primary]"
            print(header)
            cfg_disp = d.cfg_raw if d.cfg_raw is not None else d.cfg_value
            print(
                f"     cfg:      {cfg_disp if cfg_disp is not None else '[missing]'}"
            )
            if "receiver" in sources:
                rx = d.receiver_value if d.receiver_value is not None else "[N/A]"
                print(f"     receiver: {rx}")
            if "tos" in sources:
                tos = d.tos_value if d.tos_value is not None else "[N/A]"
                print(f"     TOS:      {tos}")

        # Resolve action
        action: str
        new_value: Optional[str]
        is_primary = (
            receiver_primary_active
            and d.spec.receiver_primary
            and d.receiver_value is not None
            and d.spec.tos_writable
            and tos_data is not None
        )
        if args.auto_fill and d.verdict == Verdict.MISSING and d.suggestion is not None:
            if is_primary and d.suggestion_source in ("receiver", "agree"):
                action, new_value = "set_and_push_tos", d.suggestion
                if not silent:
                    print(f"     → auto-fill from {d.suggestion_source}: {d.suggestion!r} (cfg + TOS)")
            else:
                action, new_value = "set", d.suggestion
                if not silent:
                    print(f"     → auto-fill from {d.suggestion_source}: {d.suggestion!r}")
        elif args.yes and d.suggestion is not None:
            if is_primary and d.suggestion_source in ("receiver", "agree"):
                action, new_value = "set_and_push_tos", d.suggestion
                if not silent:
                    print(f"     → accept suggestion ({d.suggestion_source}): {d.suggestion!r} (cfg + TOS)")
            else:
                action, new_value = "set", d.suggestion
                if not silent:
                    print(f"     → accept suggestion ({d.suggestion_source}): {d.suggestion!r}")
        elif args.yes and is_primary and d.receiver_value is not None:
            # --yes with receiver_primary but no agreed suggestion: still take receiver
            action, new_value = "set_and_push_tos", d.receiver_value
            if not silent:
                print(f"     → accept receiver (primary): {d.receiver_value!r} (cfg + TOS)")
        elif silent:
            # JSON mode without an applicable auto-rule: cannot prompt; skip.
            action, new_value = "skip", None
        else:
            action, new_value = _interactive_prompt(
                d, receiver_primary_active=receiver_primary_active
            )

        if action == "quit":
            if not silent:
                print(f"\n     stopped at field {idx}/{len(actionable)}")
            break
        if action == "skip":
            n_skipped += 1
            continue
        if action == "push_tos" and new_value is not None:
            _do_push_tos(
                station_id=station_id,
                diff=d,
                value=new_value,
                tos_data=tos_data,
                args=args,
                silent=silent,
            )
            continue
        if action == "push_cfg_to_tos" and new_value is not None:
            if not silent:
                print(f"     → push cfg value to TOS: {d.cfg_key} = {new_value!r}")
            _do_push_tos(
                station_id=station_id,
                diff=d,
                value=new_value,
                tos_data=tos_data,
                args=args,
                silent=silent,
            )
            continue
        if action == "set_and_push_tos" and new_value is not None:
            # Apply cfg vocabulary mapping first (same as "set")
            spec = field_specs_by_key.get(d.cfg_key)
            if spec is not None:
                try:
                    mapped = spec.cfg_format(new_value)
                except Exception as exc:  # noqa: BLE001
                    if not silent:
                        print(f"     ❌ cfg_format failed for {d.cfg_key}: {exc}")
                    continue
                if mapped is None:
                    if not silent:
                        print(
                            f"     ❌ cfg_format normalised {new_value!r} to None — skipping"
                        )
                    continue
                if mapped != new_value and not silent:
                    print(
                        f"     ↺ normalised {new_value!r} → {mapped!r} for cfg vocabulary"
                    )
                new_value = mapped
            try:
                changed = apply_diff(station_id, d, new_value)
                if changed:
                    n_written += 1
                    if not silent:
                        print(f"     ✅ wrote {d.cfg_key} = {new_value!r} to cfg")
                elif not silent:
                    print(f"     ⏭  cfg unchanged ({d.cfg_key} already = {new_value!r})")
            except SourceUnavailableError as exc:
                if not silent:
                    print(f"     ❌ could not write cfg: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                if not silent:
                    print(f"     ❌ cfg write failed: {exc}")
                continue
            # Now push to TOS — best-effort, cfg write already succeeded
            _do_push_tos(
                station_id=station_id,
                diff=d,
                value=d.receiver_value or new_value,
                tos_data=tos_data,
                args=args,
                silent=silent,
            )
            continue
        if action == "set" and new_value is not None:
            # Apply per-field cfg vocabulary mapping (e.g. TOS "SEPT POLARX5"
            # → cfg "PolaRX5"). Identity for fields without an explicit map.
            spec = field_specs_by_key.get(d.cfg_key)
            if spec is not None:
                try:
                    mapped = spec.cfg_format(new_value)
                except Exception as exc:  # noqa: BLE001
                    if not silent:
                        print(f"     ❌ cfg_format failed for {d.cfg_key}: {exc}")
                    continue
                if mapped is None:
                    if not silent:
                        print(
                            f"     ❌ cfg_format normalised {new_value!r} to None — skipping"
                        )
                    continue
                if mapped != new_value and not silent:
                    print(
                        f"     ↺ normalised {new_value!r} → {mapped!r} for cfg vocabulary"
                    )
                new_value = mapped
            try:
                changed = apply_diff(station_id, d, new_value)
                if changed:
                    n_written += 1
                    if not silent:
                        print(f"     ✅ wrote {d.cfg_key} = {new_value!r}")
                elif not silent:
                    print(f"     ⏭  unchanged ({d.cfg_key} already = {new_value!r})")
            except SourceUnavailableError as exc:
                if not silent:
                    print(f"     ❌ could not write: {exc}")
            except Exception as exc:  # noqa: BLE001
                if not silent:
                    print(f"     ❌ write failed: {exc}")

    # --canonicalize: rewrite format-mismatch fields to receiver notation
    if canonicalize_on and fmt_mismatches:
        if not silent:
            print(f"\n   Canonicalizing {len(fmt_mismatches)} notation-only field(s)…")
        for d in fmt_mismatches:
            rx_val = d.receiver_value  # guaranteed non-None by format_mismatch
            assert rx_val is not None
            if args.dry_run:
                if not silent:
                    print(f"     ≈ {d.cfg_key}: {d.cfg_raw!r} → {rx_val!r} (dry-run)")
            else:
                try:
                    changed = apply_diff(
                        station_id, d, rx_val, resolved_by="canonicalize"
                    )
                    if changed:
                        n_written += 1
                        if not silent:
                            print(f"     ✅ {d.cfg_key}: {d.cfg_raw!r} → {rx_val!r}")
                    elif not silent:
                        print(f"     ⏭  {d.cfg_key} already canonical")
                except SourceUnavailableError as exc:
                    if not silent:
                        print(f"     ❌ {d.cfg_key}: could not write: {exc}")
                except Exception as exc:  # noqa: BLE001
                    if not silent:
                        print(f"     ❌ {d.cfg_key}: write failed: {exc}")

    # Placeholder cleanup: remove keys whose raw cfg value is a recognized placeholder
    # (e.g. TOS synthetic serials like antenna-AFST-20210527).
    # --canonicalize auto-removes without prompting; interactive mode asks [d]elete.
    if cfg_placeholders:
        if canonicalize_on:
            if not silent:
                print(f"\n   Removing {len(cfg_placeholders)} placeholder value(s)…")
            for d in cfg_placeholders:
                if args.dry_run:
                    if not silent:
                        print(f"     ~ {d.cfg_key}: remove {d.cfg_raw!r} (dry-run)")
                else:
                    try:
                        changed = remove_diff(
                            station_id, d, resolved_by="canonicalize"
                        )
                        if changed:
                            n_written += 1
                            if not silent:
                                print(f"     ✅ {d.cfg_key}: removed {d.cfg_raw!r}")
                        elif not silent:
                            print(f"     ⏭  {d.cfg_key} already absent")
                    except SourceUnavailableError as exc:
                        if not silent:
                            print(f"     ❌ {d.cfg_key}: could not remove: {exc}")
                    except Exception as exc:  # noqa: BLE001
                        if not silent:
                            print(f"     ❌ {d.cfg_key}: removal failed: {exc}")
        elif not silent and not args.dry_run:
            # Interactive: prompt for each placeholder
            print(f"\n   {len(cfg_placeholders)} placeholder value(s) in cfg:")
            for d in cfg_placeholders:
                print(f"\n     ~ {d.cfg_key} = {d.cfg_raw!r}  (placeholder — no real value)")
                print(f"       [d]elete · [k]eep · [q]uit")
                try:
                    choice = input("       > ").strip().lower()
                except EOFError:
                    choice = "q"
                if choice in ("q", "quit"):
                    break
                if choice in ("d", "delete", ""):
                    try:
                        changed = remove_diff(station_id, d)
                        if changed:
                            n_written += 1
                            print(f"       ✅ removed {d.cfg_key}")
                        else:
                            print(f"       ⏭  already absent")
                    except Exception as exc:  # noqa: BLE001
                        print(f"       ❌ removal failed: {exc}")
                else:
                    n_skipped += 1

    return diffs, n_written, n_skipped


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------


def cmd_cfg_reconcile(args) -> int:
    if args.list_fields:
        print("Reconcilable fields:")
        for f in FIELDS:
            sources = []
            if f.receiver_extract:
                sources.append("receiver")
            if f.tos_extract:
                sources.append("tos")
            print(f"  {f.cfg_key:30s} sources={','.join(sources):17s} {f.description}")
        return 0

    # Stations
    open_mode = getattr(args, "open", False)
    if open_mode:
        try:
            from ..cfg import discrepancy_log as _dlog
            station_ids = _dlog.open_station_ids()
        except Exception as exc:  # noqa: BLE001
            _progress(f"❌ could not read discrepancy log: {exc}", json_mode=args.json)
            return 1
        if not station_ids:
            _progress("✅ no open discrepancies — config is clean", json_mode=args.json)
            return 0
    elif args.all:
        station_ids = _all_station_ids()
    elif args.station:
        station_ids = [s.upper() for s in args.station]
    else:
        print("❌ specify station IDs, --all, or --open")
        print("   try: receivers cfg reconcile --help")
        return 2

    if not station_ids:
        print("❌ no stations to reconcile")
        return 1

    # Sources
    if args.source == "both":
        sources = ["receiver", "tos"]
    elif args.source == "receiver":
        sources = ["receiver"]
    elif args.source == "tos":
        sources = ["tos"]
    else:
        sources = ["receiver", "tos"]

    # Disable TOS if tostools unavailable, fall back gracefully
    if "tos" in sources:
        try:
            from tostools.api.tos_client import TOSClient  # noqa: F401
        except ImportError:
            _progress(
                "⚠️  tostools not installed — disabling TOS source",
                json_mode=args.json,
            )
            sources = [s for s in sources if s != "tos"]
            if not sources:
                _progress("❌ no usable sources remain", json_mode=args.json)
                return 1

    # Fields
    fields: Optional[List[str]] = None
    if args.field:
        valid = set(all_keys())
        invalid = [f for f in args.field if f not in valid]
        if invalid:
            print(f"❌ unknown fields: {invalid}")
            print(f"   available: {sorted(valid)}")
            return 2
        fields = list(args.field)
    elif open_mode:
        # Auto-derive fields that have open conflicts — no point probing unaffected fields.
        try:
            from ..cfg import discrepancy_log as _dlog
            fields = _dlog.open_field_keys(station_ids=station_ids) or None
        except Exception:  # noqa: BLE001
            fields = None  # fall back to all fields

    # Load configs
    configs = _load_station_configs(station_ids, json_mode=args.json)
    if not configs:
        return 1

    # Skip discontinued / inactive stations — probing them is pointless and,
    # in some cases, harmful (e.g. BLAL shares an IP with SODU).
    _SKIP_STATUSES = frozenset({"discontinued", "inactive"})
    skipped_status = {sid: cfg.get("station_status") for sid, cfg in configs.items()
                      if cfg.get("station_status") in _SKIP_STATUSES}
    skipped = list(skipped_status)
    for sid in skipped:
        del configs[sid]
    if skipped:
        _progress(
            f"⏭  skipping {len(skipped)} non-active station(s): {', '.join(sorted(skipped))}",
            json_mode=args.json,
        )
        # Auto-close any stale log entries so they don't resurface on the next --open run.
        try:
            from ..cfg import discrepancy_log as _dlog
            open_rows = _dlog.list_open(station_ids=skipped)
            for row in open_rows:
                _dlog.record_resolution(
                    row.station_id,
                    row.cfg_key,
                    action=_dlog.ACTION_IGNORED,
                    resolved_value=None,
                    note=f"station_status={skipped_status.get(row.station_id, '?')} — no longer reconciled",
                )
            if open_rows:
                _progress(
                    f"   closed {len(open_rows)} stale log entries for non-active stations",
                    json_mode=args.json,
                )
        except Exception as exc:  # noqa: BLE001
            _progress(f"⚠️  could not close stale log entries: {exc}", json_mode=args.json)
    if not configs:
        _progress("✅ no active stations with open discrepancies", json_mode=args.json)
        return 0

    # Header — keep stdout clean in JSON mode so output stays parseable
    _progress(
        f"Reconciling {len(configs)} station(s) — sources={','.join(sources)}"
        + (" — open discrepancies only" if open_mode else "")
        + (f" — fields={','.join(fields)}" if fields else "")
        + (" — dry-run" if args.dry_run else "")
        + (" — auto-fill" if args.auto_fill else "")
        + (" — canonicalize" if getattr(args, "canonicalize", False) else ""),
        json_mode=args.json,
    )

    # Resolve effective worker count (0 = auto).
    workers: int = getattr(args, "workers", 1)
    if workers == 0:
        workers = min(8, len(configs))

    # Single-station or single-worker: keep per-station verbose progress.
    parallel = workers > 1 and len(configs) > 1

    # --- Probe phase (parallel-safe I/O) ------------------------------------
    probe_results: Dict[str, Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = {}

    if parallel:
        _progress(
            f"Probing {len(configs)} station(s) with {workers} workers…",
            json_mode=args.json,
        )
        ordered_sids = [sid for sid in station_ids if sid in configs]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _probe_station, sid, configs[sid], sources, args.json, False
                ): sid
                for sid in ordered_sids
            }
            for future in as_completed(futures):
                sid = futures[future]
                try:
                    probe_results[sid] = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] probe thread raised: %s", sid, exc)
                    probe_results[sid] = (None, None)
    else:
        for sid in station_ids:
            if sid in configs:
                probe_results[sid] = _probe_station(
                    sid, configs[sid], sources, args.json, verbose=True
                )

    # --- Process phase (sequential: display, write, prompt) -----------------
    json_collect: List[Dict[str, Any]] = []
    total_written = 0
    total_skipped = 0
    n_with_issues = 0

    for sid in station_ids:
        cfg = configs.get(sid)
        if cfg is None:
            continue
        rx_identity, tos_d = probe_results.get(sid, (None, None))
        try:
            diffs, n_written, n_skipped = _reconcile_one(
                sid, cfg, sources, fields, args, rx_identity, tos_d
            )
        except KeyboardInterrupt:
            print("\nInterrupted")
            break

        total_written += n_written
        total_skipped += n_skipped
        if any(d.needs_attention for d in diffs):
            n_with_issues += 1

        if args.json:
            entry: Dict[str, Any] = {
                "station_id": sid,
                "diffs": [d.as_dict() for d in diffs],
                "summary": _summary_counts(diffs),
            }
            if args.auto_fill or args.yes:
                entry["writes"] = n_written
                entry["skipped"] = n_skipped
            json_collect.append(entry)

    if args.json:
        print(json.dumps(json_collect, indent=2, default=str))
        return 0

    # Final summary
    print("\n" + "=" * 60)
    print(
        f"Stations: {len(configs)}   "
        f"with issues: {n_with_issues}   "
        f"writes: {total_written}   skipped: {total_skipped}"
    )
    return 0


# ---------------------------------------------------------------------------
# `cfg list` / `cfg history`
# ---------------------------------------------------------------------------


def _parse_since(spec: str) -> datetime:
    """Parse ``--since`` values: ``30d``, ``12h``, ``45m`` or ISO 8601."""
    s = spec.strip().lower()
    if s and s[-1] in ("d", "h", "m"):
        try:
            n = int(s[:-1])
        except ValueError as exc:
            raise ValueError(f"invalid --since value {spec!r}") from exc
        unit = s[-1]
        delta = (
            timedelta(days=n)
            if unit == "d"
            else timedelta(hours=n)
            if unit == "h"
            else timedelta(minutes=n)
        )
        return datetime.now(timezone.utc) - delta
    try:
        # Accept "2026-04-01" or "2026-04-01T12:00:00+00:00"
        dt = datetime.fromisoformat(spec)
    except ValueError as exc:
        raise ValueError(
            f"invalid --since value {spec!r} (use 30d/12h/45m or ISO 8601)"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_ts(ts: Optional[datetime]) -> str:
    if ts is None:
        return "—"
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _print_records_table(records, *, show_resolution: bool) -> None:
    if not records:
        print("(no rows)")
        return
    if show_resolution:
        header = (
            f"{'Station':<8} {'Field':<28} {'Verdict':<18} "
            f"{'Detected':<17} {'By':<14} {'Resolved':<17} {'Action':<14}"
        )
    else:
        header = (
            f"{'Station':<8} {'Field':<28} {'Verdict':<18} "
            f"{'Detected':<17} {'By':<14} {'cfg':<14} {'rx':<14} {'tos':<14}"
        )
    print(header)
    print("-" * len(header))
    for r in records:
        if show_resolution:
            print(
                f"{r.station_id:<8} {r.cfg_key:<28} {r.verdict:<18} "
                f"{_fmt_ts(r.detected_at):<17} {(r.detected_by or ''):<14} "
                f"{_fmt_ts(r.resolved_at):<17} {(r.resolved_action or '—'):<14}"
            )
        else:
            print(
                f"{r.station_id:<8} {r.cfg_key:<28} {r.verdict:<18} "
                f"{_fmt_ts(r.detected_at):<17} {(r.detected_by or ''):<14} "
                f"{_render(r.cfg_value, 14)}{_render(r.receiver_value, 14)}"
                f"{_render(r.tos_value, 14)}"
            )


def cmd_cfg_list(args) -> int:
    """``cfg list`` — open discrepancies, optionally filtered."""
    from ..cfg import discrepancy_log as _dlog  # type: ignore[attr-defined]

    station_ids = [s.upper() for s in args.station] if args.station else None
    fields: Optional[List[str]] = None
    if args.field:
        valid = set(all_keys())
        invalid = [f for f in args.field if f not in valid]
        if invalid:
            print(f"❌ unknown fields: {invalid}")
            print(f"   available: {sorted(valid)}")
            return 2
        fields = list(args.field)
    verdicts = list(args.verdict) if args.verdict else None

    try:
        records = _dlog.list_open(
            station_ids=station_ids,
            cfg_keys=fields,
            verdicts=verdicts,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"❌ could not query cfg_discrepancy: {exc}")
        return 1

    if args.json:
        print(json.dumps([r.as_dict() for r in records], indent=2, default=str))
        return 0

    print(f"Open discrepancies: {len(records)}")
    if records:
        print()
        _print_records_table(records, show_resolution=False)
    return 0


def cmd_cfg_history(args) -> int:
    """``cfg history`` — full audit trail for a station and/or field."""
    from ..cfg import discrepancy_log as _dlog  # type: ignore[attr-defined]

    if not args.station and not args.field:
        print("❌ specify a station ID, --field KEY, or both")
        return 2

    station_id = args.station[0].upper() if args.station else None
    if args.field:
        valid = set(all_keys())
        invalid = [f for f in args.field if f not in valid]
        if invalid:
            print(f"❌ unknown fields: {invalid}")
            print(f"   available: {sorted(valid)}")
            return 2
        cfg_keys: List[str] = list(args.field)
    else:
        cfg_keys = []

    since: Optional[datetime] = None
    if args.since:
        try:
            since = _parse_since(args.since)
        except ValueError as exc:
            print(f"❌ {exc}")
            return 2

    try:
        if cfg_keys:
            records = []
            for key in cfg_keys:
                records.extend(
                    _dlog.get_history(
                        station_id=station_id,
                        cfg_key=key,
                        since=since,
                        limit=args.limit,
                    )
                )
            records.sort(key=lambda r: r.detected_at, reverse=True)
            records = records[: args.limit]
        else:
            records = _dlog.get_history(
                station_id=station_id,
                since=since,
                limit=args.limit,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"❌ could not query cfg_discrepancy: {exc}")
        return 1

    if args.json:
        print(json.dumps([r.as_dict() for r in records], indent=2, default=str))
        return 0

    target = station_id or f"--field {','.join(cfg_keys)}"
    print(f"History for {target} ({len(records)} row(s)):")
    if records:
        print()
        _print_records_table(records, show_resolution=True)
        if args.verbose:
            print("\nResolution notes:")
            for r in records:
                if r.resolution_note:
                    print(f"  [{r.id}] {r.station_id} {r.cfg_key}: {r.resolution_note}")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def create_cfg_parser(subparsers) -> None:
    """Register the ``cfg`` subcommand on the main parser."""
    cfg_parser = subparsers.add_parser(
        "cfg",
        help="Manage stations.cfg (reconcile, audit)",
        description=(
            "Three-way reconciliation between stations.cfg, the live "
            "receiver, and TOS. The intended workflow is TOS → cfg with "
            "the receiver as a validation source."
        ),
    )
    cfg_subparsers = cfg_parser.add_subparsers(
        dest="cfg_command", help="cfg subcommands"
    )

    rec = cfg_subparsers.add_parser(
        "reconcile",
        help="Compare cfg vs receiver vs TOS and (optionally) update cfg",
        description=(
            "For each station, query the requested sources, compare against "
            "stations.cfg, and present discrepancies. Default is interactive "
            "review per field; --auto-fill writes missing values where "
            "sources agree."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  receivers cfg reconcile ELDC
  receivers cfg reconcile ELDC THOB --source tos
  receivers cfg reconcile --open                   # QC: review all known open issues
  receivers cfg reconcile --open --dry-run --json  # machine-readable health report
  receivers cfg reconcile --all --auto-fill --field receiver_serial
  receivers cfg reconcile --all --dry-run --json
  receivers cfg reconcile --list-fields

Fixing firmware notation mismatches (e.g. 'NP 4.81 / SP 4.81' → '4.81'):
  receivers cfg reconcile --all --field receiver_firmware_version \\
      --source receiver --canonicalize --dry-run   # preview
  receivers cfg reconcile --all --field receiver_firmware_version \\
      --source receiver --canonicalize             # apply

Diagnosing TCP authentication failures:
  If health checks log "TCP command denied: receiver requires authentication"
  for a PolaRX5 station, the most common cause is a stale
  receiver_firmware_version in stations.cfg (e.g. recorded as 5.2.0 while
  the receiver has been upgraded to 5.7.0+).  The TCP login command was
  introduced in firmware 5.7.0; if stations.cfg records an older version the
  health probe skips sending credentials and the receiver rejects subsequent
  commands.  Fix with:
    receivers cfg reconcile <SID> --field receiver_firmware_version
  or in bulk (accepts receiver-reported version without prompting):
    receivers cfg reconcile --all --yes --field receiver_firmware_version \\
        --source receiver --dry-run   # preview first
    receivers cfg reconcile --all --yes --field receiver_firmware_version \\
        --source receiver             # then apply
        """,
    )
    rec.add_argument(
        "station",
        nargs="*",
        metavar="SID",
        help="Station IDs to reconcile (4-letter markers)",
    )
    rec.add_argument(
        "--all",
        action="store_true",
        help="Reconcile every station in stations.cfg",
    )
    rec.add_argument(
        "--open",
        action="store_true",
        help=(
            "Reconcile only stations with open discrepancies in the log "
            "(faster than --all; implies --only-diffs). "
            "Fields default to those with open discrepancies unless --field is given."
        ),
    )
    rec.add_argument(
        "--source",
        choices=["receiver", "tos", "both"],
        default="both",
        help="Which source(s) to compare against (default: both)",
    )
    rec.add_argument(
        "--field",
        nargs="+",
        metavar="KEY",
        help="Restrict reconciliation to specific cfg keys",
    )
    rec.add_argument(
        "--auto-fill",
        action="store_true",
        help="Auto-write missing cfg values where sources agree (no prompt)",
    )
    rec.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Accept all suggested values without prompting (use with care)",
    )
    rec.add_argument(
        "--canonicalize",
        action="store_true",
        help=(
            "Rewrite cfg values that are logically correct but stored in a "
            "different notation than the receiver uses (e.g. "
            "'NP 4.81 / SP 4.81' → '4.81', '4.8.1' → '4.81'). "
            "Only touches verdict=OK fields where raw cfg ≠ receiver value. "
            "Combine with --field receiver_firmware_version and --source receiver "
            "to canonicalize firmware notation across all stations."
        ),
    )
    rec.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show diffs but never write to stations.cfg",
    )
    rec.add_argument(
        "--only-diffs",
        action="store_true",
        help="Only print fields that need attention (hide OK rows)",
    )
    rec.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of tables; implies no interactive prompts",
    )
    rec.add_argument(
        "--list-fields",
        action="store_true",
        help="List reconcilable fields and exit",
    )
    rec.add_argument(
        "--position-tolerance-m",
        type=float,
        default=2.0,
        metavar="METERS",
        help=(
            "Tolerance for receiver↔TOS position comparison (default: 2.0 m). "
            "Receiver coordinates come from a real-time PVT solution and are "
            "used as a sanity check that the receiver is at the expected mark."
        ),
    )
    rec.add_argument(
        "--position-abort-m",
        type=float,
        default=50.0,
        metavar="METERS",
        help=(
            "Position discrepancy threshold above which receiver-primary "
            "auto-push (type/serial/firmware → TOS) is blocked for that "
            "station (default: 50.0 m). A large position delta suggests the "
            "receiver may be at the wrong station or TOS coordinates are "
            "corrupt — hardware identity should not be written automatically "
            "in that case. A warning is printed even in dry-run mode."
        ),
    )
    rec.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    rec.add_argument(
        "--workers",
        type=int,
        default=8,
        metavar="N",
        help=(
            "Parallel probe workers when reconciling multiple stations "
            "(default: 8; 0=auto=min(8,N); 1=sequential). "
            "Only the network probe phase is parallelised — writes and "
            "interactive prompts always run sequentially."
        ),
    )
    rec.add_argument(
        "--push-tos",
        action="store_true",
        help=(
            "For each field that has a receiver-reported value and is TOS-writable, "
            "push the receiver value to TOS (in addition to — or instead of — writing "
            "to stations.cfg). Requires --source to include 'tos'. "
            "Combine with --field to target specific fields (e.g. "
            "--field receiver_firmware_version --push-tos --yes). "
            "Honours --dry-run."
        ),
    )
    rec.add_argument(
        "--no-receiver-primary",
        action="store_true",
        default=False,
        help=(
            "Disable receiver-primary mode for hardware-identity fields "
            "(receiver_type, receiver_serial, receiver_firmware_version). "
            "By default, conflicts on these fields show a single combined "
            "'accept receiver → cfg + TOS' action as the default choice. "
            "Use this flag to revert to the traditional per-action prompt "
            "for the rare cases where the receiver value is not trustworthy "
            "(e.g. just-provisioned hardware with stale factory defaults)."
        ),
    )
    rec.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help=(
            "Force full interactive prompt for every field, including "
            "receiver-primary fields (type/serial/firmware) that would "
            "otherwise auto-accept the receiver value. Implies "
            "--no-receiver-primary. Useful for testing and manual review."
        ),
    )
    rec.add_argument(
        "--effective-date",
        metavar="ISO8601",
        help=(
            "date_from timestamp for TOS attribute writes (ISO-8601, e.g. "
            "2025-03-15T00:00:00+00:00). Defaults to the current UTC time when "
            "omitted — correct for write-now corrections; use an explicit date "
            "for historical fixes (e.g. the actual firmware upgrade date)."
        ),
    )
    rec.set_defaults(func=cmd_cfg_reconcile)

    # ----- cfg list -------------------------------------------------------
    lst = cfg_subparsers.add_parser(
        "list",
        help="List currently-open cfg discrepancies",
        description=(
            "Show every discrepancy that has been detected and not yet "
            "resolved. Filter by station, field, or verdict."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  receivers cfg list
  receivers cfg list ELDC THOB
  receivers cfg list --field receiver_serial receiver_firmware_version
  receivers cfg list --verdict conflict --json
        """,
    )
    lst.add_argument(
        "station",
        nargs="*",
        metavar="SID",
        help="Restrict to specific station IDs",
    )
    lst.add_argument(
        "--field",
        nargs="+",
        metavar="KEY",
        help="Restrict to specific cfg keys",
    )
    lst.add_argument(
        "--verdict",
        nargs="+",
        choices=[
            Verdict.MISSING.value,
            Verdict.CONFLICT.value,
            Verdict.SOURCES_DISAGREE.value,
        ],
        help="Restrict to specific verdicts (default: all open)",
    )
    lst.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a table"
    )
    lst.set_defaults(func=cmd_cfg_list)

    # ----- cfg history ----------------------------------------------------
    hist = cfg_subparsers.add_parser(
        "history",
        help="Show the detection/resolution history for a station or field",
        description=(
            "Print every cfg_discrepancy row matching the given station "
            "and/or field, including resolved rows. At least one of "
            "<SID> or --field is required."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  receivers cfg history ELDC
  receivers cfg history ELDC --field receiver_serial
  receivers cfg history --field receiver_firmware_version --since 30d
  receivers cfg history ELDC --json
        """,
    )
    hist.add_argument(
        "station",
        nargs="*",
        metavar="SID",
        help="Station ID to inspect (one)",
    )
    hist.add_argument(
        "--field",
        nargs="+",
        metavar="KEY",
        help="Restrict to specific cfg keys",
    )
    hist.add_argument(
        "--since",
        metavar="WHEN",
        help="Only rows detected on/after WHEN (e.g. 30d, 12h, 2026-04-01)",
    )
    hist.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum rows to return (default 200)",
    )
    hist.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a table"
    )
    hist.add_argument(
        "-v", "--verbose", action="store_true", help="Show resolution notes"
    )
    hist.set_defaults(func=cmd_cfg_history)


def handle_cfg_command(args) -> int:
    """Handle cfg subcommands; called from main()."""
    if not getattr(args, "cfg_command", None):
        print("❌ No cfg subcommand specified")
        print("Available: reconcile, list, history")
        print("\nTry: receivers cfg reconcile --help")
        return 2
    return args.func(args)
