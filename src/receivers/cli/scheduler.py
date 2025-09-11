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
    from ..scheduling.bulk_scheduler import BulkDownloadScheduler, create_scheduler_config, HAS_APSCHEDULER
except ImportError:
    HAS_APSCHEDULER = False


def cmd_scheduler_start(args) -> int:
    """Start the bulk download scheduler."""
    
    if not HAS_APSCHEDULER:
        print("❌ APScheduler not available. Install with: pip install apscheduler")
        return 1
        
    try:
        # Create scheduler with filtering options
        scheduler = BulkDownloadScheduler(
            production_mode=not args.verbose,
            max_workers=args.max_workers,
            station_filter=getattr(args, 'stations', None),
            max_stations_per_session=getattr(args, 'max_stations', None)
        )
        
        # Schedule all sessions
        scheduler.schedule_all_sessions()
        
        # Show scheduled jobs
        jobs = scheduler.get_scheduled_jobs()
        print(f"✅ Scheduled {len(jobs)} download jobs")
        
        if args.show_jobs:
            print("\\nScheduled jobs:")
            for job in sorted(jobs, key=lambda x: x['next_run'] or ''):
                next_run = job['next_run'] or 'Not scheduled'
                print(f"  {job['id']}: {next_run}")
        
        # Set up signal handling for graceful shutdown
        def signal_handler(signum, frame):
            print("\\n🛑 Shutting down scheduler...")
            scheduler.stop()
            sys.exit(0)
            
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        print(f"🚀 Starting scheduler with {args.max_workers} workers...")
        print("   Press Ctrl+C to stop")
        
        # Start scheduler (blocking)
        scheduler.start()
        
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
    
    if args.create:
        try:
            config_file = create_scheduler_config()
            print(f"✅ Created scheduler configuration: {config_file}")
            print("   Edit this file to customize scheduling behavior")
            return 0
        except Exception as e:
            print(f"❌ Failed to create configuration: {e}")
            return 1
            
    if args.show:
        config_file = Path.home() / '.config' / 'gps_receivers' / 'scheduler.json'
        if config_file.exists():
            with open(config_file) as f:
                config = json.load(f)
            print("📋 Current scheduler configuration:")
            print(json.dumps(config, indent=2))
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
        
    try:
        print("🧪 Testing scheduler setup...")
        
        # Create scheduler with filtering options
        scheduler = BulkDownloadScheduler(
            production_mode=True,
            station_filter=getattr(args, 'stations', None),
            max_stations_per_session=getattr(args, 'max_stations', None)
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
        default=5,
        help="Maximum number of concurrent downloads (default: 5)"
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
    test_parser.set_defaults(func=cmd_scheduler_test)
    
    return scheduler_parser


# Handle scheduler subcommands
def handle_scheduler_command(args) -> int:
    """Handle scheduler subcommands."""
    
    if not hasattr(args, 'scheduler_command') or not args.scheduler_command:
        print("❌ No scheduler command specified")
        print("Available commands: start, status, config, test")
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