"""
Standardized CLI argument definitions for receivers commands.

Flag Conventions:
- All long flags use kebab-case (--start-date, not --start_date)
- Short flags reserved for frequently used options
- Common flags shared across subcommands where applicable

Common Flags:
  -v, --verbose     Verbose output (all commands)
  -f, --force       Force overwrite/redo (download, health)

Date Flags (download, health):
  -s, --start       Start date YYYYMMDD
  -e, --end         End date YYYYMMDD
  -d, --days        Number of periods back
"""

import argparse
import logging
from typing import Optional

# Try to get defaults from gps_parser
try:
    import gps_parser
    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False
    gps_parser = None


def get_default_values():
    """Get default values from gps_parser or use fallbacks."""
    try:
        if HAS_GPS_PARSER:
            parser_config = gps_parser.ConfigParser()
            return {
                'days': parser_config.getDefaultValue('default_days_back') or 10,
                'session': parser_config.getDefaultValue('default_session') or '15s_24hr',
                'compression': parser_config.getDefaultValue('default_compression') or '.gz',
            }
    except Exception:
        pass

    return {
        'days': 10,
        'session': '15s_24hr',
        'compression': '.gz',
    }


def add_verbose_flag(parser: argparse.ArgumentParser) -> None:
    """Add standard verbose flag."""
    parser.add_argument(
        '-v', '--verbose',
        action='store_const',
        dest='loglevel',
        const=logging.DEBUG,
        default=logging.INFO,
        help='Enable verbose output'
    )


def add_force_flag(parser: argparse.ArgumentParser) -> None:
    """Add standard force flag."""
    parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Force overwrite even if data unchanged'
    )


def add_date_flags(parser: argparse.ArgumentParser, include_days: bool = True) -> None:
    """Add standard date range flags.

    Args:
        parser: ArgumentParser to add flags to
        include_days: Whether to include --days flag
    """
    parser.add_argument(
        '-s', '--start',
        type=str,
        metavar='YYYYMMDD',
        help='Start date (format: YYYYMMDD or YYYYMMDD-HHMM)'
    )

    parser.add_argument(
        '-e', '--end',
        type=str,
        metavar='YYYYMMDD',
        help='End date (format: YYYYMMDD or YYYYMMDD-HHMM)'
    )

    if include_days:
        defaults = get_default_values()
        parser.add_argument(
            '-d', '--days',
            type=int,
            default=defaults['days'],
            metavar='N',
            help=f"Number of periods back (default: {defaults['days']})"
        )


