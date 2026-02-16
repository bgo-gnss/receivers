#!/usr/bin/env python3
"""
Scheduler management CLI for bulk GPS receiver downloads.

Provides complete control over the APScheduler-based bulk download system
while maintaining full compatibility with manual operations.
"""

import argparse
import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from ..scheduling.bulk_scheduler import BulkDownloadScheduler, HAS_APSCHEDULER
    from ..scheduling.config_loader import create_default_config_file, load_scheduler_config
except ImportError:
    HAS_APSCHEDULER = False


def cmd_scheduler_start(args) -> int:
    """Start the bulk download scheduler."""

    if not HAS_APSCHEDULER:
        print("❌ APScheduler not available. Install with: pip install apscheduler")
        return 1

    # Parse --only flag
    scheduler_types = None
    if getattr(args, 'only', None):
        scheduler_types = [s.strip() for s in args.only.split(',')]
        print(f"📋 Running only: {', '.join(scheduler_types)}")

    try:
        # Create scheduler with filtering options
        scheduler = BulkDownloadScheduler(
            production_mode=not args.verbose,
            max_workers=args.max_workers,
            station_filter=getattr(args, 'stations', None),
            max_stations_per_session=getattr(args, 'max_stations', None),
            scheduler_types=scheduler_types
        )

        # Schedule all sessions
        scheduler.schedule_all_sessions()

        # Count jobs before starting
        jobs_count = len(scheduler.get_scheduled_jobs())
        print(f"✅ Scheduled {jobs_count} download jobs")

        # Set up signal handling for graceful shutdown
        def signal_handler(signum, frame):
            print("\\n🛑 Shutting down scheduler...")
            scheduler.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        print(f"🚀 Starting scheduler with {scheduler.max_workers} workers...")
        print("   Press Ctrl+C to stop")

        # Start scheduler (non-blocking for BackgroundScheduler)
        scheduler.start()

        # Give scheduler a moment to process jobs and calculate next run times
        time.sleep(0.1)

        # Now show scheduled jobs with their actual next run times
        if args.show_jobs:
            jobs = scheduler.get_scheduled_jobs()
            print("\\nScheduled jobs:")
            for job in sorted(jobs, key=lambda x: x['next_run'] or ''):
                next_run = job['next_run'] or 'Not scheduled'
                print(f"  {job['id']}: {next_run}")
        
        # Keep running
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\\n🛑 Scheduler stopped by user")
        return 0
    except Exception as e:
        print(f"❌ Scheduler failed: {e}")
        return 1


def cmd_scheduler_status(args) -> int:
    """Show scheduler status and job information."""
    
    if not HAS_APSCHEDULER:
        print("❌ APScheduler not available. Install with: pip install apscheduler")
        return 1
        
    try:
        # Create scheduler (no start)
        scheduler = BulkDownloadScheduler(production_mode=True)
        
        # Get status
        status = scheduler.get_job_status()
        jobs = scheduler.get_scheduled_jobs()
        
        print("📊 Scheduler Status")
        print("=" * 50)
        print(f"Running: {status['scheduler_running']}")
        print(f"Total jobs: {status['total_jobs']}")
        print(f"Active downloads: {status['running_jobs']}")
        
        if status['current_jobs']:
            print(f"Current jobs: {', '.join(status['current_jobs'])}")
            
        if args.show_jobs and jobs:
            print(f"\\n📅 Scheduled Jobs ({len(jobs)})")
            print("-" * 50)
            
            # Group by session type
            by_session = {}
            for job in jobs:
                session = job['id'].split('_')[0]
                if session not in by_session:
                    by_session[session] = []
                by_session[session].append(job)
                
            for session_type, session_jobs in sorted(by_session.items()):
                print(f"\\n{session_type} ({len(session_jobs)} stations):")
                
                # Show next few runs
                next_runs = sorted([j for j in session_jobs if j['next_run']], 
                                 key=lambda x: x['next_run'])[:5]
                                 
                for job in next_runs:
                    station = job['id'].split('_', 1)[1]
                    next_run = datetime.fromisoformat(job['next_run']).strftime('%H:%M:%S')
                    print(f"  {station}: {next_run}")
                    
                if len(session_jobs) > 5:
                    print(f"  ... and {len(session_jobs) - 5} more")
                    
        return 0
        
    except Exception as e:
        print(f"❌ Failed to get scheduler status: {e}")
        return 1


