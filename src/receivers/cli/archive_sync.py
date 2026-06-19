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
        "--config", help="Path to sync.yaml (default: GPS_CONFIG_PATH/sync.yaml)"
    )
    parser.add_argument(
        "--host", help="gps_health host override (default: from config)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON results"
    )
    parser.set_defaults(func=cmd_archive_sync)
    return parser