def setup_download_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the download subcommand parser."""
    defaults = get_default_values()

    parser = subparsers.add_parser(
        'download',
        help='Download data from GPS receivers',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''
Download GPS receiver data for specified stations.

Examples:
  receivers download ELDC THOB --sync --archive
  receivers download ELDC -s 20260101 -e 20260107 --sync
  receivers download ELDC -d 5 --session 1Hz_1hr --sync
        '''
    )

    # Positional: stations (multiple)
    parser.add_argument(
        'stations',
        nargs='+',
        metavar='STATION',
        help='Station IDs to download (e.g., ELDC THOB)'
    )

    # Date/time options
    add_date_flags(parser, include_days=True)

    # Session options
    parser.add_argument(
        '--session',
        type=str,
        default=defaults['session'],
        choices=['15s_24hr', '1Hz_1hr', 'status_1hr'],
        help=f"Data session type (default: {defaults['session']})"
    )

    parser.add_argument(
        '--compression',
        type=str,
        default=defaults['compression'],
        metavar='EXT',
        help=f"Compression type (default: {defaults['compression']})"
    )

    # Download behavior
    parser.add_argument(
        '--sync',
        action='store_true',
        help='Enable actual download (sync new/partial files)'
    )

    parser.add_argument(
        '--archive',
        action='store_true',
        help='Archive downloaded data to final location'
    )

    parser.add_argument(
        '--clean',
        action='store_true',
        dest='clean_tmp',  # Keep internal name for compatibility
        help='Clean temp directory before download'
    )

    parser.add_argument(
        '--test-connection',
        action='store_true',
        help='Test connection before download'
    )

    # Production/logging options
    parser.add_argument(
        '--production',
        action='store_true',
        help='Enable production logging (concise, structured)'
    )

    parser.add_argument(
        '--json-log',
        action='store_true',
        help='Output logs in JSON format (for monitoring systems)'
    )

    # Advanced options (rarely used)
    advanced = parser.add_argument_group('advanced options')
    advanced.add_argument(
        '--file-frequency',
        type=str,
        default='',
        metavar='FREQ',
        dest='ffrequency',
        help='Override file frequency (auto-detected from session)'
    )

    advanced.add_argument(
        '--acq-frequency',
        type=str,
        default='',
        metavar='FREQ',
        dest='afrequency',
        help='Override acquisition frequency (auto-detected from session)'
    )

    add_verbose_flag(parser)

    return parser


def setup_status_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the status subcommand parser."""
    parser = subparsers.add_parser(
        'status',
        help='Quick receiver status check',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''Quick status check for a GPS receiver.

Shows a compact overview of receiver health including connection status,
voltage, temperature, and CPU load. Uses the same data source as the
'health' command but with simplified output for quick checks.

Examples:
  # Quick status check
  receivers status ISFS

  # JSON output for scripting
  receivers status ISFS --json
'''
    )

    parser.add_argument(
        'stations',
        metavar='STATION',
        nargs='+',
        help='Station ID(s) to check'
    )

    parser.add_argument(
        '--json',
        action='store_true',
        help='Output as JSON (same format as health command)'
    )

    parser.add_argument(
        '--save-db',
        action='store_true',
        help='Save health data to database'
    )

    parser.add_argument(
        '--icinga',
        action='store_true',
        help='Send check results to Icinga monitoring system'
    )

    add_verbose_flag(parser)

    return parser


def setup_health_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the health subcommand parser."""
    parser = subparsers.add_parser(
        'health',
        help='Get receiver health information',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''
Get health metrics from GPS receiver or extract historical data.

Live health check (no date flags):
  receivers health ISFS
  receivers health ISFS --json

Extract historical data:
  receivers health ISFS -s 20260113              # Single day
  receivers health ISFS -s 20260110 -e 20260113  # Date range
  receivers health ISFS -d 24                    # Last 24 hours (spans 1-2 days)

Multiple stations:
  receivers health ELDC THOB ISFS --import-json  # Import all
  receivers health ELDC ELEY --export-json       # Export all
  receivers health ELDC THOB -s 20260101 -e 20260115 --save-db
        '''
    )

    parser.add_argument(
        'stations',
        metavar='STATION',
        nargs='+',
        help='Station ID(s) to process (e.g., ISFS or ELDC THOB ISFS)'
    )

    # Date options for extraction (same as download command)
    date_group = parser.add_argument_group('extraction options')
    add_date_flags(date_group, include_days=False)  # Add -s, -e

    date_group.add_argument(
        '-d', '--days',
        type=int,
        metavar='N',
        help='Extract N periods back (hours for status_1hr, consistent with download)'
    )

    date_group.add_argument(
        '--extract-all',
        action='store_true',
        help='Extract all available SBF files'
    )

    date_group.add_argument(
        '--import-json',
        action='store_true',
        help='Import existing JSON health files to database'
    )

    date_group.add_argument(
        '--export-json',
        action='store_true',
        help='Export health data from database to JSON files'
    )

    date_group.add_argument(
        '--json-dir',
        type=str,
        metavar='PATH',
        help='Directory for JSON health files (auto-detected if not specified)'
    )

    # Output options
    output_group = parser.add_argument_group('output options')
    output_group.add_argument(
        '--json',
        action='store_true',
        help='Output health data as JSON'
    )

    output_group.add_argument(
        '--save-json',
        action='store_true',
        help='Save health data to JSON file'
    )

    output_group.add_argument(
        '--save-db',
        action='store_true',
        help='Save health data to PostgreSQL database'
    )

    # Extraction behavior
    behavior_group = parser.add_argument_group('behavior options')
    add_force_flag(behavior_group)

    behavior_group.add_argument(
        '--skip-blocks',
        action='store_true',
        help='Skip per-block JSON extraction'
    )

    add_verbose_flag(parser)

    return parser


