#!/usr/bin/env python3
"""Download performance experiment runner.

Tests different concurrency levels and distribution windows to find
optimal parallel download parameters for ~170 GPS stations.

Each trial:
1. Resets archived files for the test date range
2. Starts background system metrics sampler
3. Runs ``receivers download --all --parallel`` as a subprocess
4. Parses the EXPERIMENT_RESULT JSON output
5. Stores results in SQLite for analysis

Usage::

    # Single trial
    python tests/benchmarks/download_experiment.py \\
        --concurrency 64 --window 10 --session 15s_24hr --days 1

    # Phase 1 coarse sweep (skip low concurrency — too slow for 190 stations)
    python tests/benchmarks/download_experiment.py \\
        --concurrency 48,64,95,190 --window 10 --session 15s_24hr --days 1

    # Quick test with specific stations
    python tests/benchmarks/download_experiment.py \\
        --concurrency 2 --window 1 --session 15s_24hr --days 1 \\
        --stations ELDC,THOB

    # Generate report from collected results
    python tests/benchmarks/download_experiment.py --report

    # Markdown-formatted report
    python tests/benchmarks/download_experiment.py --report --format markdown
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# Ensure imports work when running script directly
_script_dir = Path(__file__).resolve().parent
_project_root = _script_dir.parent.parent
sys.path.insert(0, str(_project_root / "src"))
sys.path.insert(0, str(_script_dir))

from system_sampler import SystemSampler
from results_store import ResultsStore


def _resolve_data_prepath() -> Path:
    """Read data_prepath from receivers config.

    Falls back to ~/tmp/gpsdata if config is unavailable.
    """
    try:
        from receivers.config.receivers_config import ReceiversConfig
        cfg = ReceiversConfig()
        return Path(cfg.get_data_prepath())
    except Exception:
        return Path.home() / "tmp" / "gpsdata"


def _resolve_tmp_dir() -> Path:
    """Read tmp download directory from receivers config."""
    try:
        from receivers.config.receivers_config import ReceiversConfig
        cfg = ReceiversConfig()
        return Path(cfg.get_tmp_dir())
    except Exception:
        return Path.home() / "tmp" / "gps_receivers" / "downloads"


def _calculate_batches(total_stations: int, target_concurrency: int) -> int:
    """Compute number of batches to achieve a target concurrency.

    group_size = ceil(total / batches), so we solve for batches:
    batches = ceil(total / target_concurrency)
    """
    if target_concurrency >= total_stations:
        return 1
    return math.ceil(total_stations / target_concurrency)


def _effective_concurrency(total_stations: int, batches: int) -> int:
    """What group_size (actual concurrency) results from this batch count."""
    return math.ceil(total_stations / batches)


def _count_stations(stations_arg: Optional[str]) -> int:
    """Count how many stations will participate in the experiment.

    If --stations is given, count those.  Otherwise query the config
    for all active stations.
    """
    if stations_arg:
        return len(stations_arg.split(","))

    try:
        from receivers.cli.main import get_all_station_configs
        return len(get_all_station_configs())
    except Exception:
        return 170  # reasonable default


def _get_station_list(stations_arg: Optional[str]) -> list[str]:
    """Return explicit station list, or None to use --all."""
    if stations_arg:
        return [s.strip().upper() for s in stations_arg.split(",")]
    return []


def _reset_archives(
    data_prepath: Path,
    tmp_dir: Path,
    session: str,
    start_date: datetime,
    end_date: datetime,
) -> dict[str, int]:
    """Delete ALL data for the test date range — archives, tmp, everything.

    Ensures no leftover files can make stations appear "up_to_date" in the
    next trial.  ``find_existing_archive()`` checks both the archive path
    AND per-station tmp dirs (``tmp_dir/STATION/session/``), so both must
    be cleaned.

    Returns dict with counts: archive_removed, tmp_removed, remaining.
    """
    archive_removed = 0
    tmp_removed = 0
    current = start_date

    # 1. Archive files: data_prepath/YEAR/MONTH/*/session/raw/*
    while current <= end_date:
        year = str(current.year)
        month = current.strftime("%b").lower()

        year_month_dir = data_prepath / year / month
        if year_month_dir.is_dir():
            for station_dir in year_month_dir.iterdir():
                if not station_dir.is_dir():
                    continue
                session_raw = station_dir / session / "raw"
                if session_raw.is_dir():
                    for f in session_raw.iterdir():
                        if f.is_file():
                            f.unlink()
                            archive_removed += 1

        current += timedelta(days=1)

    # 2. Per-station tmp dirs: tmp_dir/STATION/session/
    #    These are partial/completed downloads that find_existing_archive() checks.
    #    Nuke everything under tmp to ensure no contamination between trials.
    if tmp_dir.is_dir():
        for item in tmp_dir.iterdir():
            if item.is_dir():
                # Count files before removing
                for f in item.rglob("*"):
                    if f.is_file():
                        tmp_removed += 1
                shutil.rmtree(item, ignore_errors=True)
            elif item.is_file():
                item.unlink(missing_ok=True)
                tmp_removed += 1

    # 3. Post-reset verification — count any survivors
    remaining = _count_archive_files(data_prepath, session, start_date, end_date)
    if tmp_dir.is_dir():
        remaining += sum(1 for f in tmp_dir.rglob("*") if f.is_file())

    return {
        "archive_removed": archive_removed,
        "tmp_removed": tmp_removed,
        "remaining": remaining,
    }


def _reset_file_tracking(
    session: str, start_date: datetime, end_date: datetime
) -> int:
    """Delete file_tracking entries for the test date range.

    DELETES rows rather than setting status='missing', because the
    is_file_missing() DB function treats status='missing' as "known missing
    on receiver — skip download", which prevents re-downloads between trials.

    Also clears related file_locations rows (CASCADE) and backfill_progress.

    Uses DatabaseConnectionFactory (same as all other receivers components).
    Returns number of rows affected.
    """
    try:
        from receivers.health.database_factory import DatabaseConnectionFactory

        conn = DatabaseConnectionFactory.get_connection(database="gps_health")
        try:
            with conn.cursor() as cur:
                # Delete file_tracking rows (file_locations CASCADE-deletes)
                cur.execute(
                    """\
                    DELETE FROM file_tracking
                    WHERE session_type = %s
                      AND file_date >= %s
                      AND file_date <= %s
                    """,
                    (session, start_date.date(), end_date.date()),
                )
                affected = cur.rowcount

                # Also reset backfill_progress for this session
                cur.execute(
                    """\
                    DELETE FROM backfill_progress
                    WHERE session_type = %s
                      AND backfill_end >= %s
                    """,
                    (session, start_date.date()),
                )
            conn.commit()
            return affected
        finally:
            conn.close()
    except Exception as e:
        print(f"  Warning: could not reset file_tracking: {e}")
        return 0


def _count_archive_files(
    data_prepath: Path,
    session: str,
    start_date: datetime,
    end_date: datetime,
) -> int:
    """Count archived files on disk for the date range.

    Walks data_prepath/YEAR/MONTH/*/session/raw/ and counts files.
    Only counts the archive path — tmp dir files are partial/failed
    downloads that should NOT be counted as successfully downloaded.
    """
    count = 0
    current = start_date

    while current <= end_date:
        year = str(current.year)
        month = current.strftime("%b").lower()

        year_month_dir = data_prepath / year / month
        if year_month_dir.is_dir():
            for station_dir in year_month_dir.iterdir():
                if not station_dir.is_dir():
                    continue
                session_raw = station_dir / session / "raw"
                if session_raw.is_dir():
                    count += sum(1 for f in session_raw.iterdir() if f.is_file())

        current += timedelta(days=1)

    return count


def _find_receivers_command() -> str:
    """Find the receivers CLI entry point.

    Prefers the installed ``receivers`` command.  Falls back to
    ``python -m receivers`` if the entry point is not on PATH.
    """
    if shutil.which("receivers"):
        return "receivers"
    return f"{sys.executable} -m receivers"


def _build_download_command(
    *,
    batches: int,
    window: float,
    session: str,
    days: int,
    retry_delay: float,
    stations: list[str],
) -> list[str]:
    """Build the receivers download command line.

    Uses the installed ``receivers`` CLI entry point — same as the
    operational system and the scheduler.
    """
    receivers_cmd = _find_receivers_command()
    cmd = receivers_cmd.split() + ["download"]

    if stations:
        cmd.extend(stations)
    else:
        cmd.append("--all")

    cmd.extend([
        "--sync", "--archive", "--parallel",
        "--batches", str(batches),
        "--distribution-window", str(window),
        "--retry-delay", str(retry_delay),
        "--session", session,
        "-d", str(days),
        "--json-log",
    ])

    return cmd


def _parse_experiment_result(stdout: str) -> Optional[dict[str, Any]]:
    """Extract the EXPERIMENT_RESULT JSON from subprocess stdout."""
    for line in stdout.splitlines():
        if line.startswith("EXPERIMENT_RESULT:"):
            json_str = line[len("EXPERIMENT_RESULT:"):]
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                print(f"  Warning: failed to parse JSON: {json_str[:200]}")
                return None
    return None


def _calculate_time_range(
    session: str, days: int
) -> tuple[datetime, datetime]:
    """Calculate the date range that will be downloaded.

    Mirrors the logic in receivers CLI: previous complete periods.
    """
    now = datetime.now()

    if "24hr" in session:
        # Daily session: go back `days` complete days
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
    else:
        # Hourly session: go back `days` complete hours
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=days)

    return start, end


def run_trial(
    *,
    target_concurrency: int,
    window: float,
    session: str,
    days: int,
    retry_delay: float,
    stations: list[str],
    store: ResultsStore,
    dry_run: bool = False,
) -> Optional[int]:
    """Execute a single experiment trial.

    Returns trial_id on success, None on failure.
    """
    total = len(stations) if stations else _count_stations(None)
    batches = _calculate_batches(total, target_concurrency)
    effective = _effective_concurrency(total, batches)

    print(f"\n{'='*70}")
    print(f"TRIAL: concurrency={target_concurrency} (effective={effective}), "
          f"batches={batches}, window={window}min, session={session}")
    print(f"{'='*70}")

    if dry_run:
        print("  [DRY RUN] Would execute:")
        cmd = _build_download_command(
            batches=batches, window=window, session=session,
            days=days, retry_delay=retry_delay, stations=stations,
        )
        print(f"  {' '.join(cmd)}")
        return None

    # 1. Reset state
    data_prepath = _resolve_data_prepath()
    tmp_dir = _resolve_tmp_dir()
    start_date, end_date = _calculate_time_range(session, days)

    print(f"  Resetting ALL data in {data_prepath} for "
          f"{start_date.date()} to {end_date.date()}...")
    reset = _reset_archives(data_prepath, tmp_dir, session, start_date, end_date)
    print(f"  Removed {reset['archive_removed']} archive + "
          f"{reset['tmp_removed']} tmp files")
    if reset["remaining"] > 0:
        print(f"  WARNING: {reset['remaining']} files survived reset!")

    tracking_reset = _reset_file_tracking(session, start_date, end_date)
    print(f"  Reset {tracking_reset} file_tracking rows")

    # 2. Create trial record
    trial_id = store.create_trial(
        concurrency=effective,
        batches=batches,
        distribution_window=window,
        session=session,
        days_back=days,
        total_stations=total,
        notes=f"target_concurrency={target_concurrency}",
    )
    print(f"  Trial ID: {trial_id}")

    # 3. Start system sampler
    sampler = SystemSampler(interval=2.0)
    sampler.start()

    # 4. Run download subprocess
    cmd = _build_download_command(
        batches=batches, window=window, session=session,
        days=days, retry_delay=retry_delay, stations=stations,
    )
    print(f"  Running: {' '.join(cmd)}")
    t0 = time.monotonic()

    try:
        # Use Popen with process group so we can kill the entire tree on timeout.
        # subprocess.run(capture_output=True, timeout=...) can deadlock when the
        # child has zombie threads holding pipe FDs open after SIGTERM.
        import os
        import signal

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(_project_root),
            start_new_session=True,  # New process group for clean kill
        )
        try:
            stdout, stderr = proc.communicate(timeout=7200)
        except subprocess.TimeoutExpired:
            # Kill entire process group (SIGKILL) to handle zombie threads
            os.killpg(proc.pid, signal.SIGKILL)
            # Drain any remaining output (non-blocking after SIGKILL)
            stdout, stderr = proc.communicate(timeout=10)
            raise  # Re-raise TimeoutExpired
        result = subprocess.CompletedProcess(
            cmd, proc.returncode, stdout, stderr
        )
        wall_clock = time.monotonic() - t0
    except subprocess.TimeoutExpired:
        wall_clock = time.monotonic() - t0
        print(f"  TIMEOUT after {wall_clock:.0f}s")
        sampler.stop()

        # Fallback: count files that were archived before timeout
        fallback_files = _count_archive_files(
            data_prepath, session, start_date, end_date
        )
        print(f"  Fallback file count from archive: {fallback_files}")

        store.finish_trial(
            trial_id,
            wall_clock_seconds=wall_clock,
            files_downloaded=fallback_files,
            stations_successful=0,
            stations_unreachable=0,
            stations_failed=total,
            retried=0,
            retry_recovered=0,
        )
        # Preserve system samples even on timeout
        store.insert_system_samples(trial_id, sampler.to_dicts())
        return trial_id

    # 5. Stop sampler
    sampler.stop()

    # 6. Parse results
    experiment_data = _parse_experiment_result(result.stdout)

    if experiment_data:
        store.finish_trial(
            trial_id,
            wall_clock_seconds=wall_clock,
            files_downloaded=experiment_data.get("total_files", 0),
            stations_successful=experiment_data.get("successful", 0),
            stations_unreachable=experiment_data.get("unreachable", 0),
            stations_failed=experiment_data.get("failed", 0),
            retried=experiment_data.get("retried", 0),
            retry_recovered=experiment_data.get("retry_recovered", 0),
        )

        # Store per-station results
        stations_data = experiment_data.get("stations", {})
        store.insert_station_results(trial_id, stations_data)
    else:
        print("  Warning: no EXPERIMENT_RESULT found in output")
        if result.stderr:
            # Print last few lines of stderr for debugging
            stderr_lines = result.stderr.strip().splitlines()
            for line in stderr_lines[-10:]:
                print(f"  stderr: {line}")

        # Fallback: count archived files on disk
        fallback_files = _count_archive_files(
            data_prepath, session, start_date, end_date
        )
        if fallback_files:
            print(f"  Fallback file count from archive: {fallback_files}")

        store.finish_trial(
            trial_id,
            wall_clock_seconds=wall_clock,
            files_downloaded=fallback_files,
            stations_successful=0,
            stations_unreachable=0,
            stations_failed=0,
            retried=0,
            retry_recovered=0,
        )

    # 7. Store system samples
    store.insert_system_samples(trial_id, sampler.to_dicts())

    # Print quick summary
    mins = wall_clock / 60
    print(f"  Completed in {mins:.1f}m ({wall_clock:.0f}s)")
    if experiment_data:
        print(f"  Files: {experiment_data.get('total_files', 0)}, "
              f"OK: {experiment_data.get('successful', 0)}, "
              f"Unreachable: {experiment_data.get('unreachable', 0)}, "
              f"Retried: {experiment_data.get('retried', 0)}, "
              f"Recovered: {experiment_data.get('retry_recovered', 0)}")

    return trial_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download performance experiment runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Coarse sweep (Phase 1) — skip low concurrency, too slow for 190 stations
  %(prog)s --concurrency 48,64,95,190 --window 10 --session 15s_24hr --days 1

  # Window refinement (Phase 2)
  %(prog)s --concurrency 64 --window 5,10,15 --session 15s_24hr --days 1

  # Quick test
  %(prog)s --concurrency 2 --window 1 --session 15s_24hr --days 1 --stations ELDC,THOB

  # View results
  %(prog)s --report
  %(prog)s --report --format markdown
""",
    )

    parser.add_argument(
        "--concurrency",
        type=str,
        help="Target concurrency level(s), comma-separated (e.g., 10,30,50)",
    )
    parser.add_argument(
        "--window",
        type=str,
        default="10",
        help="Distribution window(s) in minutes, comma-separated (default: 10)",
    )
    parser.add_argument(
        "--session",
        type=str,
        default="15s_24hr",
        help="Session type (default: 15s_24hr)",
    )
    parser.add_argument(
        "--days", "-d",
        type=int,
        default=1,
        help="Days/periods to look back (default: 1)",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=30.0,
        help="Retry delay in seconds (default: 30, production uses 90)",
    )
    parser.add_argument(
        "--stations",
        type=str,
        default=None,
        help="Comma-separated station subset (default: --all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without executing",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate report from stored results",
    )
    parser.add_argument(
        "--format",
        choices=["text", "markdown"],
        default="text",
        help="Report output format (default: text)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Override SQLite database path",
    )

    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None
    store = ResultsStore(db_path=db_path)

    # Report mode
    if args.report:
        from report_generator import generate_report
        generate_report(store, output_format=args.format)
        return 0

    # Trial mode — require concurrency
    if not args.concurrency:
        parser.error("--concurrency is required (or use --report)")

    concurrency_levels = [int(c.strip()) for c in args.concurrency.split(",")]
    windows = [float(w.strip()) for w in args.window.split(",")]
    stations = _get_station_list(args.stations)

    # Run trials
    trial_count = len(concurrency_levels) * len(windows)
    print(f"Experiment plan: {trial_count} trial(s)")
    print(f"  Concurrency: {concurrency_levels}")
    print(f"  Windows: {windows}")
    print(f"  Session: {args.session}, Days: {args.days}")
    if stations:
        print(f"  Stations: {', '.join(stations)} ({len(stations)})")
    else:
        n = _count_stations(None)
        print(f"  Stations: ALL ({n})")

    trial_ids = []
    for window in windows:
        for concurrency in concurrency_levels:
            trial_id = run_trial(
                target_concurrency=concurrency,
                window=window,
                session=args.session,
                days=args.days,
                retry_delay=args.retry_delay,
                stations=stations,
                store=store,
                dry_run=args.dry_run,
            )
            if trial_id is not None:
                trial_ids.append(trial_id)

    if trial_ids and not args.dry_run:
        print(f"\n{'='*70}")
        print(f"All trials complete. Trial IDs: {trial_ids}")
        print(f"Run with --report to see results.")
        print(f"DB: {store.db_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
