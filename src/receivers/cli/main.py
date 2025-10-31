#!/usr/bin/env python3
"""
receivers CLI - GPS Receiver Data Management Tool

Enhanced command-line interface for downloading and managing GPS receiver data.
Migrated from getSeptentrio3 with modern subcommand architecture.

Usage:
    receivers download STATION [STATION...] [OPTIONS]
    receivers status STATION
    receivers health STATION
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import gtimes.timefunc as gt
from gtimes.timefunc import currDatetime

from ..base.exceptions import ConfigurationError, ConnectionError
from ..base.type_validator import ReceiverTypeValidator
from ..base.receiver_factory import get_receiver_factory, create_receiver
from ..utils.time_utils import calculate_download_time_range

# Import gps_parser for centralized config
try:
    import sys
    sys.path.append('../gps_parser/src')
    import gps_parser
    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False
    gps_parser = None

# Import station config from utility to avoid circular imports
from ..config_utils import get_station_config


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Set up logging for CLI commands."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    return logging.getLogger("receivers")


def parse_datetime(date_str: str) -> datetime:
    """Parse datetime string in format YYYYMMDD-HHMM or YYYYMMDD."""
    if "-" in date_str:
        return datetime.strptime(date_str, "%Y%m%d-%H%M")
    else:
        return datetime.strptime(date_str, "%Y%m%d")


# get_station_config function moved to config_utils.py to avoid circular imports


def cmd_download(args) -> int:
    """Download command - main data download functionality."""
    
    # Set up production logging if requested
    if getattr(args, 'production', False) or getattr(args, 'json_log', False):
        from ..base.production_logging import setup_production_logging
        production_config = setup_production_logging(
            json_output=getattr(args, 'json_log', False),
            verbose=(args.loglevel == logging.DEBUG)
        )
        logger = production_config.create_station_logger('receivers')
        audit_logger = production_config.get_audit_logger()
    else:
        logger = setup_logging(args.loglevel)
        audit_logger = None
    
    logger.info(f"Starting download for stations: {args.stations}")
    
    # Process time arguments (from getSeptentrio3 logic)
    start_time = None
    end_time = None
    reverse_chronological = False  # Default for explicit --start/--end

    if args.start:
        start_time = parse_datetime(args.start)

    if args.end:
        end_time = parse_datetime(args.end)
    
    # Default to time periods back if no start/end specified (use shared time_utils)
    if not start_time and args.days:
        # -D flag used: prioritize latest data (reverse chronological)
        reverse_chronological = True

        # Use shared time utility - single source of truth for time calculation
        # This implements correct "previous complete period" logic
        start_time, end_time = calculate_download_time_range(
            session_type=args.session,
            lookback_periods=args.days
        )

    # If explicit --start or --end provided, honor them
    if args.start and not end_time:
        # User provided start but no end - calculate reasonable end
        if args.session and "1hr" in args.session:
            end_time = start_time + timedelta(hours=1)
        else:
            end_time = start_time + timedelta(days=1)

    if args.end and not start_time:
        # User provided end but no start - calculate reasonable start
        if args.session and "1hr" in args.session:
            start_time = end_time - timedelta(hours=1)
        else:
            start_time = end_time - timedelta(days=1)
    
    # Process session frequency arguments (from getSeptentrio3)
    afrequency = args.afrequency or args.session.split("_")[0]
    ffrequency = args.ffrequency or args.session.split("_")[1]
    
    # Convert frequency to gtimes format
    frequency_mapping = {
        "24hr": "1D",  # Daily
        "1hr": "1H",   # Hourly
    }
    ffrequency = frequency_mapping.get(ffrequency, ffrequency)
    
    logger.info(f"Time range: {start_time} to {end_time}")
    logger.info(f"Session: {args.session}, File frequency: {ffrequency}, Acquisition frequency: {afrequency}")
    
    # Download for each station
    total_downloaded = 0
    total_errors = 0
    
    for station_id in args.stations:
        station_id = station_id.upper()
        logger.info(f"Processing station: {station_id}")
        
        try:
            # Get station configuration
            station_config = get_station_config(station_id)
            if station_config is None:
                logger.warning(f"⚠️  Station {station_id} not found in configuration - SKIPPING")
                total_errors += 1
                continue
            
            # Validate required configuration values
            try:
                ip = station_config["router"]["ip"]
                port = station_config["receiver"]["ftpport"]
                if not ip or not port:
                    logger.warning(f"⚠️  Station {station_id} missing IP ({ip}) or port ({port}) - SKIPPING")
                    total_errors += 1
                    continue
            except KeyError as e:
                logger.warning(f"⚠️  Station {station_id} configuration missing required key {e} - SKIPPING")
                total_errors += 1
                continue
            
            # Create receiver instance using factory pattern
            receiver = create_receiver(station_id, station_config)
            
            # Test connection if requested
            if args.test_connection:
                status = receiver.get_connection_status()
                if not status.get('receiver'):
                    logger.error(f"Connection test failed for {station_id}: {status.get('error')}")
                    total_errors += 1
                    continue
                logger.info(f"Connection test successful for {station_id}")
            
            # Download data
            result = receiver.download_data(
                start=start_time,
                end=end_time,
                session=args.session,
                ffrequency=ffrequency,
                afrequency=afrequency,
                compression=args.compression,
                sync=args.sync,
                clean_tmp=args.clean_tmp,
                archive=args.archive,
                reverse_chronological=reverse_chronological,
                loglevel=args.loglevel
            )
            
            # Report results
            files_downloaded = result.get('files_downloaded', 0)
            total_downloaded += files_downloaded
            
            # Log to audit trail if production logging enabled
            if audit_logger:
                audit_logger.log_download_session(station_id, {
                    'session': args.session,
                    'status': result.get('status', 'unknown'),
                    'duration': result.get('duration', 0),
                    'files_downloaded': files_downloaded,
                    'bytes_downloaded': result.get('total_bytes', 0),
                    'errors': result.get('errors', 0),
                    'start_time': start_time.isoformat() if start_time else None,
                    'end_time': end_time.isoformat() if end_time else None,
                    'connection_time': getattr(receiver, '_last_connection_time', None)
                })
            
            logger.info(f"Station {station_id}: {files_downloaded} files downloaded")
            logger.info(f"Status: {result.get('status')}, Duration: {result.get('duration', 0):.2f}s")
            
            if files_downloaded > 0:
                logger.info("Downloaded files:")
                for file_path in result.get('downloaded_files', []):
                    logger.info(f"  - {file_path}")
            
        except (ConfigurationError, ConnectionError) as e:
            logger.error(f"Error processing {station_id}: {e}")
            total_errors += 1
        except Exception as e:
            import traceback
            logger.error(f"Unexpected error processing {station_id}: {e}")
            logger.debug(f"Traceback:\n{traceback.format_exc()}")
            total_errors += 1
    
    # Final summary
    logger.info(f"Download complete. Total files: {total_downloaded}, Errors: {total_errors}")
    return 0 if total_errors == 0 else 1


def cmd_status(args) -> int:
    """Status command - check receiver connection status."""
    logger = setup_logging(args.loglevel)
    station_id = args.station.upper()
    
    try:
        station_config = get_station_config(station_id)
        if station_config is None:
            logger.warning(f"⚠️  Station {station_id} not found in configuration")
            return 1
            
        # Create receiver instance using factory pattern
        receiver = create_receiver(station_id, station_config)
        
        status = receiver.get_connection_status()
        
        print(f"Station: {station_id}")
        print(f"IP: {status.get('ip')}:{status.get('port')}")
        print(f"Router Status: {'✅' if status.get('router') else '❌'}")
        print(f"Receiver Status: {'✅' if status.get('receiver') else '❌'}")
        
        if status.get('error'):
            print(f"Error: {status['error']}")
            return 1
            
        return 0
        
    except Exception as e:
        logger.error(f"Status check failed: {e}")
        return 1


def cmd_health(args) -> int:
    """Health command - get receiver health information."""
    logger = setup_logging(args.loglevel)
    station_id = args.station.upper()

    try:
        station_config = get_station_config(station_id)
        if station_config is None:
            logger.warning(f"⚠️  Station {station_id} not found in configuration")
            return 1

        # Create receiver instance using factory pattern
        receiver = create_receiver(station_id, station_config)

        # Get comprehensive health status
        health = receiver.get_health_status()

        # Save to JSON if requested
        if getattr(args, 'save_json', False):
            json_path = receiver.save_health_to_json(health)
            if json_path:
                logger.info(f"Saved health data to {json_path}")

        # Save to database if requested
        if getattr(args, 'save_db', False):
            success = receiver.save_health_to_database(health)
            if success:
                logger.info("Saved health data to database")
            else:
                logger.warning("Failed to save health data to database")

        # Output format
        if getattr(args, 'json', False):
            # JSON output
            import json
            print(json.dumps(health, indent=2, default=str))
        else:
            # Human-readable output
            print(f"Station: {health['station_id']}")
            print(f"Receiver Type: {health['receiver_type']}")
            print(f"Timestamp: {health.get('timestamp', 'N/A')}")
            print(f"Overall Status: {health.get('overall_status', 'unknown').upper()}")

            # Connection summary
            connection = health.get('connection', {})
            if connection:
                print(f"\nConnection Health:")
                for level, data in connection.items():
                    status = data.get('status', 'unknown')
                    emoji = '✅' if status == 'ok' else '⚠️' if status == 'warning' else '❌'
                    print(f"  {level}: {emoji} {status}")

            # Metrics summary
            metrics = health.get('metrics', {})
            if metrics:
                print(f"\nMetrics:")
                for metric, data in metrics.items():
                    if isinstance(data, dict):
                        value = data.get('value', data.get('voltage', data.get('percent', 'N/A')))
                        unit = data.get('unit', '')
                        status = data.get('status', 'unknown')
                        print(f"  {metric}: {value} {unit} [{status}]")

            # Status summary
            summary = health.get('status_summary', {})
            if summary:
                print(f"\nStatus Summary:")
                print(f"  Healthy: {summary.get('healthy', 0)}")
                print(f"  Warning: {summary.get('warning', 0)}")
                print(f"  Critical: {summary.get('critical', 0)}")

        return 0

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        if getattr(args, 'verbose', False):
            import traceback
            traceback.print_exc()
        return 1


def cmd_validate_web_accuracy(args) -> int:
    """Validate configuration accuracy using web interface scraping."""
    import sys
    from pathlib import Path
    
    # Add the receivers root directory to path
    receivers_root = Path(__file__).parent.parent.parent.parent
    sys.path.insert(0, str(receivers_root))
    
    try:
        from config_accuracy_validator import ConfigAccuracyValidator
        
        validator = ConfigAccuracyValidator()
        
        # Get stations to validate
        if args.stations:
            station_ids = [s.upper() for s in args.stations]
        else:
            # Get all configured stations
            station_configs = get_all_station_configs()
            station_ids = list(station_configs.keys())
        
        if not station_ids:
            print("❌ No stations found to validate")
            return 1
        
        # Run web accuracy validation
        results, summary = validator.validate_multiple_stations(station_ids)
        
        if args.summary:
            # Show summary only
            print(f"\n📊 CONFIGURATION ACCURACY SUMMARY")
            print(f"{'='*50}")
            print(f"Total stations: {summary['total_stations']}")
            print(f"✅ Correct configs: {summary['correct_configs']}")
            print(f"❌ Mismatched configs: {summary['mismatched_configs']}")
            print(f"⚠️  Unverifiable: {summary['unverifiable_configs']}")
            
            if summary['type_mismatches']:
                print(f"\n🔧 Type mismatches: {', '.join(summary['type_mismatches'])}")
            
            if summary['name_mismatches']:
                print(f"🏷️  Name mismatches: {', '.join(summary['name_mismatches'])}")
            
            accuracy_rate = summary['correct_configs'] / summary['total_stations'] * 100
            print(f"\n📈 Configuration accuracy: {accuracy_rate:.1f}%")
        else:
            # Show detailed results
            print(f"\n📋 DETAILED CONFIGURATION VALIDATION")
            print(f"{'='*60}")
            
            for station_id, result in results.items():
                print(f"\n🏢 {station_id}:")
                print(f"   IP: {result['ip']}")
                
                if result['status'] == 'error':
                    print(f"   ❌ ERROR: {result['error']}")
                    continue
                
                if result.get('actual_type'):
                    type_status = "✅" if result.get('type_match', True) else "❌"
                    print(f"   {type_status} Type: {result['configured_type']} vs {result['actual_type']}")
                
                if result.get('actual_station_name'):
                    name_status = "✅" if result.get('name_match', True) else "❌"
                    print(f"   {name_status} Name: {station_id} vs {result['actual_station_name']}")
                
                if result.get('type_mismatch'):
                    print(f"      🔧 Fix: {result['type_mismatch']['suggested_fix']}")
                
                if result.get('name_mismatch'):
                    print(f"      🔧 Fix: {result['name_mismatch']['suggested_fix']}")
        
        return 0 if summary['mismatched_configs'] == 0 else 1
        
    except ImportError as e:
        print(f"❌ Web accuracy validation requires additional dependencies: {e}")
        print("   Install with: pip install beautifulsoup4")
        return 1
    except Exception as e:
        print(f"❌ Web accuracy validation failed: {e}")
        return 1


def cmd_validate(args) -> int:
    """Validate command - check receiver type configuration accuracy."""
    logger = setup_logging(args.loglevel)
    
    # Check if web accuracy validation was requested
    if args.web_accuracy:
        return cmd_validate_web_accuracy(args)
    
    try:
        # Initialize validator
        validator = ReceiverTypeValidator(logger)
        
        # Get receiver factory for available types
        factory = get_receiver_factory()
        available_types = list(factory.get_available_types().keys())
        logger.info(f"Available receiver types: {', '.join(available_types)}")

        # Get stations to validate
        if args.stations:
            # Validate specific stations
            station_ids = [s.upper() for s in args.stations]
            station_configs = {}
            for station_id in station_ids:
                config = get_station_config(station_id)
                if config:
                    station_configs[station_id] = config
                else:
                    logger.warning(f"Station {station_id} not found in configuration")
        else:
            # Validate all stations
            logger.info("Validating all stations in configuration...")
            station_configs = get_all_station_configs()

        if not station_configs:
            logger.error("No stations found to validate")
            return 1

        # Run validation
        logger.info(f"Validating receiver types for {len(station_configs)} stations...")
        results = validator.batch_validate_stations(station_configs)
        
        # Analyze results
        matches = sum(1 for r in results.values() if r.get('validation_status') == 'match')
        mismatches = sum(1 for r in results.values() if r.get('validation_status') == 'mismatch')
        unreachable = sum(1 for r in results.values() if r.get('validation_status') == 'unreachable')
        errors = sum(1 for r in results.values() if r.get('validation_status') == 'error')
        
        # Print summary
        print(f"\n=== RECEIVER TYPE VALIDATION RESULTS ===")
        print(f"Total stations validated: {len(results)}")
        print(f"✅ Correct receiver types: {matches}")
        print(f"❌ Mismatched receiver types: {mismatches}")
        print(f"🔌 Unreachable stations: {unreachable}")
        print(f"⚠️  Errors: {errors}")
        
        # Show mismatches in detail
        if mismatches > 0:
            print(f"\n=== RECEIVER TYPE MISMATCHES ===")
            for station_id, result in results.items():
                if result.get('validation_status') == 'mismatch':
                    configured = result.get('configured_type', 'Unknown')
                    detected = ', '.join(result.get('detected_types', []))
                    suggestion = result.get('suggestion', {})
                    recommended = suggestion.get('recommended_type', 'Unknown')
                    confidence = suggestion.get('confidence', 0)
                    
                    print(f"\n📡 {station_id} ({result.get('ip', 'Unknown IP')})")
                    print(f"   Configured: {configured}")
                    print(f"   Detected:   {detected}")
                    print(f"   Recommended: {recommended} (confidence: {confidence:.1%})")
        
        # Generate correction report if requested
        if args.report:
            report = validator.generate_correction_report(results)
            print(f"\n{report}")
            
            # Save report to file
            report_file = f"receiver_validation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            with open(report_file, 'w') as f:
                f.write(report)
            print(f"📄 Detailed report saved to: {report_file}")
        
        # Auto-fix if requested (EXPERIMENTAL)
        if args.fix and mismatches > 0:
            logger.warning("⚠️  AUTO-FIX is EXPERIMENTAL - backup your stations.cfg first!")
            response = input("Do you want to proceed with auto-corrections? [y/N]: ")
            
            if response.lower() in ['y', 'yes']:
                fixed_count = apply_receiver_type_corrections(results)
                print(f"🔧 Applied corrections to {fixed_count} stations")
                if fixed_count > 0:
                    print("⚠️  Please restart receivers service and verify functionality")
            else:
                print("Auto-fix cancelled")
        
        # Return appropriate exit code
        return 0 if (mismatches == 0 and errors == 0) else 1
        
    except Exception as e:
        logger.error(f"Validation failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


def get_all_station_configs() -> Dict[str, Dict[str, Any]]:
    """Get configurations for all stations.
    
    Returns:
        Dictionary mapping station_id to configuration
    """
    if not HAS_GPS_PARSER:
        logging.error("gps_parser not available - cannot load all stations")
        return {}
    
    try:
        import configparser
        parser = gps_parser.ConfigParser()
        config = configparser.ConfigParser()
        config.read(parser.get_stations_config_path())
        
        stations = {}
        for section in config.sections():
            try:
                station_config = get_station_config(section)
                if station_config:
                    stations[section] = station_config
            except Exception as e:
                logging.debug(f"Could not load config for {section}: {e}")
        
        return stations
    except Exception as e:
        logging.error(f"Could not load all station configurations: {e}")
        return {}


def apply_receiver_type_corrections(validation_results: Dict[str, Dict[str, Any]]) -> int:
    """Apply receiver type corrections to stations.cfg (EXPERIMENTAL).
    
    Args:
        validation_results: Results from validation
        
    Returns:
        Number of corrections applied
    """
    # TODO: Implement auto-correction to stations.cfg
    # This would require:
    # 1. Reading stations.cfg
    # 2. Updating receiver_type fields for mismatched stations
    # 3. Writing back to stations.cfg
    # 4. Validating the changes
    
    logging.warning("Auto-correction not yet implemented - use --report to get manual corrections")
    return 0


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser with getSeptentrio3 compatibility."""
    parser = argparse.ArgumentParser(
        prog="receivers",
        description="GPS Receiver Data Management Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  receivers download ELDC --sync --archive
  receivers download THOB ELDC --days 7 --session 15s_24hr
  receivers download ELDC --start 20250905 --end 20250906
  receivers status ELDC
  receivers health THOB
        """
    )
    
    # Global options
    parser.add_argument(
        "-v", "--verbose",
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.INFO,
        help="Enable verbose output"
    )
    
    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Download subcommand (main functionality from getSeptentrio3)
    download_parser = subparsers.add_parser(
        "download",
        help="Download data from GPS receivers",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    download_parser.add_argument(
        "stations",
        nargs="+",
        help="List of stations to download (e.g., ELDC THOB)"
    )
    
    # Time range options (from getSeptentrio3)
    # Get default values from gps_parser configuration
    try:
        if HAS_GPS_PARSER:
            parser_config = gps_parser.ConfigParser()
            default_days = parser_config.getDefaultValue('default_days_back')
            default_session = parser_config.getDefaultValue('default_session')
            default_compression = parser_config.getDefaultValue('default_compression')
        else:
            # Fallback values if gps_parser not available
            default_days = 10
            default_session = "15s_24hr"
            default_compression = ".gz"
    except Exception:
        # Fallback values on any gps_parser error
        default_days = 10
        default_session = "15s_24hr"
        default_compression = ".gz"
    
    download_parser.add_argument(
        "-D", "--days",
        type=int,
        default=default_days,
        help=f"Number of time periods back to check for data. For daily sessions (15s_24hr): days back. For hourly sessions (1Hz_1hr, status_1hr): hours back (default: {default_days})"
    )
    
    download_parser.add_argument(
        "-s", "--start",
        type=str,
        help="Start date, format YYYYMMDD or YYYYMMDD-HHMM"
    )
    
    download_parser.add_argument(
        "-e", "--end", 
        type=str,
        help="End date, format YYYYMMDD or YYYYMMDD-HHMM"
    )
    
    # Session options (from getSeptentrio3)
    download_parser.add_argument(
        "-se", "--session",
        type=str,
        default=default_session,
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help=f"Data sampling session (default: {default_session})"
    )
    
    download_parser.add_argument(
        "-comp", "--compression",
        type=str,
        default=default_compression,
        help=f"Compression type (default: {default_compression})"
    )
    
    download_parser.add_argument(
        "-ffr", "--ffrequency",
        type=str,
        default="",
        help="Data file frequency (auto-detected from session if not specified)"
    )
    
    download_parser.add_argument(
        "-afr", "--afrequency",
        type=str,
        default="",
        help="Acquisition frequency (auto-detected from session if not specified)"
    )
    
    # Download behavior options (from getSeptentrio3)
    download_parser.add_argument(
        "-sy", "--sync",
        action="store_true",
        help="Sync new or partial files from source (enable actual download)"
    )
    
    download_parser.add_argument(
        "-cl", "--clean_tmp",
        action="store_true",
        help="Clean download directory and start over on partial downloads"
    )
    
    download_parser.add_argument(
        "-ar", "--archive",
        action="store_true",
        help="Archive the downloaded data to final location"
    )
    
    download_parser.add_argument(
        "-t", "--test-connection",
        action="store_true",
        help="Test connection before attempting download"
    )
    
    # Production logging options
    download_parser.add_argument(
        "--json-log",
        action="store_true",
        help="Output logs in JSON format for monitoring systems"
    )
    
    download_parser.add_argument(
        "--production",
        action="store_true",
        help="Enable production logging mode (concise, structured output)"
    )

    download_parser.add_argument(
        "-v", "--verbose",
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.INFO,
        help="Enable verbose output"
    )

    download_parser.set_defaults(func=cmd_download)
    
    # Status subcommand
    status_parser = subparsers.add_parser("status", help="Check receiver connection status")
    status_parser.add_argument("station", help="Station ID to check")
    status_parser.add_argument(
        "-v", "--verbose",
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.INFO,
        help="Enable verbose output"
    )
    status_parser.set_defaults(func=cmd_status)
    
    # Health subcommand  
    health_parser = subparsers.add_parser("health", help="Get receiver health information")
    health_parser.add_argument("station", help="Station ID to check")
    health_parser.add_argument(
        "-v", "--verbose",
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.INFO,
        help="Enable verbose output"
    )
    health_parser.add_argument(
        "--json",
        action="store_true",
        help="Output health data in JSON format"
    )
    health_parser.add_argument(
        "--save-json",
        action="store_true",
        help="Save health data to JSON file in status_1hr/health/"
    )
    health_parser.add_argument(
        "--save-db",
        action="store_true",
        help="Save health data to PostgreSQL database"
    )
    health_parser.set_defaults(func=cmd_health)
    
    # Validate subcommand - check receiver type configuration
    validate_parser = subparsers.add_parser("validate", help="Validate receiver type configuration")
    validate_parser.add_argument(
        "stations", 
        nargs="*", 
        help="Station IDs to validate (if none provided, validates all stations)"
    )
    validate_parser.add_argument(
        "--fix",
        action="store_true",
        help="Automatically fix mismatched receiver types in station.cfg (EXPERIMENTAL)"
    )
    validate_parser.add_argument(
        "--report",
        action="store_true", 
        help="Generate detailed correction report"
    )
    validate_parser.add_argument(
        "--web-accuracy", "-w",
        action="store_true",
        help="Validate config accuracy using web interface scraping (more reliable)"
    )
    validate_parser.add_argument(
        "--summary", "-s", 
        action="store_true",
        help="Show summary report only"
    )
    validate_parser.add_argument(
        "-v", "--verbose",
        action="store_const",
        dest="loglevel",
        const=logging.INFO,
        help="Enable verbose output"
    )
    validate_parser.set_defaults(func=cmd_validate)
    
    # Scheduler subcommand (bulk downloads) 
    try:
        from .scheduler import create_scheduler_parser
        create_scheduler_parser(subparsers)
    except ImportError:
        # APScheduler not available - add placeholder
        scheduler_parser = subparsers.add_parser(
            "scheduler", 
            help="Bulk download scheduler (requires APScheduler)"
        )
        scheduler_parser.set_defaults(func=lambda args: print(
            "❌ Scheduler requires APScheduler. Install with: pip install apscheduler"
        ))
    
    return parser


def main() -> int:
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    # Handle scheduler subcommands
    if args.command == "scheduler":
        try:
            from .scheduler import handle_scheduler_command
            return handle_scheduler_command(args)
        except ImportError:
            print("❌ Scheduler requires APScheduler. Install with: pip install apscheduler")
            return 1
    
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        return 130
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())