"""``receivers epos-disseminate`` — EPOS RINEX3 long-name dissemination.

Drives the whole phase-1 chain for ``--station``/``--date``: convert the archived
RINEX (or raw) to a RINEX 3.04 long-name file, QC the header against TOS, push to
the dissemination target, and index the pushed file into the EPOS DB. Also:
``--list-stations`` (TOS EPOS filter) and ``--refresh-metadata`` (TOS→EPOS station
ETL). Dry-run-safe; the target is gated ``active: false`` in sync.yaml.
See docs/architecture/epos-dissemination-plan.md.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("receivers.cli.epos_disseminate")


def _result_dict(r) -> dict:
    return {
        "station": r.station,
        "file_date": r.file_date.isoformat(),
        "ok": r.ok,
        "dry_run": r.dry_run,
        "source_path": r.source_path,
        "long_name": r.long_name,
        "cached": r.cached,
        "pushed": r.pushed,
        "artifact_path": r.artifact_path,
        "qc_passed": r.qc_passed,
        "qc_message": r.qc_message,
        "dest": r.dest,
        "message": r.message,
        "errors": r.errors,
    }


def _epos_conn():
    """Open an EPOS DB connection (``[epos_db]`` / ``EPOS_DB_*`` env), or None."""
    try:
        from ..dissemination import epos_db

        return epos_db.connect()
    except Exception as exc:  # noqa: BLE001 - DB optional for dry-run / no-index
        logger.warning("no EPOS DB connection (%s) — metadata/index steps skipped", exc)
        return None


def _cmd_refresh_metadata(args: argparse.Namespace) -> int:
    """Run the TOS→EPOS station metadata ETL for --station (or all EPOS stations)."""
    from ..dissemination import run_etl

    conn = _epos_conn()
    if conn is None:
        print("No EPOS DB connection — cannot refresh metadata.")
        return 1
    try:
        markers = [args.station] if args.station else None
        res = run_etl(conn, markers=markers)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(res.__dict__, indent=2))
    else:
        print(
            f"metadata ETL: {res.stations} station(s) "
            f"({res.inserted} inserted, {res.updated} updated)"
        )
        for err in res.errors:
            print(f"   ⚠ {err}")
    return 0 if not res.errors else 1


def cmd_epos_disseminate(args: argparse.Namespace) -> int:
    from ..dissemination import EposDisseminate, load_dissemination_config

    config_path = Path(args.config) if args.config else None
    targets = load_dissemination_config(config_path)
    if args.target:
        targets = [t for t in targets if t.name == args.target]
    if not targets:
        print(
            "No dissemination targets configured (sync.yaml has no tier:dissemination)."
        )
        return 1
    target = targets[0]

    # --list-stations: print the EPOS-eligible set from TOS and exit.
    if args.list_stations:
        from ..dissemination import epos_markers

        markers = epos_markers()
        if args.json:
            print(json.dumps(markers, indent=2))
        else:
            print(f"{len(markers)} EPOS-eligible stations:")
            print(" ".join(markers))
        return 0

    # --refresh-metadata: TOS→EPOS station ETL (no file pipeline).
    if args.refresh_metadata:
        return _cmd_refresh_metadata(args)

    if not args.station or not args.date:
        print(
            "--station and --date are required "
            "(or use --list-stations / --refresh-metadata)"
        )
        return 1

    if not target.active and not args.force:
        print(
            f"Target {target.name!r} is inactive — pass --force for a pre-stage run "
            f"(use --dest-override to a staging path)."
        )
        return 1

    try:
        d = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        print(f"Invalid --date {args.date!r} (expected YYYY-MM-DD)")
        return 1

    # The header-QC gate runs by default (live TOS session provider); --no-qc
    # disables it (e.g. an offline pre-stage where TOS is unreachable).
    session_provider = None
    if not args.no_qc:
        from ..dissemination import make_session_provider

        session_provider = make_session_provider()

    engine = EposDisseminate(
        target,
        dry_run=args.dry_run,
        dest_override=args.dest_override,
        session_provider=session_provider,
    )
    result = engine.run_one(args.station, d)

    # Index-on-push: record the pushed file in the EPOS rinex_file table (T4).
    # Best-effort and only for a real (non-dry-run) successful push; needs the
    # station row (run --refresh-metadata first). --no-index disables it.
    indexed_id = None
    if result.ok and not args.dry_run and not args.no_index and result.artifact_path:
        conn = _epos_conn()
        if conn is not None:
            try:
                from ..dissemination import index_rinex_file

                rel = f"/files/{result.station}/{result.long_name}"
                indexed_id = index_rinex_file(
                    conn,
                    Path(result.artifact_path),
                    result.station,
                    datetime(d.year, d.month, d.day),
                    relative_path=rel,
                    session=(target.sessions[0] if target.sessions else "15s_24hr"),
                    rinex_version=target.rinex_version,
                )
            except Exception as exc:  # noqa: BLE001 - index must not fail the push
                logger.warning("rinex_file index failed: %s", exc)
            finally:
                conn.close()

    if args.json:
        out = _result_dict(result)
        out["indexed_id"] = indexed_id
        print(json.dumps(out, indent=2))
    else:
        icon = "✅" if result.ok else "❌"
        tag = " [dry-run]" if result.dry_run else ""
        print(f"{icon} {result.station} {result.file_date}{tag}: {result.message}")
        if result.source_path:
            print(f"   source={result.source_path}")
        if result.qc_passed is not None:
            print(
                f"   qc={'pass' if result.qc_passed else 'FAIL'} ({result.qc_message})"
            )
        if result.dest:
            print(f"   dest={result.dest}")
        if indexed_id is not None:
            print(f"   indexed rinex_file id={indexed_id}")
        for err in result.errors:
            print(f"   ⚠ {err}")
    return 0 if result.ok else 1


def create_epos_disseminate_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "epos-disseminate",
        help="Convert archived RINEX to RINEX3 long names and push to EPOS (T1)",
        description=(
            "EPOS dissemination (phase 1, tracer bullet): convert one archived "
            "RINEX for --station/--date to RINEX 3.04 with a long IGS name and push "
            "it to the tier:dissemination target. Gated active:false in sync.yaml."
        ),
    )
    parser.add_argument("--station", help="4-char station id (e.g. FIM2)")
    parser.add_argument("--date", help="Observation date YYYY-MM-DD (UTC day)")
    parser.add_argument(
        "--list-stations",
        action="store_true",
        help="List EPOS-eligible stations from TOS (in_network_epos + min attrs) and exit",
    )
    parser.add_argument(
        "--refresh-metadata",
        action="store_true",
        help="Run the TOS→EPOS station metadata ETL (for --station, or all EPOS "
        "stations) and exit — no file pipeline",
    )
    parser.add_argument(
        "--no-qc",
        action="store_true",
        help="Skip the header-QC gate (e.g. offline pre-stage with TOS unreachable)",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Do not index the pushed file into the EPOS rinex_file table",
    )
    parser.add_argument(
        "--target", help="Dissemination target name (default: first in sync.yaml)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Convert + rsync --dry-run; no file is written to the dest",
    )
    parser.add_argument(
        "--dest-override",
        help="Override the target dest (e.g. /tmp/epos_stage for pre-stage)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if the target is inactive (pre-stage; use --dest-override)",
    )
    parser.add_argument(
        "--config", help="Path to sync.yaml (default: GPS_CONFIG_PATH/sync.yaml)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON result"
    )
    parser.set_defaults(func=cmd_epos_disseminate)
    return parser
