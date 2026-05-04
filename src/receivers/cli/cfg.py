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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..cfg.field_manifest import FIELDS, all_keys, fields_by_key, with_position_tolerance
from ..cfg.reconciler import (
    FieldDiff,
    SourceUnavailableError,
    Verdict,
    apply_diff,
    compare_station,
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
        if not show_ok and d.verdict == Verdict.OK:
            continue
        glyph = _VERDICT_GLYPH.get(d.verdict, "?")
        print(
            f" {glyph} {d.label:<24} {_render(d.cfg_value)} "
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
  r   set to the receiver-reported value
  t   set to the TOS value
  e   enter a custom value
  k   keep the existing cfg value (skip)
  q   quit reconciliation for this station
  ?   show this help
""".rstrip()


def _interactive_prompt(diff: FieldDiff) -> Tuple[str, Optional[str]]:
    """Ask the user what to do for one field.

    Returns ``(action, value)`` where action is one of
    ``set``, ``skip``, ``quit`` and ``value`` is the chosen value
    when action is ``set``.
    """
    options: List[str] = []
    if diff.suggestion is not None:
        src = diff.suggestion_source or "?"
        options.append(f"[s]et to {diff.suggestion!r} ({src})")
    if diff.receiver_value is not None and diff.receiver_value != diff.suggestion:
        options.append(f"[r]eceiver={diff.receiver_value!r}")
    if diff.tos_value is not None and diff.tos_value != diff.suggestion:
        options.append(f"[t]os={diff.tos_value!r}")
    options.extend(["[e]dit", "[k]eep", "[q]uit", "[?]help"])

    while True:
        print(f"     {' · '.join(options)}")
        try:
            choice = input("     > ").strip().lower()
        except EOFError:
            return ("quit", None)

        if choice == "?" or choice == "help":
            print(_HELP)
            continue
        if choice in ("k", "keep", ""):
            return ("skip", None)
        if choice in ("q", "quit"):
            return ("quit", None)
        if choice in ("s", "set"):
            if diff.suggestion is None:
                print("     (no suggestion available — pick r/t/e)")
                continue
            return ("set", diff.suggestion)
        if choice in ("r", "receiver"):
            if diff.receiver_value is None:
                print("     (receiver value not available)")
                continue
            return ("set", diff.receiver_value)
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
# Per-station reconciliation
# ---------------------------------------------------------------------------


def _reconcile_one(
    station_id: str,
    station_config: Dict[str, Any],
    sources: List[str],
    fields: Optional[List[str]],
    args: argparse.Namespace,
) -> Tuple[List[FieldDiff], int, int]:
    """Reconcile one station. Returns (diffs, n_written, n_skipped)."""
    receiver_identity: Optional[Dict[str, Any]] = None
    tos_data: Optional[Dict[str, Any]] = None

    if not args.json:
        _print_setup_header(station_id, station_config)

    if "receiver" in sources:
        if station_config.get("_adhoc"):
            _progress(
                f"   ↳ {station_id}: ad-hoc config, skipping receiver probe",
                json_mode=args.json,
            )
        else:
            _progress(
                f"   ↳ {station_id}: probing receiver…",
                json_mode=args.json,
                flush=True,
            )
            receiver_identity = _query_receiver_identity(station_id, station_config)
            if receiver_identity is None:
                _progress(
                    f"   ↳ {station_id}: receiver unreachable or no identity",
                    json_mode=args.json,
                )

    if "tos" in sources:
        _progress(
            f"   ↳ {station_id}: querying TOS…",
            json_mode=args.json,
            flush=True,
        )
        tos_data = _query_tos(station_id)
        if tos_data is None:
            _progress(
                f"   ↳ {station_id}: not in TOS or TOS unavailable",
                json_mode=args.json,
            )

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
    show_ok = not args.only_diffs
    if not silent:
        _print_diff_table(diffs, show_ok=show_ok)

    n_written = 0
    n_skipped = 0
    actionable = [d for d in diffs if d.needs_attention]
    if not actionable:
        return diffs, 0, 0

    if args.dry_run:
        if not silent:
            print(f"\n   {len(actionable)} field(s) need attention (dry-run, no writes)")
        return diffs, 0, len(actionable)

    # In JSON mode without an auto-resolution flag we can't make decisions —
    # nothing to do beyond returning diffs for the caller to report.
    if silent and not (args.auto_fill or args.yes):
        return diffs, 0, 0

    field_specs_by_key = fields_by_key()

    if not silent:
        print()
    for idx, d in enumerate(actionable, start=1):
        if not silent:
            header = f"  [{idx}/{len(actionable)}] {d.cfg_key} ({d.verdict.value})"
            print(header)
            print(
                f"     cfg:      {d.cfg_value if d.cfg_value is not None else '[missing]'}"
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
        if args.auto_fill and d.verdict == Verdict.MISSING and d.suggestion is not None:
            action, new_value = "set", d.suggestion
            if not silent:
                print(f"     → auto-fill from {d.suggestion_source}: {d.suggestion!r}")
        elif args.yes and d.suggestion is not None:
            action, new_value = "set", d.suggestion
            if not silent:
                print(f"     → accept suggestion ({d.suggestion_source}): {d.suggestion!r}")
        elif silent:
            # JSON mode without an applicable auto-rule: cannot prompt; skip.
            action, new_value = "skip", None
        else:
            action, new_value = _interactive_prompt(d)

        if action == "quit":
            if not silent:
                print(f"\n     stopped at field {idx}/{len(actionable)}")
            break
        if action == "skip":
            n_skipped += 1
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
    if args.all:
        station_ids = _all_station_ids()
    elif args.station:
        station_ids = [s.upper() for s in args.station]
    else:
        print("❌ specify station IDs or --all")
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

    # Load configs
    configs = _load_station_configs(station_ids, json_mode=args.json)
    if not configs:
        return 1

    # Header — keep stdout clean in JSON mode so output stays parseable
    _progress(
        f"Reconciling {len(configs)} station(s) — sources={','.join(sources)}"
        + (f" — fields={','.join(fields)}" if fields else "")
        + (" — dry-run" if args.dry_run else "")
        + (" — auto-fill" if args.auto_fill else ""),
        json_mode=args.json,
    )

    json_collect: List[Dict[str, Any]] = []
    total_written = 0
    total_skipped = 0
    n_with_issues = 0

    for sid in station_ids:
        cfg = configs.get(sid)
        if cfg is None:
            continue
        try:
            diffs, n_written, n_skipped = _reconcile_one(
                sid, cfg, sources, fields, args
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
  receivers cfg reconcile --all --auto-fill --field receiver_serial
  receivers cfg reconcile --all --dry-run --json
  receivers cfg reconcile --list-fields
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
    rec.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
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