def cmd_scheduler_config(args) -> int:
    """Manage scheduler configuration."""
    import os

    # Determine config path (respects GPS_CONFIG_PATH env var)
    gps_config_dir = os.getenv('GPS_CONFIG_PATH')
    if gps_config_dir:
        config_file = Path(gps_config_dir) / 'scheduler.yaml'
    else:
        config_file = Path.home() / '.config' / 'gpsconfig' / 'scheduler.yaml'

    if args.create:
        try:
            created_file = create_default_config_file()
            print(f"✅ Created scheduler configuration: {created_file}")
            print("   Edit this file to customize scheduling behavior")
            return 0
        except Exception as e:
            print(f"❌ Failed to create configuration: {e}")
            return 1

    if args.show:
        if config_file.exists():
            # Load and display as YAML (or raw content)
            try:
                config = load_scheduler_config(config_file)
                print(f"📋 Current scheduler configuration ({config_file}):")
                print(json.dumps(config, indent=2))
            except Exception as e:
                # Fall back to showing raw file
                print(f"📋 Current scheduler configuration ({config_file}):")
                print(config_file.read_text())
        else:
            print(f"❌ No configuration file found at {config_file}")
            print("   Create one with: receivers scheduler config --create")
            return 1

    return 0


def cmd_scheduler_test(args) -> int:
    """Test scheduler setup without starting."""

    if not HAS_APSCHEDULER:
        print("❌ APScheduler not available. Install with: pip install apscheduler")
        return 1

    # Parse --only flag
    scheduler_types = None
    if getattr(args, 'only', None):
        scheduler_types = [s.strip() for s in args.only.split(',')]
        print(f"📋 Testing only: {', '.join(scheduler_types)}")

    try:
        print("🧪 Testing scheduler setup...")

        # Create scheduler with filtering options
        scheduler = BulkDownloadScheduler(
            production_mode=True,
            station_filter=getattr(args, 'stations', None),
            max_stations_per_session=getattr(args, 'max_stations', None),
            scheduler_types=scheduler_types
        )

        # Load stations
        print(f"✅ Loaded {len(scheduler.stations)} station configurations")

        # Show filtering info
        if scheduler.station_filter:
            print(f"🔍 Station filter: {', '.join(scheduler.station_filter)}")
        if scheduler.max_stations_per_session:
            print(f"🔢 Max stations per session: {scheduler.max_stations_per_session}")

        # Test scheduling (without starting)
        scheduler.schedule_all_sessions()
        jobs = scheduler.get_scheduled_jobs()

        print(f"✅ Successfully scheduled {len(jobs)} jobs")

        # Show distribution by session
        by_session = {}
        station_list = {}
        for job in jobs:
            session = job['id'].split('_')[0]
            station = job['id'].split('_', 1)[1]
            by_session[session] = by_session.get(session, 0) + 1
            if session not in station_list:
                station_list[session] = []
            station_list[session].append(station)

        print("\\n📊 Job distribution:")
        for session, count in sorted(by_session.items()):
            config = scheduler.schedule_configs.get(session, {})
            schedule_time = f"{config.schedule_minute:02d}:XX" if hasattr(config, 'schedule_minute') else 'Unknown'
            frequency = getattr(config, 'frequency', 'unknown')
            stations = ', '.join(sorted(station_list[session])[:5])
            if len(station_list[session]) > 5:
                stations += f" +{len(station_list[session])-5} more"
            print(f"  {session}: {count} stations ({frequency} at {schedule_time})")
            print(f"           Stations: {stations}")

        # Test next run times
        if jobs:
            next_jobs = sorted([j for j in jobs if j['next_run']],
                             key=lambda x: x['next_run'])[:3]
            print("\\n⏰ Next few scheduled runs:")
            for job in next_jobs:
                station = job['id'].split('_', 1)[1]
                session = job['id'].split('_')[0]
                next_run = datetime.fromisoformat(job['next_run']).strftime('%Y-%m-%d %H:%M:%S')
                print(f"  {station} ({session}): {next_run}")

        print("\\n✅ Scheduler test completed successfully")
        print("   Use 'receivers scheduler start' to run the scheduler")

        return 0

    except Exception as e:
        print(f"❌ Scheduler test failed: {e}")
        return 1


