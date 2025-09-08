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
from typing import List, Optional

import gtimes.timefunc as gt
from gtimes.timefunc import currDatetime

from ..septentrio.polarx5 import PolaRX5
from ..base.exceptions import ConfigurationError, ConnectionError

# Import gps_parser for centralized config
try:
    import sys
    sys.path.append('../gps_parser/src')
    import gps_parser
    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False
    gps_parser = None


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Set up logging for CLI commands."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    return logging.getLogger("receivers")


def parse_datetime(date_str: str) -> datetime:
    """Parse datetime string in format YYYYMMDD-HHMM or YYYYMMDD."""
    if "-" in date_str:
        return datetime.strptime(date_str, "%Y%m%d-%H%M")
    else:
        return datetime.strptime(date_str, "%Y%m%d")


def get_station_config(station_id: str) -> dict:
    """Get station configuration from gps_parser with getSeptentrio2 fallback."""
    station_upper = station_id.upper()
    
    # Try gps_parser first (centralized config)
    if HAS_GPS_PARSER:
        try:
            parser = gps_parser.ConfigParser()
            station_info = parser.getStationInfo(station_upper)
            
            if station_info and 'station' in station_info:
                station_data = station_info['station']
                
                # Extract router IP from gps_parser format
                router_ip = station_data.get('router_ip')
                if not router_ip:
                    # Handle different field naming in gps_parser
                    router_ip = station_data.get('Router_IP')
                
                if router_ip:
                    # Build config from gps_parser data
                    ftp_port = int(station_data.get('receiver_ftpport', 
                                                  station_data.get('Receiver_FTP_PORT', 2160)))
                    
                    station_config = {
                        'router': {'ip': router_ip},
                        'receiver': {'ftpport': ftp_port, 'ftp_mode': 'auto'}
                    }
                    return station_config
                    
        except Exception as e:
            # Log but continue to fallback - don't fail hard on gps_parser issues
            logging.warning(f"gps_parser failed for {station_upper}: {e}")
    
    # Fallback to hardcoded mapping from getSeptentrio2 (for operational reliability)
    STATION_IP_MAPPING = {
        # Extended network stations (10.6.x.x range) - commonly used
        'ELDC': "10.6.1.90",
        'ROTH': "10.6.1.97",
        'SVIE': "10.6.1.87", 
        'HAHV': "10.6.1.68",
        'SAUD': "10.6.1.64",
        'INTA': "10.6.1.93",
        'AUST': "10.6.1.156",
        'ORFC': "10.6.1.91",
        'GFUM': "10.6.1.58",
        'KAST': "10.6.1.102",
        
        # Internal network stations (10.4.x.x range) - commonly used
        'THOB': "10.4.2.28",
        'HUSM': "10.4.1.240",
        'GJAC': "10.4.1.226", 
        'HLID': "10.4.1.238",
        'VMEY': "10.4.2.81",
        'VOGS': "10.4.2.8",
        'HVOL': "10.4.2.23",
        'HVER': "10.4.2.25",
        'SLEC': "10.4.2.224",
        'BUDH': "10.4.2.26",
        'SVIN': "10.4.2.22",
        'ENTC': "10.4.2.96",
        'INGC': "10.4.1.23",
        'KVIC': "10.4.1.24", 
        'KOTC': "10.4.1.21",
        
        # Test station
        'TEST': "10.4.2.235",
        
        # Domain-based stations (*.gps.vedur.is) - commonly requested  
        'NYLA': "nyla.gps.vedur.is",
        'VONC': "vonc.gps.vedur.is",
        'OLKE': "OLKE.gps.vedur.is",
        'KALF': "kalf.gps.vedur.is",
        'KVEC': "kvec.gps.vedur.is",
        'HRIC': "hric.gps.vedur.is",
        'GOLA': "GOLA.gps.vedur.is",
        'SKRO': "skro.gps.vedur.is",
        'URHC': "URHC.gps.vedur.is",
        'HAFS': "HAFS.gps.vedur.is",
        'SOHO': "SOHO.gps.vedur.is",
        # TODO: Add remaining 60+ stations as needed from operational config
    }
    
    if station_upper not in STATION_IP_MAPPING:
        # Return None to indicate missing configuration - caller should handle warning
        return None
    
    # Build configuration from fallback mapping
    station_config = {
        'router': {'ip': STATION_IP_MAPPING[station_upper]},
        'receiver': {'ftpport': 2160, 'ftp_mode': 'auto'}  # Most stations use auto-detect mode
    }
    
    return station_config


