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
import importlib
import inspect
from pathlib import Path

# Import gps_parser for centralized config
try:
    import sys
    sys.path.append('../gps_parser/src')
    import gps_parser
    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False
    gps_parser = None


def get_available_receiver_types() -> dict:
    """Dynamically discover available receiver types by scanning receiver modules.
    
    Returns:
        dict: Maps receiver_type names to their module paths and classes
    """
    available_receivers = {}
    
    try:
        # Get the receivers package directory
        import receivers
        receivers_dir = Path(receivers.__file__).parent
        
        # Scan all subdirectories (manufacturer folders)
        for manufacturer_dir in receivers_dir.iterdir():
            if not manufacturer_dir.is_dir() or manufacturer_dir.name.startswith('_'):
                continue
                
            # Scan Python files in manufacturer directory
            for py_file in manufacturer_dir.glob('*.py'):
                if py_file.name.startswith('_'):
                    continue
                    
                module_name = f"receivers.{manufacturer_dir.name}.{py_file.stem}"
                try:
                    # Import the module
                    module = importlib.import_module(module_name)
                    
                    # Look for classes that inherit from BaseReceiver
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        # Check if it's a receiver class (has BaseReceiver in MRO)
                        if (hasattr(obj, '__module__') and 
                            obj.__module__ == module_name and
                            hasattr(obj, '__bases__')):
                            
                            # Check if it inherits from BaseReceiver but is not BaseReceiver itself
                            base_names = [base.__name__ for base in obj.__mro__]
                            if 'BaseReceiver' in base_names and name != 'BaseReceiver':
                                available_receivers[name] = {
                                    'class': obj,
                                    'module': module_name,
                                    'manufacturer': manufacturer_dir.name
                                }
                                
                except ImportError as e:
                    # Skip modules that can't be imported
                    logging.debug(f"Could not import {module_name}: {e}")
                    continue
                    
    except Exception as e:
        logging.warning(f"Failed to scan for receiver types: {e}")
        # Fallback to known receivers
        available_receivers = {'PolaRX5': {'class': PolaRX5, 'module': 'receivers.septentrio.polarx5', 'manufacturer': 'septentrio'}}
        
    return available_receivers


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


def get_station_config(station_id: str) -> Optional[dict]:
    """Get station configuration from gps_parser centralized configuration."""
    station_upper = station_id.upper()
    
    # Use gps_parser as primary configuration source
    if not HAS_GPS_PARSER:
        logging.error("gps_parser module not available - cannot load station configurations")
        return None
    
    try:
        parser = gps_parser.ConfigParser()
        station_info = parser.getStationInfo(station_upper)
        
        if not station_info or 'station' not in station_info:
            logging.warning(f"Station {station_upper} not found in stations.cfg")
            return None
            
        station_data = station_info['station']
        
        # Extract required configuration from gps_parser format
        router_ip = station_data.get('router_ip')
        ftp_port = station_data.get('receiver_ftpport')
        receiver_type = station_data.get('receiver_type')
        
        if not router_ip or not ftp_port:
            logging.warning(f"Station {station_upper} missing required config: router_ip={router_ip}, ftpport={ftp_port}")
            return None
        
        # Convert port to int if it's a string
        try:
            ftp_port = int(ftp_port)
        except (ValueError, TypeError):
            logging.warning(f"Station {station_upper} has invalid FTP port: {ftp_port}")
            return None
        
        # Check for supported receiver types using dynamic discovery
        available_receivers = get_available_receiver_types()
        supported_types = list(available_receivers.keys())
        
        if not receiver_type:
            logging.warning(f"⚠️  Station {station_upper} missing receiver_type in configuration")
            logging.warning(f"   Supported types: {', '.join(supported_types)}")
            logging.warning(f"   This station will be skipped - please add receiver_type to stations.cfg")
            return None
        elif receiver_type not in supported_types:
            logging.warning(f"⚠️  Station {station_upper} has unsupported receiver type: {receiver_type}")
            logging.warning(f"   Supported types: {', '.join(supported_types)}")
            logging.warning(f"   This station will be skipped until receiver support is implemented")
            if supported_types:
                manufacturers = set(info['manufacturer'] for info in available_receivers.values())
                logging.warning(f"   To add support, create: src/receivers/{receiver_type.lower()}/module.py")
                logging.warning(f"   Current manufacturers: {', '.join(manufacturers)}")
            return None
        
        # Get FTP mode using enhanced gps_parser rule-based system
        ftp_mode = parser.getStationFtpMode(station_upper, router_ip)
        
        # Build standardized config structure
        station_config = {
            'router': {'ip': router_ip},
            'receiver': {
                'ftpport': ftp_port, 
                'ftp_mode': ftp_mode,
                'type': receiver_type
            },
            'station': {
                'id': station_upper,
                'router_type': station_data.get('router_type'),
                'connection_type': station_data.get('connection_type')
            }
        }
        
        logging.debug(f"Loaded config for {station_upper}: {router_ip}:{ftp_port} ({receiver_type})")
        return station_config
        
    except Exception as e:
        logging.error(f"Failed to load configuration for {station_upper}: {e}")
        return None


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
            
            # Create receiver instance dynamically based on receiver_type
            receiver_type = station_config["receiver"]["type"]
            available_receivers = get_available_receiver_types()
            
            if receiver_type not in available_receivers:
                logger.error(f"⚠️  Receiver type {receiver_type} not available - this should not happen")
                total_errors += 1
                continue
                
            ReceiverClass = available_receivers[receiver_type]["class"]
            receiver = ReceiverClass(station_id, station_config)
            
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
            
        # Create receiver instance dynamically based on receiver_type
        receiver_type = station_config["receiver"]["type"]
        available_receivers = get_available_receiver_types()
        ReceiverClass = available_receivers[receiver_type]["class"]
        receiver = ReceiverClass(station_id, station_config)
        
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
            
        # Create receiver instance dynamically based on receiver_type
        receiver_type = station_config["receiver"]["type"]
        available_receivers = get_available_receiver_types()
        ReceiverClass = available_receivers[receiver_type]["class"]
        receiver = ReceiverClass(station_id, station_config)
        
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
        help=f"Number of days back to check for data (default: {default_days})"
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