def cmd_scheduler_stop(args) -> int:
    """Stop the running scheduler."""

    if not HAS_APSCHEDULER:
        print("❌ APScheduler not available. Install with: pip install apscheduler")
        return 1

    try:
        import os
        import psutil

        # Find running scheduler process
        current_pid = os.getpid()
        scheduler_pids = []

        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline', [])
                if cmdline and 'receivers' in ' '.join(cmdline) and 'scheduler' in ' '.join(cmdline) and 'start' in ' '.join(cmdline):
                    if proc.info['pid'] != current_pid:
                        scheduler_pids.append(proc.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not scheduler_pids:
            print("ℹ️  No running scheduler found")
            return 0

        print(f"🛑 Found {len(scheduler_pids)} running scheduler process(es)")

        for pid in scheduler_pids:
            try:
                proc = psutil.Process(pid)

                if args.force:
                    print(f"⚡ Force stopping scheduler (PID {pid})...")
                    proc.kill()  # SIGKILL - immediate termination
                    proc.wait(timeout=1)
                    print(f"✅ Scheduler forcefully stopped (PID {pid})")
                else:
                    print(f"🛑 Gracefully stopping scheduler (PID {pid})...")
                    print("   Waiting for active downloads to complete...")
                    proc.terminate()  # SIGTERM - graceful shutdown
                    proc.wait(timeout=30)
                    print(f"✅ Scheduler stopped gracefully (PID {pid})")

            except psutil.TimeoutExpired:
                print(f"⚠️  Scheduler did not stop within timeout, force killing...")
                proc.kill()
                proc.wait(timeout=5)
                print(f"✅ Scheduler forcefully stopped (PID {pid})")
            except Exception as e:
                print(f"❌ Failed to stop scheduler (PID {pid}): {e}")
                return 1

        return 0

    except ImportError:
        print("❌ psutil not available. Install with: pip install psutil")
        print("   Or manually stop the scheduler with: pkill -f 'receivers scheduler start'")
        return 1
    except Exception as e:
        print(f"❌ Failed to stop scheduler: {e}")
        return 1


def cmd_scheduler_backfill(args) -> int:
    """Manually trigger backfill for a specific session type."""
    from datetime import date, timedelta

    session_type = getattr(args, 'session', 'status_1hr')
    days_back = getattr(args, 'days', 30)
    stations = getattr(args, 'stations', None)

    print(f"Starting manual backfill for {session_type} ({days_back} days back)")

    try:
        from ..health.database_factory import DatabaseConnectionFactory

        # If specific stations requested, just process them
        if stations:
            station_ids = [s.upper() for s in stations]
        else:
            # Get stations from backfill_progress table
            with DatabaseConnectionFactory.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT sid FROM backfill_progress
                        WHERE session_type = %s
                          AND status IN ('pending', 'in_progress')
                        ORDER BY last_run ASC NULLS FIRST
                    """, (session_type,))
                    station_ids = [row[0] for row in cur.fetchall()]

        if not station_ids:
            print(f"No stations pending backfill for {session_type}")
            return 0

        max_stations = getattr(args, 'max_stations', None)
        if max_stations:
            station_ids = station_ids[:max_stations]

        print(f"Processing {len(station_ids)} stations: {', '.join(station_ids[:10])}")
        if len(station_ids) > 10:
            print(f"  ... and {len(station_ids) - 10} more")

        from ..scheduling.backfill import _backfill_station_day_generic

        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=days_back - 1)
        archiving_mode = getattr(args, 'archiving_mode', 'bulk')
        immediate = archiving_mode != 'bulk'

        total_processed = 0
        for station_id in station_ids:
            current = start_date
            while current <= end_date:
                has_more = _backfill_station_day_generic(
                    station_id, current, end_date, session_type,
                    immediate_archive=immediate,
                )
                total_processed += 1
                if not has_more:
                    break
                current += timedelta(days=1)

                # Show progress
                if total_processed % 10 == 0:
                    print(f"  Processed {total_processed} station-days...")

        print(f"\nBackfill complete: {total_processed} station-days processed")
        return 0

    except ImportError as e:
        print(f"Required modules not available: {e}")
        return 1
    except Exception as e:
        print(f"Backfill failed: {e}")
        return 1


def cmd_scheduler_reconcile(args) -> int:
    """Manually trigger raw->RINEX archive reconciliation."""

    days_back = getattr(args, 'days', 30)
    dry_run = getattr(args, 'dry_run', False)
    stations = getattr(args, 'stations', None)

    print(f"Archive reconciler: scanning {days_back} days back")
    if dry_run:
        print("  DRY RUN: no conversions will be performed")

    try:
        from ..cli.main import get_all_station_configs
        from ..config.receiver_registry import has_rinex_converter
        from ..health.file_tracker import ArchiveFileChecker
        from ..scheduling.archive_reconciler import (
            _find_raw_file, _find_rinex_file, _convert_raw_to_rinex,
        )
        from datetime import date, datetime, timedelta, timezone

        all_stations = get_all_station_configs()

        if stations:
            # User-specified stations: use as-is, look up receiver types
            target_stations = {}
            for s in stations:
                sid = s.upper()
                cfg = all_stations.get(sid, {})
                rt = cfg.get('receiver_type', '').lower()
                target_stations[sid] = rt if rt else 'polarx5'
        else:
            # All active stations with RINEX converters
            target_stations = {
                sid: cfg.get('receiver_type', '').lower()
                for sid, cfg in all_stations.items()
                if cfg.get('enabled', True)
                and has_rinex_converter(cfg.get('receiver_type', ''))
                and cfg.get('station_status') not in ('discontinued', 'inactive')
            }

        session_types = ['15s_24hr', '1Hz_1hr']
        print(f"Scanning {len(target_stations)} stations, sessions: {session_types}")

        checker = ArchiveFileChecker()
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=days_back - 1)

        total_missing = 0
        total_converted = 0

        for station_id in sorted(target_stations):
            receiver_type = target_stations[station_id]
            for session_type in session_types:
                current = start_date
                while current <= end_date:
                    dt = datetime.combine(current, datetime.min.time()).replace(
                        tzinfo=timezone.utc
                    )

                    hours = [0] if session_type == "15s_24hr" else list(range(24))
                    for hour in hours:
                        file_dt = dt.replace(hour=hour)
                        raw_path = _find_raw_file(
                            station_id, session_type, file_dt, checker,
                            receiver_type=receiver_type,
                        )
                        if raw_path is None:
                            continue
                        rinex_path = _find_rinex_file(raw_path)
                        if rinex_path is not None:
                            continue

                        total_missing += 1
                        if dry_run:
                            print(f"  MISSING RINEX: {raw_path}")
                        else:
                            success = _convert_raw_to_rinex(
                                station_id, raw_path,
                                receiver_type=receiver_type,
                            )
                            if success:
                                total_converted += 1

                    current += timedelta(days=1)

        if dry_run:
            print(f"\nDry run complete: {total_missing} raw files missing RINEX")
        else:
            print(f"\nReconciliation complete: {total_missing} missing, {total_converted} converted")

        return 0

    except ImportError as e:
        print(f"Required modules not available: {e}")
        return 1
    except Exception as e:
        print(f"Reconciliation failed: {e}")
        return 1


def cmd_scheduler_pipeline_status(args) -> int:
    """Show pipeline job status and history."""
    try:
        from ..scheduling.pipeline import PipelineStateStore

        store = PipelineStateStore()
        station = getattr(args, 'station', None)
        session = getattr(args, 'session', None)
        limit = getattr(args, 'limit', 20)

        if station:
            jobs = store.load_jobs_by_station(
                station.upper(),
                session_type=session,
                limit=limit,
            )
            print(f"Pipeline jobs for {station.upper()}" + (f" ({session})" if session else ""))
        else:
            # Show stats and recent incomplete jobs
            stats = store.get_stats()
            print("Pipeline Statistics")
            print("=" * 50)
            print(f"Total jobs:      {stats['total_jobs']}")
            print(f"Complete:        {stats['complete_jobs']}")
            print(f"Incomplete:      {stats['incomplete_jobs']}")

            if stats['by_session_type']:
                print("\nBy session type:")
                for session_type, count in sorted(stats['by_session_type'].items()):
                    print(f"  {session_type}: {count}")

            jobs = store.load_incomplete_jobs()
            if not jobs:
                print("\nNo incomplete pipeline jobs")
                return 0
            print(f"\nIncomplete jobs ({len(jobs)}):")

        if not jobs:
            print("  No matching pipeline jobs found")
            return 0

        print(f"\n{'Station':<8} {'Session':<12} {'Stages':<40} {'Updated':<20}")
        print("-" * 80)

        for job in jobs[:limit]:
            stages_str = []
            for stage, result in job.stages.items():
                icon = {
                    'pending': '⏳',
                    'running': '🔄',
                    'completed': '✅',
                    'failed': '❌',
                    'skipped': '⏭️',
                }.get(result.status.value, '?')
                stages_str.append(f"{icon}{stage.value}")

            updated = job.updated_at.strftime('%Y-%m-%d %H:%M') if job.updated_at else 'N/A'
            print(f"{job.station_id:<8} {job.session_type:<12} {' '.join(stages_str):<40} {updated:<20}")

        return 0

    except ImportError as e:
        print(f"Pipeline tracking not available: {e}")
        return 1
    except Exception as e:
        print(f"Failed to get pipeline status: {e}")
        return 1


def cmd_scheduler_load_status(args) -> int:
    """Show current system load and throttling status."""
    try:
        from ..scheduling.load_monitor import LoadMonitor
        from ..scheduling.config_loader import load_scheduler_config

        config = load_scheduler_config()
        load_cfg = config.get('load_monitoring', {})

        if not load_cfg.get('enabled', False):
            print("Load monitoring is disabled in scheduler.yaml")
            print("Enable it with: load_monitoring: { enabled: true }")
            return 0

        monitor = LoadMonitor(load_cfg)
        status = monitor.get_status()

        print("System Load Status")
        print("=" * 50)
        print(f"CPU load (1m):     {status['cpu_load_1m']:.2f}  (max: {status['thresholds']['max_cpu_load']})")
        print(f"CPU load (5m):     {status['cpu_load_5m']:.2f}")
        print(f"Active threads:    {status['active_threads']}  (max: {status['thresholds']['max_active_jobs']})")
        print(f"Network:           {status['network_mbps']:.1f} Mbps  (max: {status['thresholds']['max_network_mbps']})")

        print(f"\nJob admission by priority:")
        for priority_name, allowed in status['can_start'].items():
            icon = "✅" if allowed else "❌"
            print(f"  {icon} {priority_name}")

        return 0

    except ImportError as e:
        print(f"Load monitoring not available: {e}")
        return 1
    except Exception as e:
        print(f"Failed to get load status: {e}")
        return 1


def cmd_scheduler_bootstrap(args) -> int:
    """Manually trigger bootstrap (cold-start) downloads."""
    if not HAS_APSCHEDULER:
        print("APScheduler not available. Install with: pip install apscheduler")
        return 1

    try:
        from ..scheduling.bootstrap import schedule_bootstrap

        days = getattr(args, 'days', 3)
        stations_arg = getattr(args, 'stations', None)
        station_filter = [s.upper() for s in stations_arg] if stations_arg else None

        scheduler = BulkDownloadScheduler(
            production_mode=not getattr(args, 'verbose', False),
            station_filter=station_filter,
        )

        bootstrap_cfg = {
            'distribution_window': getattr(args, 'window', 10),
            'initial_lookback_days': days,
        }

        jobs = schedule_bootstrap(
            scheduler=scheduler.scheduler,
            stations=scheduler.stations,
            session_configs=scheduler.schedule_configs,
            bootstrap_cfg=bootstrap_cfg,
            production_mode=scheduler.production_mode,
            station_filter=station_filter,
        )

        if jobs == 0:
            print("No bootstrap jobs created (no eligible stations)")
            return 0

        print(f"Created {jobs} bootstrap download jobs (lookback={days} days)")
        print("Starting scheduler to execute bootstrap jobs...")

        import signal

        def signal_handler(signum, frame):
            print("\nStopping bootstrap...")
            scheduler.stop()
            import sys
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        scheduler.start()

        # Wait for jobs to complete
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nBootstrap stopped by user")
        return 0
    except Exception as e:
        print(f"Bootstrap failed: {e}")
        return 1


def cmd_scheduler_backfill_status(args) -> int:
    """Show backfill progress and priority queue."""
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        session = getattr(args, 'session', None)

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                if session:
                    cur.execute("""
                        SELECT sid, session_type, status, next_date, backfill_end,
                               files_found, files_imported, files_missing, files_error,
                               last_run
                        FROM backfill_progress
                        WHERE session_type = %s
                        ORDER BY status, last_run ASC NULLS FIRST
                    """, (session,))
                else:
                    cur.execute("""
                        SELECT sid, session_type, status, next_date, backfill_end,
                               files_found, files_imported, files_missing, files_error,
                               last_run
                        FROM backfill_progress
                        ORDER BY session_type, status, last_run ASC NULLS FIRST
                    """)

                rows = cur.fetchall()

        if not rows:
            print("No backfill progress entries found")
            return 0

        # Summary
        by_status = {}
        for row in rows:
            status = row[2]
            by_status[status] = by_status.get(status, 0) + 1

        title = f"Backfill Status" + (f" ({session})" if session else "")
        print(title)
        print("=" * 60)
        for status, count in sorted(by_status.items()):
            print(f"  {status}: {count} stations")

        # Detail table
        print(f"\n{'Station':<8} {'Session':<12} {'Status':<12} {'Next Date':<12} {'End':<12} {'Found':<6} {'Import':<6} {'Miss':<6} {'Err':<4}")
        print("-" * 90)

        limit = getattr(args, 'limit', 30)
        for row in rows[:limit]:
            sid, sess, status, next_dt, end_dt = row[0], row[1], row[2], row[3], row[4]
            found, imported, missing, errors = row[5] or 0, row[6] or 0, row[7] or 0, row[8] or 0
            next_str = str(next_dt) if next_dt else 'N/A'
            end_str = str(end_dt) if end_dt else 'N/A'
            print(f"{sid:<8} {sess:<12} {status:<12} {next_str:<12} {end_str:<12} {found:<6} {imported:<6} {missing:<6} {errors:<4}")

        if len(rows) > limit:
            print(f"... and {len(rows) - limit} more")

        return 0

    except ImportError as e:
        print(f"Database not available: {e}")
        return 1
    except Exception as e:
        print(f"Failed to get backfill status: {e}")
        return 1


def cmd_scheduler_gaps(args) -> int:
    """Find gaps in downloaded files."""
    from datetime import date, timedelta

    try:
        from ..health.file_tracker import GapDetector
        from ..cli.main import get_all_station_configs
    except ImportError as e:
        print(f"❌ Required modules not available: {e}")
        return 1

    # Get stations
    station_filter = getattr(args, 'stations', None)
    if station_filter:
        station_ids = [s.upper() for s in station_filter]
    else:
        # Get all enabled stations
        all_stations = get_all_station_configs()
        station_ids = [
            sid for sid, cfg in all_stations.items()
            if cfg.get('enabled', True)
        ]

    # Limit stations for testing
    max_stations = getattr(args, 'max_stations', None)
    if max_stations:
        station_ids = station_ids[:max_stations]

    session_type = getattr(args, 'session', '15s_24hr')
    days_back = getattr(args, 'days', 7)

    print(f"🔍 Finding gaps in {session_type} data for {len(station_ids)} stations")
    print(f"   Checking last {days_back} days\n")

    # Calculate date range
    end_date = date.today() - timedelta(days=1)  # Yesterday
    start_date = end_date - timedelta(days=days_back - 1)

    with GapDetector() as detector:
        if getattr(args, 'summary', False):
            # Get summary for all stations
            summary = detector.get_gap_summary(
                station_ids,
                session_type,
                days_back=days_back,
            )

            print(f"📊 Gap Summary ({summary['start_date']} to {summary['end_date']})")
            print(f"   Total expected files: {summary['total_expected']}")
            print(f"   Total archived files: {summary['total_archived']}")
            print(f"   Total gaps (need download): {summary['total_gaps']}")

            if summary['total_gaps'] > 0:
                print("\n   Stations with gaps:")
                stations_with_gaps = [
                    (sid, info) for sid, info in summary['stations'].items()
                    if info['gaps'] > 0
                ]
                for sid, info in sorted(stations_with_gaps, key=lambda x: -x[1]['gaps'])[:20]:
                    pct = 100 * info['archived'] / info['expected'] if info['expected'] else 0
                    print(f"     {sid}: {info['gaps']} gaps ({pct:.0f}% complete)")

                if len(stations_with_gaps) > 20:
                    print(f"     ... and {len(stations_with_gaps) - 20} more stations")
        else:
            # Show detailed gaps per station
            total_gaps = 0
            for station_id in sorted(station_ids):
                gaps = detector.find_gaps(
                    station_id,
                    session_type,
                    start_date,
                    end_date,
                    sync_first=True,
                    skip_missing_on_receiver=not getattr(args, 'include_missing', False),
                )

                if gaps:
                    total_gaps += len(gaps)
                    print(f"📁 {station_id}: {len(gaps)} gaps")
                    if getattr(args, 'verbose', False):
                        for gap in gaps[:10]:
                            hour_str = f" hour {gap.file_hour:02d}" if gap.file_hour is not None else ""
                            print(f"     {gap.file_date}{hour_str}")
                        if len(gaps) > 10:
                            print(f"     ... and {len(gaps) - 10} more")

            print(f"\n✅ Total gaps found: {total_gaps}")

    return 0


def cmd_scheduler_restart(args) -> int:
    """Restart the scheduler (stop and start)."""

    if not HAS_APSCHEDULER:
        print("❌ APScheduler not available. Install with: pip install apscheduler")
        return 1

    print("🔄 Restarting scheduler...")

    # Stop the scheduler
    stop_args = argparse.Namespace(force=args.force)
    result = cmd_scheduler_stop(stop_args)

    if result != 0:
        print("❌ Failed to stop scheduler, cannot restart")
        return result

    # Brief pause to ensure clean shutdown
    time.sleep(2)

    # Start the scheduler with same options as before
    # Note: This will use default settings. Users should manually start with custom options if needed.
    start_args = argparse.Namespace(
        max_workers=args.max_workers if hasattr(args, 'max_workers') else 5,
        show_jobs=False,
        verbose=args.verbose if hasattr(args, 'verbose') else False,
        stations=getattr(args, 'stations', None),
        max_stations=getattr(args, 'max_stations', None)
    )

    print("🚀 Starting scheduler...")
    return cmd_scheduler_start(start_args)


def create_scheduler_parser(subparsers):
    """Add scheduler subcommands to the main parser."""
    
    # Scheduler command group
    scheduler_parser = subparsers.add_parser(
        "scheduler",
        help="Manage bulk download scheduler",
        description="APScheduler-based bulk download system"
    )
    
    scheduler_subparsers = scheduler_parser.add_subparsers(
        dest="scheduler_command", 
        help="Scheduler commands"
    )
    
    # Start command
    start_parser = scheduler_subparsers.add_parser(
        "start",
        help="Start the bulk download scheduler"
    )
    start_parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Maximum concurrent downloads (default: from scheduler.yaml, or 15)"
    )
    start_parser.add_argument(
        "--show-jobs",
        action="store_true",
        help="Show all scheduled jobs before starting"
    )
    start_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    start_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only schedule these specific stations (e.g., OLKE ELDC THOB)"
    )
    start_parser.add_argument(
        "--max-stations",
        type=int,
        help="Maximum number of stations per session (for testing)"
    )
    start_parser.add_argument(
        "--only",
        type=str,
        help="Only run specific scheduler types (comma-separated). "
             "Options: health, 15s_24hr, 1Hz_1hr, status_1hr, downloads, all"
    )
    start_parser.set_defaults(func=cmd_scheduler_start)
    
    # Status command
    status_parser = scheduler_subparsers.add_parser(
        "status",
        help="Show scheduler status and jobs"
    )
    status_parser.add_argument(
        "--show-jobs",
        action="store_true",
        help="Show detailed job information"
    )
    status_parser.set_defaults(func=cmd_scheduler_status)
    
    # Config command
    config_parser = scheduler_subparsers.add_parser(
        "config",
        help="Manage scheduler configuration"
    )
    config_parser.add_argument(
        "--create",
        action="store_true",
        help="Create default configuration file"
    )
    config_parser.add_argument(
        "--show",
        action="store_true", 
        help="Show current configuration"
    )
    config_parser.set_defaults(func=cmd_scheduler_config)
    
    # Test command
    test_parser = scheduler_subparsers.add_parser(
        "test",
        help="Test scheduler setup without starting"
    )
    test_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only test these specific stations (e.g., OLKE ELDC THOB)"
    )
    test_parser.add_argument(
        "--max-stations",
        type=int,
        help="Maximum number of stations per session (for testing)"
    )
    test_parser.add_argument(
        "--only",
        type=str,
        help="Only test specific scheduler types (comma-separated). "
             "Options: health, 15s_24hr, 1Hz_1hr, status_1hr, downloads, all"
    )
    test_parser.set_defaults(func=cmd_scheduler_test)

    # Stop command
    stop_parser = scheduler_subparsers.add_parser(
        "stop",
        help="Stop the running scheduler"
    )
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="Force immediate shutdown without waiting for active downloads"
    )
    stop_parser.set_defaults(func=cmd_scheduler_stop)

    # Restart command
    restart_parser = scheduler_subparsers.add_parser(
        "restart",
        help="Restart the scheduler (stop and start)"
    )
    restart_parser.add_argument(
        "--force",
        action="store_true",
        help="Force immediate shutdown without waiting for active downloads"
    )
    restart_parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Maximum concurrent downloads after restart (default: from scheduler.yaml, or 15)"
    )
    restart_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging after restart"
    )
    restart_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only schedule these specific stations after restart"
    )
    restart_parser.add_argument(
        "--max-stations",
        type=int,
        help="Maximum number of stations per session after restart"
    )
    restart_parser.set_defaults(func=cmd_scheduler_restart)

    # Pipeline status command
    pipeline_parser = scheduler_subparsers.add_parser(
        "pipeline-status",
        help="Show pipeline job status and history"
    )
    pipeline_parser.add_argument(
        "--station",
        type=str,
        help="Filter by station ID"
    )
    pipeline_parser.add_argument(
        "--session",
        type=str,
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help="Filter by session type"
    )
    pipeline_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of jobs to show (default: 20)"
    )
    pipeline_parser.set_defaults(func=cmd_scheduler_pipeline_status)

    # Load status command
    load_parser = scheduler_subparsers.add_parser(
        "load-status",
        help="Show current system load and throttling status"
    )
    load_parser.set_defaults(func=cmd_scheduler_load_status)

    # Bootstrap command
    bootstrap_parser = scheduler_subparsers.add_parser(
        "bootstrap",
        help="Trigger bootstrap (cold-start) downloads"
    )
    bootstrap_parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="Number of days to look back (default: 3)"
    )
    bootstrap_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only bootstrap these specific stations"
    )
    bootstrap_parser.add_argument(
        "--window",
        type=int,
        default=10,
        help="Distribution window in minutes (default: 10)"
    )
    bootstrap_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    bootstrap_parser.set_defaults(func=cmd_scheduler_bootstrap)

    # Backfill status command
    backfill_status_parser = scheduler_subparsers.add_parser(
        "backfill-status",
        help="Show backfill progress and priority queue"
    )
    backfill_status_parser.add_argument(
        "--session",
        type=str,
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help="Filter by session type"
    )
    backfill_status_parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Maximum number of entries to show (default: 30)"
    )
    backfill_status_parser.set_defaults(func=cmd_scheduler_backfill_status)

    # Gaps command
    gaps_parser = scheduler_subparsers.add_parser(
        "gaps",
        help="Find gaps in downloaded files"
    )
    gaps_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only check these specific stations (e.g., OLKE ELDC THOB)"
    )
    gaps_parser.add_argument(
        "--max-stations",
        type=int,
        help="Maximum number of stations to check"
    )
    gaps_parser.add_argument(
        "--session",
        type=str,
        default="15s_24hr",
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help="Session type to check (default: 15s_24hr)"
    )
    gaps_parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to check (default: 7)"
    )
    gaps_parser.add_argument(
        "--summary",
        action="store_true",
        help="Show summary across all stations instead of per-station details"
    )
    gaps_parser.add_argument(
        "--include-missing",
        action="store_true",
        help="Include files known to be missing on receiver"
    )
    gaps_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed gap information"
    )
    gaps_parser.set_defaults(func=cmd_scheduler_gaps)

    # Backfill command
    backfill_parser = scheduler_subparsers.add_parser(
        "backfill",
        help="Manually trigger backfill for a session type"
    )
    backfill_parser.add_argument(
        "--session",
        type=str,
        default="status_1hr",
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help="Session type to backfill (default: status_1hr)"
    )
    backfill_parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days to backfill (default: 30)"
    )
    backfill_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only backfill these specific stations"
    )
    backfill_parser.add_argument(
        "--max-stations",
        type=int,
        help="Maximum number of stations to process"
    )
    backfill_parser.add_argument(
        "--archiving-mode",
        type=str,
        default="bulk",
        choices=["bulk", "immediate"],
        help="Archiving mode: bulk (download all then archive) or immediate (default: bulk)"
    )
    backfill_parser.set_defaults(func=cmd_scheduler_backfill)

    # Reconcile command
    reconcile_parser = scheduler_subparsers.add_parser(
        "reconcile",
        help="Reconcile SBF archives with RINEX output"
    )
    reconcile_parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days to scan (default: 30)"
    )
    reconcile_parser.add_argument(
        "--stations",
        nargs="+",
        help="Only check these specific stations"
    )
    reconcile_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be converted without actually converting"
    )
    reconcile_parser.set_defaults(func=cmd_scheduler_reconcile)

    return scheduler_parser


# Handle scheduler subcommands
def handle_scheduler_command(args) -> int:
    """Handle scheduler subcommands."""

    if not hasattr(args, 'scheduler_command') or not args.scheduler_command:
        print("❌ No scheduler command specified")
        print("Available commands: start, stop, restart, status, config, test, gaps, backfill, backfill-status, reconcile, pipeline-status, load-status, bootstrap")
        return 1

    return args.func(args)


if __name__ == "__main__":
    # Direct CLI testing
    parser = argparse.ArgumentParser(description="GPS Receiver Scheduler")
    subparsers = parser.add_subparsers(dest="command")
    
    create_scheduler_parser(subparsers)
    
    args = parser.parse_args()
    
    if args.command == "scheduler":
        sys.exit(handle_scheduler_command(args))
    else:
        parser.print_help()
        sys.exit(1)