def cmd_download(args) -> int:
    """Download command - main data download functionality."""
    logger = setup_logging(args.loglevel)
    logger.info(f"Starting download for stations: {args.stations}")
    
    # Process time arguments (from getSeptentrio3 logic)
    start_time = None
    end_time = None
    
    if args.start:
        start_time = parse_datetime(args.start)
    
    if args.end:
        end_time = parse_datetime(args.end)
    
    # Default to days back if no start/end specified
    if not start_time and args.days:
        start_time = currDatetime(days=-args.days, refday=datetime.now())
    
    if not end_time:
        end_time = datetime.now() - timedelta(days=1)  # Default to yesterday
    
    # For hourly sessions, extend end_time to end of day to capture all hours
    if args.session == "status_1hr" and start_time and end_time:
        if start_time.date() == end_time.date():  # Same day
            end_time = end_time.replace(hour=23, minute=0, second=0, microsecond=0)
    
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
            
            # Create PolaRX5 instance
            receiver = PolaRX5(station_id, station_config)
            
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
                loglevel=args.loglevel
            )
            
            # Report results
            files_downloaded = result.get('files_downloaded', 0)
            total_downloaded += files_downloaded
            
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
            logger.error(f"Unexpected error processing {station_id}: {e}")
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
            
        receiver = PolaRX5(station_id, station_config)
        
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
            
        receiver = PolaRX5(station_id, station_config)
        
        if args.analyze_status:
            # Detailed health analysis from status session files
            analysis = receiver.analyze_health_data()
            
            if 'error' in analysis:
                print(f"❌ Error: {analysis['error']}")
                if 'suggestion' in analysis:
                    print(f"💡 Suggestion: {analysis['suggestion']}")
                return 1
            
            print(analysis.get('health_report', 'No health report available'))
            
            # Save detailed analysis to CSV if requested
            if args.save_csv and analysis.get('dataframe_available'):
                from ..septentrio.health_analyzer import HealthDataAnalyzer
                analyzer = HealthDataAnalyzer(analysis['ascii_directory'])
                analyzer.load_all_files()
                csv_path = f"{station_id}_health_analysis.csv"
                analyzer.save_health_data_csv(csv_path)
                print(f"\n📊 Detailed data saved to: {csv_path}")
            
        else:
            # Basic health check (connection status)
            health = receiver.get_health_status()
            
            print(f"Station: {health['station_id']}")
            print(f"Receiver Type: {health['receiver_type']}")
            print(f"Overall Status: {health['overall_status']}")
            print(f"Timestamp: {health['timestamp']}")
            
            # Connection details
            conn = health.get('connection', {})
            print(f"Connection: {'✅' if conn.get('receiver') else '❌'}")
        
        return 0
        
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return 1


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
    download_parser.add_argument(
        "-D", "--days",
        type=int,
        default=10,
        help="Number of days back to check for data (default: 10)"
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
        default="15s_24hr",
        choices=["15s_24hr", "1Hz_1hr", "status_1hr"],
        help="Data sampling session (default: 15s_24hr)"
    )
    
    download_parser.add_argument(
        "-comp", "--compression",
        type=str,
        default=".gz",
        help="Compression type (default: .gz)"
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
        "-a", "--analyze-status",
        action="store_true",
        help="Analyze detailed health data from status session ASCII files"
    )
    health_parser.add_argument(
        "--save-csv",
        action="store_true", 
        help="Save detailed health analysis to CSV file (requires --analyze-status)"
    )
    health_parser.set_defaults(func=cmd_health)
    
    return parser


def main() -> int:
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
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