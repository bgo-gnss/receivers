"""``receivers archive-truncation plan|apply`` — find and repair truncated raw.

    receivers archive-truncation plan --start 2026-10-17 --end 2026-11-15 -o plan.json
    receivers archive-truncation apply plan.json                 # dry-run: re-checks only
    receivers archive-truncation apply plan.json --catalog-prod --yes

The decision ladder (first match wins) and the safety rules are in
:mod:`receivers.archive.truncation` and :mod:`receivers.archive.truncation_apply`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_ICON = {
    "repaired": "✅",
    "would_apply": "↻",
    "refused": "⛔",
    "failed": "❌",
    "deferred": "⏸",
    "unreachable": "⏸",
    "not_on_receiver": "∅",
    "recorded": "📝",
}


def _data_prepath() -> str:
    from ..config.receivers_config import get_receivers_config

    return get_receivers_config().get_data_prepath()


def _read_root(cli_value):
    from .archive_sync import _read_root_looks_mounted, _resolve_read_root

    root = _resolve_read_root(cli_value)
    if not root or not os.path.isdir(root) or not _read_root_looks_mounted(root):
        return None
    return root


def cmd_plan(args: argparse.Namespace) -> int:
    from ..archive.truncation_scan import build_plan, format_plan
    from ..db.connection import get_connection

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = (
        date.fromisoformat(args.start)
        if args.start
        else end - timedelta(days=args.days - 1)
    )
    if start > end:
        print("❌ --start is after --end")
        return 2
    archive_root = _read_root(args.read_root)
    if archive_root is None:
        print(
            "❌ archive read root missing or unmounted (--read-root / archive_verify.read_root)"
        )
        return 2
    local_root = args.local_root or _data_prepath()
    stations = {s.upper() for s in args.stations} if args.stations else None

    conn = get_connection(host_override=args.host, single_host=True)
    try:
        plan = build_plan(
            conn,
            local_root=local_root.rstrip("/"),
            archive_root=archive_root.rstrip("/"),
            start=start,
            end=end,
            stations=stations,
            sessions=args.sessions,
            deep=args.deep,
            settle_hours=args.settle_hours,
            horizon_max_age_hours=args.horizon_max_age_hours,
        )
    finally:
        conn.close()

    if args.output:
        with open(args.output, "w") as fh:
            fh.write(plan.to_json())
    if args.json:
        print(plan.to_json())
        return 0
    print(
        f"archive-truncation plan {start}..{end}  archive={plan.archive_root} local={plan.local_root}"
    )
    print("   " + json.dumps(plan.stats, sort_keys=True))
    if plan.items:
        print(format_plan(plan))
    else:
        print("✅ no truncated or corrupt raw files found")
    if args.output:
        print(
            f"plan written to {args.output} — review, then: receivers archive-truncation apply {args.output}"
        )
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    from ..archive.config import load_sync_config
    from ..archive.reindex import resolve_catalog_hosts
    from ..archive.truncation_apply import (
        ApplyConfig,
        apply_plan,
        default_local_quarantine,
    )
    from ..archive.truncation_scan import Plan

    plan = Plan.load(args.plan)
    targets = load_sync_config()
    target = next((t for t in targets if t.name == args.storage_location), None)
    if target is None:
        print(f"❌ no sync.yaml target named {args.storage_location!r}")
        return 2
    hosts = resolve_catalog_hosts(args.catalog_host, prod=args.catalog_prod)
    if args.yes and args.catalog_prod and not hosts:
        print("❌ --catalog-prod but [archive] catalog_hosts is unset — refusing")
        return 2
    if args.yes and not os.path.isdir(plan.archive_root):
        print(f"❌ the plan's archive root {plan.archive_root} is not mounted here")
        return 2

    cfg = ApplyConfig(
        ssh_target=f"{target.user}@{target.host}",
        dest_root=target.dest,
        catalog_hosts=hosts,
        local_quarantine=args.local_quarantine or default_local_quarantine(),
        archive_quarantine=args.archive_quarantine,
        rinex=not args.no_rinex,
        max_items=args.max_items,
    )
    try:
        res = apply_plan(plan, cfg, execute=args.yes)
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return 2
    for r in res.results:
        line = f"{_ICON.get(r.outcome, '?')} {r.outcome:<15} {r.action:<13} {r.rel}"
        if r.detail:
            line += f"  — {r.detail}"
        print(line)
    if not args.yes:
        print(
            "dry-run: nothing written. Re-run with --yes (and --catalog-prod) to apply."
        )
    else:
        print(
            f"run {cfg.run_id}: archive quarantine {cfg.archive_quarantine}/{cfg.run_id}, "
            f"local quarantine {cfg.local_quarantine}/{cfg.run_id}"
        )
    return 0 if res.ok else 1


def create_archive_truncation_parser(subparsers) -> argparse.ArgumentParser:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "archive-truncation",
        help="Find and repair truncated raw .gz files (plan, then apply)",
        description=(
            "Deterministic repair of truncated raw files. A complete copy elsewhere "
            "is used first; then the receiver (if it still holds the period); then "
            "the CRC-valid SBF prefix is salvaged and recorded as partial. An "
            "unreachable receiver or stale retention info DEFERS — it never salvages "
            "while the full file may still exist."
        ),
    )
    sub = p.add_subparsers(dest="truncation_cmd", required=True)

    pp = sub.add_parser("plan", help="Scan and write a reviewable plan (read-only)")
    pp.add_argument("--start", help="First file date (YYYY-MM-DD)")
    pp.add_argument("--end", help="Last file date, inclusive (default: yesterday)")
    pp.add_argument(
        "--days", type=int, default=30, help="Window when --start is omitted"
    )
    pp.add_argument("--stations", nargs="+", help="Limit to these stations")
    pp.add_argument(
        "--sessions", nargs="+", default=["15s_24hr", "1Hz_1hr", "status_1hr"]
    )
    pp.add_argument(
        "--deep",
        action="store_true",
        help="Decompress every file in the window, not only uncataloged/size-mismatched ones",
    )
    pp.add_argument(
        "--settle-hours",
        type=float,
        default=6.0,
        help="Skip files modified more recently than this (may be in flight)",
    )
    pp.add_argument(
        "--horizon-max-age-hours",
        type=float,
        default=48.0,
        help="Older receiver_horizon observations count as unknown (deferred)",
    )
    pp.add_argument(
        "--read-root", help="Archive read-only mount (default archive_verify.read_root)"
    )
    pp.add_argument("--local-root", help="Local collection root (default data_prepath)")
    pp.add_argument(
        "--host", help="gps_health host to read facts from (default database.cfg)"
    )
    pp.add_argument("-o", "--output", help="Write the plan JSON here")
    pp.add_argument("--json", action="store_true", help="Print the plan JSON")
    pp.set_defaults(func=cmd_plan)

    pa = sub.add_parser("apply", help="Apply a plan (dry-run unless --yes)")
    pa.add_argument("plan", help="Plan JSON from `plan -o`")
    pa.add_argument("--yes", action="store_true", help="Actually write")
    pa.add_argument(
        "--catalog-prod",
        action="store_true",
        help="Write the production catalog set (rek-d01 + pgdev)",
    )
    pa.add_argument("--catalog-host", help="Explicit catalog host(s), comma-separated")
    pa.add_argument("--storage-location", default="imo_archive")
    pa.add_argument(
        "--max-items", type=int, default=50, help="Cap per run (rawdata is single-core)"
    )
    pa.add_argument(
        "--no-rinex", action="store_true", help="Do not convert repaired periods"
    )
    pa.add_argument(
        "--local-quarantine",
        default=None,
        help="Where old local copies are kept "
        "(default ~/.cache/gps_receivers/truncation_quarantine)",
    )
    pa.add_argument("--archive-quarantine", default="~/quarantine/truncation")
    pa.set_defaults(func=cmd_apply)
    return p
