"""``receivers stream-splice`` — backfill 15s dailies from archived 1 Hz stream hours.

Plans by default (no writes). ``--build`` splices the planned days into a
staging tree and validates them; ``--push`` also pushes them through the
rawdata gateway and reindexes the catalog. See
:mod:`receivers.streaming.splice_backfill` for what each day is classified as
and why.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

#: Archive read roots, first existing wins (rek-d01, laptop). The local rolling
#: window (/mnt/data/gpsdata) is deliberately NOT a candidate: history is not
#: there, and splicing from a partial window would rebuild days short.
ARCHIVE_READ_ROOTS = ("/mnt/rawgpsdata", "/mnt_data/rawgpsdata")


def _parse_day(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {value!r}")


def _read_root(args) -> Optional[Path]:
    if args.source_dir:
        return Path(args.source_dir)
    for cand in ARCHIVE_READ_ROOTS:
        if Path(cand).is_dir():
            return Path(cand)
    return None


def _refresh_local_copy(staged: Path, rel: str, local_root: Path) -> bool:
    """Replace the rolling window's copy of a just-pushed daily, if it has one.

    rek-d01 keeps the recent archive locally, so a truncated daily that was
    rebuilt on the archive still sits there truncated; the cross-checks would
    then see local and archive disagree. Only a stream product is replaced, and
    the staged file's mtime is kept so archive-sync (``--update``) sees the two
    as the same file. Returns True when a copy was replaced.
    """
    from ..streaming.downsample import is_stream_product, read_rinex_pgm

    local = local_root / rel
    if not local.is_file() or not is_stream_product(read_rinex_pgm(local)):
        return False
    landing = local.with_name(f".{local.name}.tmp")
    shutil.copy2(staged, landing)
    os.replace(landing, local)
    return True


def cmd_stream_splice(args: argparse.Namespace) -> int:
    from ..streaming.downsample import RinexDownsampler
    from ..streaming.splice_backfill import (
        BUILD_REASONS,
        build_day,
        make_tos_header_hook,
        plan_station,
        summarise,
    )

    root = _read_root(args)
    if root is None:
        print(f"❌ no archive read root found ({', '.join(ARCHIVE_READ_ROOTS)});")
        print("   pass --source-dir")
        return 2
    reasons = set(BUILD_REASONS)
    if args.no_convert_r3:
        reasons.discard("stream_r3")
    stations = [s.upper() for s in args.stations]
    print(
        f"📂 reading {root}  (inclusive {args.start or 'first'} .. {args.end or 'last'})"
    )

    plans = []
    rerinex = 0
    for sta in stations:
        sp = plan_station(
            root, sta, args.start, args.end, convert_r3=not args.no_convert_r3
        )
        counts = summarise(sp)
        todo = [p for p in sp if p.reason in reasons]
        print(
            f"  {sta}: {len(sp)} day(s) with 1 Hz hours → "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
        if args.verbose:
            for p in sp:
                if p.reason not in ("complete", "authoritative", "no_source"):
                    print(f"      {p.day}  {p.reason:<18} {p.detail}")
        plans.extend(todo)
        rerinex += counts.get("rerinex", 0)
    if args.limit:
        plans = plans[: args.limit]
    print(f"🧩 {len(plans)} day(s) to splice")
    if rerinex:
        print(
            f"   ({rerinex} day(s) have 15 s raw and no daily — convert with `rinex`)"
        )
    if not (args.build or args.push) or not plans:
        if plans:
            print("   plan only — add --build to stage + validate, --push to publish")
        return 0

    work = Path(args.work_dir).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    print(f"🏗  staging into {work}")
    ds = RinexDownsampler(interval=args.interval)
    caches: dict = {}
    configs: dict = {}
    built: List = []
    failed = 0
    for p in plans:
        hook = None
        if not args.no_header_fix:
            if p.station not in configs:
                try:
                    from ..config_utils import get_station_config

                    configs[p.station] = get_station_config(p.station)
                except Exception:  # noqa: BLE001 - TOS path still applies
                    configs[p.station] = None
            hook = make_tos_header_hook(
                p.station,
                p.day,
                station_config=configs[p.station],
                tos_metadata_cache=caches.setdefault(p.station, {}),
            )
        out = build_day(p, work, ds, header_hook=hook)
        tag = f"{p.station} {p.day} {p.reason}"
        if out.ok:
            built.append(out)
            old = f" (was {out.old_epochs})" if out.old_epochs is not None else ""
            print(f"   ✓ {tag}: {out.epochs} epochs with satellites{old}")
        else:
            failed += 1
            print(f"   ✗ {tag}: {out.error}")
    print(f"🧪 built {len(built)}, refused/failed {failed}")
    if not args.push or not built:
        if built:
            print(f"   staged in {work} — re-run with --push to publish")
        return 1 if failed else 0

    from .main import _push_reconverted

    rel = [str(o.staged.relative_to(work)) for o in built]
    verified = {str(o.staged.relative_to(work)) for o in built if o.plan.daily}
    push_args = argparse.Namespace(
        dry_run=False,
        fix_headers=False,
        from_archive=False,
        source_dir=str(root),  # arms both push gates against the archive copy
        backup_old=True,  # local pre-overwrite copies → <work>_prev/
        catalog_prod=args.catalog_prod,
        catalog_host=args.catalog_host,
        reindex=args.reindex,
    )
    stats = _push_reconverted(
        work, push_args, logger, only_rel=rel, verified_replacements=verified
    )
    if stats.get("rc") != 0:
        print(f"❌ push incomplete: {stats.get('note')}")
        return 1

    if args.local_root:
        local_root = Path(args.local_root)
        refreshed = sum(
            _refresh_local_copy(o.staged, r, local_root)
            for o, r in zip(built, rel)
            if local_root.is_dir()
        )
        if refreshed:
            print(
                f"🔁 refreshed {refreshed} rolling-window copy(ies) under {local_root}"
            )

    # Pushed and catalogued: the staged copies are redundant now. The local
    # pre-overwrite copies (<work>_prev) stay for the verification window.
    for o in built:
        o.staged.unlink(missing_ok=True)
    print(f"🧹 staged files removed; pre-overwrite copies kept in {work}_prev")
    return 1 if failed else 0


def create_stream_splice_parser(subparsers) -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "stream-splice",
        help="Backfill 15s dailies (RINEX 2.11) from archived 1 Hz stream hours",
        description=(
            "Splice archived 1 Hz BNC stream hours into 15 s daily RINEX 2.11 "
            "for days the live stream cycle never finished: no daily (missing), "
            "a daily frozen part-way through its day (truncated), or a complete "
            "BNC daily in RINEX 3.01 (stream_r3, GAMIT discards R3). Never "
            "touches a daily from any other program, or a day whose 15 s raw "
            "is archived. Plans by default; --build stages + validates; --push "
            "publishes via the rawdata gateway and reindexes."
        ),
    )
    parser.add_argument("stations", nargs="+", help="Station IDs (e.g. SEY9 GONH)")
    parser.add_argument(
        "-s", "--start", type=_parse_day, help="First day, inclusive (YYYY-MM-DD)"
    )
    parser.add_argument(
        "-e", "--end", type=_parse_day, help="Last day, INCLUSIVE (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--source-dir",
        help=f"Archive read root (default: first of {', '.join(ARCHIVE_READ_ROOTS)})",
    )
    parser.add_argument(
        "--work-dir",
        default="~/tmp/stream_splice",
        help="Staging tree (default ~/tmp/stream_splice; on rek-d01 use "
        "/mnt/data/gpsops_scratch/stream_splice — /home is small)",
    )
    parser.add_argument(
        "--local-root",
        help="Rolling-window root whose stream-daily copies are refreshed after "
        "a push (rek-d01: /mnt/data/gpsdata)",
    )
    parser.add_argument("--interval", type=int, default=15, help="Sample seconds")
    parser.add_argument(
        "--no-convert-r3",
        action="store_true",
        help="Leave complete RINEX 3.01 BNC dailies alone",
    )
    parser.add_argument(
        "--no-header-fix",
        action="store_true",
        help="Keep BNC's skeleton header (default: correct it from TOS/config)",
    )
    parser.add_argument("--limit", type=int, help="Splice at most N days")
    parser.add_argument("--build", action="store_true", help="Stage + validate")
    parser.add_argument(
        "--push", action="store_true", help="Build, push to the archive, reindex"
    )
    parser.add_argument(
        "--catalog-prod",
        action="store_true",
        help="Reindex every [archive] catalog_hosts host after the push",
    )
    parser.add_argument("--catalog-host", help="Reindex this catalog host instead")
    parser.add_argument(
        "--reindex", action="store_true", help="Reindex the default gps_health"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.set_defaults(func=cmd_stream_splice)
    return parser