def setup_validate_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the validate subcommand parser."""
    parser = subparsers.add_parser(
        'validate',
        help='Validate receiver type configuration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''
Validate that configured receiver types match actual hardware.

Examples:
  receivers validate              # Validate all stations
  receivers validate ELDC THOB    # Validate specific stations
  receivers validate --web        # Use web scraping (more accurate)
        '''
    )

    parser.add_argument(
        'stations',
        nargs='*',
        metavar='STATION',
        help='Station IDs to validate (default: all stations)'
    )

    parser.add_argument(
        '--fix',
        action='store_true',
        help='Auto-fix mismatched receiver types (EXPERIMENTAL)'
    )

    parser.add_argument(
        '--report',
        action='store_true',
        help='Generate detailed correction report'
    )

    parser.add_argument(
        '--web', '-w',
        action='store_true',
        dest='web_accuracy',
        help='Use web interface scraping (more accurate)'
    )

    parser.add_argument(
        '--summary',
        action='store_true',
        help='Show summary only'
    )

    add_verbose_flag(parser)

    return parser


def setup_push_config_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the push-config subcommand parser."""
    parser = subparsers.add_parser(
        'push-config',
        help='Push configuration to Septentrio receivers',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''
Push configuration commands to Septentrio PolaRx5 receivers via TCP.

Examples:
  receivers push-config THOB config_file.txt
  receivers push-config THOB,ISFS config_file.txt
  receivers push-config THOB config_file.txt --dry-run
  receivers push-config THOB config_file.txt --no-save
        '''
    )

    parser.add_argument(
        'stations',
        metavar='STATIONS',
        help='Station ID(s), comma-separated (e.g., THOB or THOB,ISFS)'
    )

    parser.add_argument(
        'config_file',
        metavar='CONFIG_FILE',
        help='Configuration file with receiver commands'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be sent without actually sending'
    )

    parser.add_argument(
        '--no-save',
        action='store_true',
        help='Do not save config to boot (default: saves to both Current and Boot)'
    )

    parser.add_argument(
        '--port',
        type=int,
        metavar='PORT',
        help='Override control port (default from config: 28784)'
    )

    parser.add_argument(
        '--timeout',
        type=float,
        default=10,
        metavar='SECONDS',
        help='Connection timeout in seconds (default: 10)'
    )

    add_verbose_flag(parser)

    return parser


def create_argument_parser() -> argparse.ArgumentParser:
    """Create the main argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog='receivers',
        description='GPS Receiver Data Management Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  receivers download ELDC --sync --archive
  receivers status THOB
  receivers health ISFS --json
  receivers validate --web

For subcommand help: receivers <command> --help
        '''
    )

    # Global verbose (applies if no subcommand)
    add_verbose_flag(parser)

    # Subcommands
    subparsers = parser.add_subparsers(
        dest='command',
        title='commands',
        description='Available commands'
    )

    # Add all subcommand parsers
    setup_download_parser(subparsers)
    setup_status_parser(subparsers)
    setup_health_parser(subparsers)
    setup_validate_parser(subparsers)
    setup_push_config_parser(subparsers)

    # Scheduler (optional - requires APScheduler)
    try:
        from .scheduler import create_scheduler_parser
        create_scheduler_parser(subparsers)
    except ImportError:
        pass

    return parser
