"""``receivers archive-sync`` — host-level batch delta sweep to the archive.

Reads ``sync.yaml``, computes each active target's watermark-bounded delta,
rsyncs raw files to the archive gateway (``gpsops@rawdata:~/gpsdata``), and
forward-indexes them into ``archive_catalog``. Dry-run-safe; the live push and
the legacy-cutover are operator actions. See design 1781867391.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("receivers.cli.archive_sync")


def _get_conn(host: Optional[str], required: bool):
    """Open a gps_health connection; tolerate absence only when not required."""
    try:
        from ..db.connection import get_connection

        return get_connection(host_override=host)
    except Exception as exc:  # noqa: BLE001 - dev laptops may lack gps_health
        if required:
            raise
        logger.warning("no gps_health connection (%s) — dry-run without indexing", exc)
        return None


def cmd_archive_sync(args: argparse.Namespace) -> int:
    from ..archive import ArchiveSync, load_sync_config

    config_path = Path(args.config) if args.config else None
    targets = load_sync_config(config_path)
    if args.target:
        targets = [t for t in targets if t.name == args.target]
        if not targets:
            print(f"No sync target named {args.target!r} in config")
            return 1

    if not targets:
        print("No sync targets configured (sync.yaml absent or empty).")
        return 0

    if args.status:
        return _cmd_status(args, targets)

    cutover_override = None
    if args.cutover:
        from ..archive.config import _parse_cutover

        try:
            cutover_override = _parse_cutover(args.cutover, "cli")
        except ValueError as exc:
            print(f"Invalid --cutover: {exc}")
            return 1

    # The catalog + watermark need a DB; a pure dry-run can run without one.
    conn = _get_conn(args.host, required=not args.dry_run)

    results = []
    exit_code = 0
    try:
        for target in targets:
            engine = ArchiveSync(
                target,
                conn=conn,
                dry_run=args.dry_run,
                dest_override=args.dest_override,
                force=args.force,
                cutover_override=cutover_override,
            )
            result = engine.run()
            results.append(result)
            if not result.ok:
                exit_code = 1
            if not args.json:
                _print_result(result)
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(
            json.dumps(
                [_result_dict(r) for r in results], indent=2, default=_json_default
            )
        )
    return exit_code


def _cmd_status(args: argparse.Namespace, targets) -> int:
    """Print per-target sync freshness; non-zero exit if any target is alerting."""
    from datetime import datetime

    from ..archive.freshness import evaluate_all

    conn = _get_conn(args.host, required=True)
    try:
        statuses = evaluate_all(
            conn, targets, now=datetime.now(), max_age_minutes=args.max_age_minutes
        )
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(
            json.dumps(
                [_status_dict(s) for s in statuses], indent=2, default=_json_default
            )
        )
    else:
        for s in statuses:
            icon = {"ok": "✅", "stale": "🔴", "never": "⚠️", "inactive": "⏸️"}.get(
                s.state, "?"
            )
            age = (
                f"{s.age_seconds / 60:.0f} min ago"
                if s.age_seconds is not None
                else "—"
            )
            last = f"{s.last_success:%Y-%m-%d %H:%M:%S}" if s.last_success else "never"
            print(f"{icon} {s.target}: {s.state}  (last success {last}, {age})")
    return 1 if any(s.is_alerting for s in statuses) else 0


def _status_dict(s) -> dict:
    return {
        "target": s.target,
        "state": s.state,
        "last_success": s.last_success,
        "age_seconds": s.age_seconds,
        "threshold_seconds": s.threshold_seconds,
    }


def _print_result(result) -> None:
    icon = "✅" if result.ok else "❌"
    tag = " [dry-run]" if result.dry_run else ""
    print(f"{icon} {result.target}{tag}: {result.message}")
    print(
        f"   floor={result.floor:%Y-%m-%d %H:%M:%S}  delta={result.delta_count}  "
        f"transferred={result.transferred}  cataloged={result.cataloged}"
    )
    for err in result.errors:
        print(f"   ⚠ {err}")


def _result_dict(result) -> dict:
    return {
        "target": result.target,
        "floor": result.floor,
        "delta_count": result.delta_count,
        "transferred": result.transferred,
        "cataloged": result.cataloged,
        "ok": result.ok,
        "dry_run": result.dry_run,
        "message": result.message,
        "errors": result.errors,
    }


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def cmd_archive_verify(args: argparse.Namespace) -> int:
    """Verify archived files against the catalog (local↔archive + read-back)."""
    from ..archive import load_sync_config, verify_archive_catalog

    config_path = Path(args.config) if args.config else None
    targets = load_sync_config(config_path)
    target = None
    if targets:
        target = next(
            (t for t in targets if t.name == args.storage_location), targets[0]
        )
    # dest_prefix lets us map the stored archive path onto the local read mount.
    dest_prefix = args.dest_prefix or (target.dest if target else None)

    conn = _get_conn(args.host, required=True)
    try:
        stats = verify_archive_catalog(
            conn,
            storage_location=args.storage_location,
            read_root=args.read_root,
            dest_prefix=dest_prefix,
            limit=args.limit,
            reverify_after_days=args.reverify_after_days,
        )
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(json.dumps(stats.to_dict(), indent=2))
    else:
        icon = "❌" if (stats.mismatched or stats.local_divergent) else "✅"
        mode = "read-back" if stats.read_back else "cross-check only"
        print(f"{icon} archive verify ({mode}): {stats.checked} checked")
        print(
            f"   verified={stats.verified}  CORRUPT={stats.mismatched}  "
            f"local-divergent={stats.local_divergent}  missing={stats.missing}"
        )
        for f in stats.findings[:50]:
            print(f"   ⚠ {f}")
    # Non-zero exit on any real integrity problem.
    return 1 if (stats.mismatched or stats.local_divergent) else 0


def cmd_archive_reindex(args: argparse.Namespace) -> int:
    """Re-hash files in a staging mirror and refresh their archive_catalog rows.

    For files modified out-of-band (e.g. ``rinex --fix-headers --push``): the
    archive bytes changed but the catalog still holds the pre-edit
    content_sha256. Point ``--dir`` at the staging mirror that was pushed (its
    bytes are identical to what landed on the archive) and this upserts the
    correct hash. Laptop-friendly — no archive mount needed.
    """
    import os

    from ..archive import load_sync_config, reindex_files

    root = str(Path(args.dir).expanduser())
    if not os.path.isdir(root):
        print(f"❌ --dir not a directory: {root}")
        return 2

    # Resolve the archive target for storage_location + dest prefix.
    config_path = Path(args.config) if args.config else None
    targets = load_sync_config(config_path)
    target = next(
        (t for t in targets if t.name == args.storage_location),
        next((t for t in targets if getattr(t, "tier", None) == "archive"), None),
    )
    if target is None:
        print("❌ no archive target in sync.yaml (need storage_location + dest)")
        return 2
    dest_prefix = args.dest_prefix or target.dest

    # Collect files under the mirror (skip *_archive backup dirs).
    files: list[str] = []
    for dirpath, _dirs, names in os.walk(root):
        if f"{os.sep}rinex_archive{os.sep}" in dirpath + os.sep:
            continue
        for n in names:
            files.append(os.path.join(dirpath, n))
    if not files:
        print(f"⚠️  no files under {root}")
        return 0

    if not args.host:
        print("⚠️  --host not set → writing to the DEFAULT gps_health "
              "(localhost = DEV catalog on a laptop, NOT production).")
        print("   Pass --host pgdev.vedur.is to update the production catalog.")

    conn = _get_conn(args.host, required=True)
    try:
        stats = reindex_files(
            conn,
            files,
            root=root,
            storage_location=target.name,
            dest_prefix=dest_prefix,
            dry_run=args.dry_run,
            only_existing=args.only_existing,
        )
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        print(json.dumps(stats.to_dict(), indent=2))
    else:
        host_label = args.host or "localhost"
        verb = "would reindex" if args.dry_run else "reindexed"
        print(
            f"↻ {verb} archive_catalog ({target.name} on {host_label}): "
            f"{stats.updated} updated, {stats.inserted} inserted, "
            f"{stats.unchanged} unchanged"
            + (f", {stats.skipped_new} skipped (no prior row)" if stats.skipped_new else "")
            + (f", {stats.skipped} unparsable" if stats.skipped else "")
        )
        for msg in stats.errors[:50]:
            print(f"   ⚠ {msg}")
    return 1 if stats.errors else 0


def create_archive_reindex_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "archive-reindex",
        help="Refresh archive_catalog content_sha256 for out-of-band file edits",
        description=(
            "Re-hash the files in a staging mirror (the --work-dir that "
            "`rinex --fix-headers --push` pushed from) and upsert their "
            "archive_catalog rows, so the catalog matches the edited archive "
            "bytes and the integrity verify stops flagging them. Runs from a "
            "laptop; pass --host pgdev.vedur.is for the production catalog."
        ),
    )
    parser.add_argument(
        "--dir",
        required=True,
        help="Staging mirror root (archive layout YYYY/mon/STA/session/cat/FILE), "
        "e.g. ~/tmp/rinex_fixes",
    )
    parser.add_argument(
        "--storage-location",
        default="imo_archive",
        help="archive_catalog.storage_location to write (default: imo_archive)",
    )
    parser.add_argument(
        "--dest-prefix",
        help="Archive dest prefix for file_path (default: target.dest from sync.yaml)",
    )
    parser.add_argument(
        "--host",
        help="gps_health host (pgdev.vedur.is for production; default: database.cfg, "
        "which is localhost/DEV on a laptop)",
    )
    parser.add_argument(
        "--config", help="Path to sync.yaml (default: GPS_CONFIG_PATH/sync.yaml)"
    )
    parser.add_argument(
        "--only-existing",
        action="store_true",
        help="Only repair rows that already exist (skip inserts) — fix known-stale "
        "sha256 without expanding catalog coverage to previously-uncataloged files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify + report without writing catalog rows",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON results"
    )
    parser.set_defaults(func=cmd_archive_reindex)
    return parser


def create_archive_verify_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "archive-verify",
        help="Verify archived files against the catalog (re-hash + local compare)",
        description=(
            "Re-hash files on the long-term archive and compare to the stored "
            "content_sha256 (read-back, needs --read-root = the archive mount), and "
            "cross-check local file_tracking hashes vs the catalog. Detects "
            "archive bit-rot / partial transfers and local↔archive divergence."
        ),
    )
    parser.add_argument(
        "--storage-location",
        default="imo_archive",
        help="archive_catalog.storage_location to verify (default: imo_archive); "
        "also selects the sync target whose dest maps to --read-root",
    )
    parser.add_argument(
        "--read-root",
        help="Local mount of the archive for read-back re-hashing "
        "(rek-d01: /mnt/rawgpsdata). Omit for the DB-only cross-check.",
    )
    parser.add_argument(
        "--dest-prefix",
        help="Archive dest prefix stored in file_path (default: target.dest from "
        "sync.yaml), swapped for --read-root to locate each file",
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="Max catalog rows per run (default: 500)"
    )
    parser.add_argument(
        "--reverify-after-days",
        type=int,
        help="Re-verify rows whose last_verified_at is older than N days "
        "(default: only never-verified rows)",
    )
    parser.add_argument(
        "--config", help="Path to sync.yaml (default: GPS_CONFIG_PATH/sync.yaml)"
    )
    parser.add_argument(
        "--host", help="gps_health host override (default: from config)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON results"
    )
    parser.set_defaults(func=cmd_archive_verify)
    return parser


def create_archive_sync_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "archive-sync",
        help="Batch delta sync of raw files to the long-term archive gateway",
        description=(
            "Push raw files newer than each target's watermark to the archive "
            "(rawdata/ananas) and index them in archive_catalog. Reads sync.yaml."
        ),
    )
    parser.add_argument(
        "--target", help="Only run this target (default: all active targets)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview: rsync --dry-run, no catalog writes, no watermark advance",
    )
    parser.add_argument(
        "--dest-override",
        help="Override the remote dest path (e.g. ~/gpsdata_staging for pre-stage)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if the target is inactive (for the pre-stage verify before "
        "the cutover). The scheduled :45 job is unaffected — it only runs active targets.",
    )
    parser.add_argument(
        "--cutover",
        help="Override the target's cutover (ISO ts, e.g. 2026-06-18T00:00:00) for "
        "this run — e.g. a pre-stage verify with a recent cutover so there is a "
        "real delta. Does not change sync.yaml.",
    )
    parser.add_argument(
        "--config", help="Path to sync.yaml (default: GPS_CONFIG_PATH/sync.yaml)"
    )
    parser.add_argument(
        "--host", help="gps_health host override (default: from config)"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show per-target sync freshness instead of syncing "
        "(non-zero exit if any target is stale/never)",
    )
    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=120,
        help="Freshness threshold for --status (default: 120)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON results"
    )
    parser.set_defaults(func=cmd_archive_sync)
    return parser
