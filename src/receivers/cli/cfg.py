"""``receivers cfg reconcile`` — three-way reconciliation CLI.

Compares values in ``stations.cfg`` against:

* the live receiver (via the health pipeline)
* TOS (via :mod:`tostools`)

The intended workflow is **TOS → cfg**: TOS is authoritative; the live
receiver is a validation source that flags issues for human review. The
silent auto-write that ``receivers health`` used to perform has been
removed in favour of this explicit, reviewable workflow.

Subcommands:

* ``reconcile``  — show diffs and (optionally) write fixes to stations.cfg
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..cfg.field_manifest import FIELDS, all_keys
from ..cfg.reconciler import (
    FieldDiff,
    SourceUnavailableError,
    Verdict,
    apply_diff,
    compare_station,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source acquisition
# ---------------------------------------------------------------------------


def _load_station_configs(station_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Return ``{station_id: station_config}`` for the requested stations."""
    from ..config_utils import get_station_config

    configs: Dict[str, Dict[str, Any]] = {}
    for sid in station_ids:
        cfg = get_station_config(sid)
        if cfg is None:
            print(f"⚠️  {sid}: not found in stations.cfg — skipping")
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
        identity = health.get("receiver_identity") if isinstance(health, dict) else None
        if not identity:
            logger.debug("[%s] receiver returned no identity dict", station_id)
            return None
        return identity
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] receiver probe failed: %s", station_id, exc)
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
            print(f"   ↳ {station_id}: ad-hoc config, skipping receiver probe")
        else:
            print(f"   ↳ {station_id}: probing receiver…", flush=True)
            receiver_identity = _query_receiver_identity(station_id, station_config)
            if receiver_identity is None:
                print(f"   ↳ {station_id}: receiver unreachable or no identity")

    if "tos" in sources:
        print(f"   ↳ {station_id}: querying TOS…", flush=True)
        tos_data = _query_tos(station_id)
        if tos_data is None:
            print(f"   ↳ {station_id}: not in TOS or TOS unavailable")

    diffs = compare_station(
        station_id=station_id,
        station_config=station_config,
        receiver_identity=receiver_identity,
        tos_data=tos_data,
        fields=fields,
        queried_sources=set(sources) | {"cfg"},
    )

    show_ok = not args.only_diffs
    if not args.json:
        _print_diff_table(diffs, show_ok=show_ok)

    if args.json:
        return diffs, 0, 0

    n_written = 0
    n_skipped = 0
    actionable = [d for d in diffs if d.needs_attention]
    if not actionable:
        return diffs, 0, 0

    if args.dry_run:
        print(f"\n   {len(actionable)} field(s) need attention (dry-run, no writes)")
        return diffs, 0, len(actionable)

    print()
    for idx, d in enumerate(actionable, start=1):
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
            print(f"     → auto-fill from {d.suggestion_source}: {d.suggestion!r}")
        elif args.yes and d.suggestion is not None:
            action, new_value = "set", d.suggestion
            print(f"     → accept suggestion ({d.suggestion_source}): {d.suggestion!r}")
        else:
            action, new_value = _interactive_prompt(d)

        if action == "quit":
            print(f"\n     stopped at field {idx}/{len(actionable)}")
            break
        if action == "skip":
            n_skipped += 1
            continue
        if action == "set" and new_value is not None:
            try:
                changed = apply_diff(station_id, d, new_value)
                if changed:
                    n_written += 1
                    print(f"     ✅ wrote {d.cfg_key} = {new_value!r}")
                else:
                    print(f"     ⏭  unchanged ({d.cfg_key} already = {new_value!r})")
            except SourceUnavailableError as exc:
                print(f"     ❌ could not write: {exc}")
            except Exception as exc:  # noqa: BLE001
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
            print("⚠️  tostools not installed — disabling TOS source")
            sources = [s for s in sources if s != "tos"]
            if not sources:
                print("❌ no usable sources remain")
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
    configs = _load_station_configs(station_ids)
    if not configs:
        return 1

    # Header
    print(
        f"Reconciling {len(configs)} station(s) — sources={','.join(sources)}"
        + (f" — fields={','.join(fields)}" if fields else "")
        + (" — dry-run" if args.dry_run else "")
        + (" — auto-fill" if args.auto_fill else "")
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
            json_collect.append(
                {
                    "station_id": sid,
                    "diffs": [d.as_dict() for d in diffs],
                    "summary": _summary_counts(diffs),
                }
            )

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
    rec.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    rec.set_defaults(func=cmd_cfg_reconcile)


def handle_cfg_command(args) -> int:
    """Handle cfg subcommands; called from main()."""
    if not getattr(args, "cfg_command", None):
        print("❌ No cfg subcommand specified")
        print("Available: reconcile")
        print("\nTry: receivers cfg reconcile --help")
        return 2
    return args.func(args)
