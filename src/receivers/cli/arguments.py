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

    # Parallel download options
    parallel_group = parser.add_argument_group('parallel download options')
    parallel_group.add_argument(
        '--parallel',
        action='store_true',
        help='Download stations in parallel (grouped batches with stagger)'
    )
    parallel_group.add_argument(
        '--batches',
        type=int,
        default=None,
        metavar='N',
        help='Number of batch groups (default: from scheduler.yaml or 10)'
    )
    parallel_group.add_argument(
        '--distribution-window',
        type=float,
        default=None,
        metavar='MINUTES',
        dest='distribution_window',
        help='Minutes to spread batch groups across (default: from scheduler.yaml)'
    )
    parallel_group.add_argument(
        '--retry-delay',
        type=float,
        default=90.0,
        metavar='SECONDS',
        dest='retry_delay',
        help='Seconds before retrying unreachable stations (default: 90)'
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

    # Live-mode options (no date flags)
    live_group = parser.add_argument_group('live-mode options')
    live_group.add_argument(
        '--compact',
        action='store_true',
        help='Compact status display (same format as "status" command)'
    )

    live_group.add_argument(
        '--icinga',
        action='store_true',
        help='Send check results to Icinga monitoring system'
    )

    live_group.add_argument(
        '--no-files',
        action='store_true',
        help='Skip file system checks in live mode'
    )

    live_group.add_argument(
        '--no-ntrip',
        action='store_true',
        help='Skip NTRIP/RTK checks in live mode'
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


def setup_rec_config_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the rec-config subcommand parser."""
    parser = subparsers.add_parser(
        'rec-config',
        help='Extract or push configuration for Septentrio receivers',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''
Extract or push configuration for Septentrio PolaRx5 receivers via TCP.

By default, extracted configs are printed to stdout (Unix convention).
Use --save to store to file with naming: {ReceiverType}_{StationID}_{ConfigType}_{Timestamp}.txt

Examples:
  # Extract config to stdout
  receivers rec-config THOB --extract
  receivers rec-config THOB --extract --config-type Boot

  # Save config to file (uses rec_config_dir from receivers.cfg or /tmp/polarconfig/)
  receivers rec-config THOB --extract --save
  receivers rec-config THOB,ISFS --extract --save --output-dir ~/configs/

  # Push config to receiver
  receivers rec-config THOB --push config_file.txt
  receivers rec-config THOB --push config_file.txt --dry-run
  receivers rec-config THOB --push config_file.txt --no-save

  # Compare configs
  receivers rec-config THOB --extract --diff-with ~/configs/old_config.txt
        '''
    )

    parser.add_argument(
        'stations',
        metavar='STATIONS',
        help='Station ID(s), comma-separated (e.g., THOB or THOB,ISFS)'
    )

    # Operation mode (mutually exclusive)
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        '--extract',
        action='store_true',
        help='Extract configuration from receiver (prints to stdout by default)'
    )
    mode_group.add_argument(
        '--push',
        metavar='CONFIG_FILE',
        help='Push configuration file to receiver'
    )

    parser.add_argument(
        '--config-type',
        choices=['Current', 'Boot'],
        default='Current',
        help='Configuration type to extract (default: Current)'
    )

    parser.add_argument(
        '--save',
        action='store_true',
        help='Save extracted config to file (default: print to stdout)'
    )

    parser.add_argument(
        '--output-dir',
        metavar='DIR',
        help='Override output directory for --save (default: rec_config_dir from config or /tmp/polarconfig/)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without executing'
    )

    parser.add_argument(
        '--no-save',
        action='store_true',
        help='Do not save config to boot after push (default: saves to Boot)'
    )

    parser.add_argument(
        '--diff-with',
        metavar='FILE',
        help='Compare extracted config with existing file'
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


def setup_rinex_parser(subparsers) -> argparse.ArgumentParser:
    """Set up the rinex subcommand parser."""
    defaults = get_default_values()

    parser = subparsers.add_parser(
        'rinex',
        help='Convert raw GPS data to RINEX format',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''
Convert raw GPS receiver data (SBF, T02, T00, m00) to RINEX format with proper
header metadata from TOS database.

Supported formats:
  - Septentrio SBF (.sbf, .sbf.gz) - using sbf2rin
  - Trimble T02 (.T02) - using runpkr00 + GFZRNX
  - Trimble T00 (.T00) - using runpkr00 + GFZRNX
  - Leica m00 (.m00, .m00.gz) - using teqc + GFZRNX

Examples:
  # Convert last 7 days of daily data
  receivers rinex ELDC -d 7

  # Convert specific date range
  receivers rinex ELDC -s 20260101 -e 20260107

  # RINEX 3 with long naming convention
  receivers rinex ELDC -d 7 --version 3 --naming long

  # RINEX 2 with short naming (legacy)
  receivers rinex MANA --version 2 --naming short

  # Validate existing RINEX headers against TOS
  receivers rinex ELDC --validate-only -d 30

  # Dry run - show what would be done
  receivers rinex ELDC -d 7 --dry-run
        '''
    )

    # Positional: stations (multiple)
    parser.add_argument(
        'stations',
        nargs='+',
        metavar='STATION',
        help='Station IDs to convert (e.g., ELDC THOB MANA)'
    )

    # Date/time options
    date_group = parser.add_argument_group('date options')
    add_date_flags(date_group, include_days=True)

    # Format options
    format_group = parser.add_argument_group('format options')
    format_group.add_argument(
        '--session',
        type=str,
        default=defaults['session'],
        choices=['15s_24hr', '1Hz_1hr'],
        help=f"Data session type (default: {defaults['session']})"
    )

    format_group.add_argument(
        '--version', '-V',
        type=int,
        default=None,
        choices=[2, 3, 4],
        dest='rinex_version',
        help='RINEX version: 2, 3, or 4 (default: from receivers.cfg)'
    )

    format_group.add_argument(
        '--naming',
        type=str,
        default=None,
        choices=['short', 'long'],
        help='Filename convention: short (RINEX 2 style) or long (IGS) (default: from receivers.cfg)'
    )

    format_group.add_argument(
        '--observation-types',
        type=str,
        metavar='TYPES',
        help='Observation types to include (comma-separated, e.g., C1C,L1C,S1C)'
    )

    format_group.add_argument(
        '--native-trimble',
        action='store_true',
        help='Use native Trimble converter via Docker (requires trm2rinex image)'
    )

    # Metadata options
    meta_group = parser.add_argument_group('metadata options')
    meta_group.add_argument(
        '--no-header-correction',
        action='store_true',
        help='Skip TOS metadata header corrections'
    )

    meta_group.add_argument(
        '--force-config-metadata',
        action='store_true',
        help='Use current station config even for old data (skip TOS lookup)'
    )

    # Output options
    output_group = parser.add_argument_group('output options')
    output_group.add_argument(
        '--output-dir', '-o',
        type=str,
        metavar='DIR',
        help='Override output directory (default: archive path)'
    )

    output_group.add_argument(
        '--format',
        type=str,
        default=None,
        choices=['modern', 'legacy'],
        dest='output_format',
        help='Output format: modern (.rnx.gz) or legacy (.D.Z) (default: from config)'
    )

    output_group.add_argument(
        '--keep-intermediate',
        action='store_true',
        help='Keep intermediate files (e.g., .tgd from runpkr00)'
    )

    # Operation modes
    mode_group = parser.add_argument_group('operation modes')
    mode_group.add_argument(
        '--validate-only',
        action='store_true',
        help='Only validate existing RINEX headers against TOS (no conversion)'
    )

    mode_group.add_argument(
        '--rename-only',
        action='store_true',
        help='Only rename existing RINEX files to new convention (no conversion)'
    )

    mode_group.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes'
    )

    add_force_flag(parser)
    add_verbose_flag(parser)

    return parser


def setup_tools_parser(subparsers) -> argparse.ArgumentParser:
    """Set up argument parser for tools management subcommand."""
    parser = subparsers.add_parser(
        'tools',
        help='Manage RINEX conversion tools',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''
Manage external tools required for RINEX conversion.

Tools are installed to ~/.local/share/gps-rinex-tools/bin/ by default.

Supported tools:
  teqc      - Leica MDB/m00 to RINEX 2 (UNAVCO, auto-install)
  gfzrnx    - RINEX format conversion and QC (GFZ, auto-install)
  rnx2crx   - Hatanaka compression (GSI, auto-install)
  mdb2rinex - Leica MDB to RINEX 3 (Leica myWorld, manual)
  runpkr00  - Trimble T00/T02 extraction (Trimble, manual)
  sbf2rin   - Septentrio SBF to RINEX (Septentrio, manual)

Examples:
  receivers tools list              # Show all tools and status
  receivers tools install teqc      # Install specific tool
  receivers tools install-all       # Install all auto-installable tools
  receivers tools check             # Verify tools are working
  receivers tools configure         # Update receivers.cfg with tool paths
        '''
    )

    # Subcommands for tools
    tools_subparsers = parser.add_subparsers(
        dest='tools_command',
        title='tools commands',
    )

    # list
    list_parser = tools_subparsers.add_parser(
        'list',
        help='List all tools and their installation status'
    )

    # install
    install_parser = tools_subparsers.add_parser(
        'install',
        help='Install a specific tool'
    )
    install_parser.add_argument(
        'tool_name',
        help='Name of tool to install (teqc, gfzrnx, rnx2crx, etc.)'
    )
    install_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Force reinstall even if already installed'
    )

    # install-all
    install_all_parser = tools_subparsers.add_parser(
        'install-all',
        help='Install all auto-installable tools'
    )
    install_all_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Force reinstall even if already installed'
    )

    # check
    check_parser = tools_subparsers.add_parser(
        'check',
        help='Check tool availability for a receiver type'
    )
    check_parser.add_argument(
        '--receiver-type',
        help='Check tools needed for specific receiver type (e.g., G10, PolaRX5)'
    )

    # configure
    configure_parser = tools_subparsers.add_parser(
        'configure',
        help='Update receivers.cfg with installed tool paths'
    )
    configure_parser.add_argument(
        '--config',
        help='Path to receivers.cfg (default: ~/.config/gpsconfig/receivers.cfg)'
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
  receivers rinex ELDC -d 7

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
    setup_rec_config_parser(subparsers)
    setup_rinex_parser(subparsers)
    setup_tools_parser(subparsers)

    # Scheduler (optional - requires APScheduler)
    try:
        from .scheduler import create_scheduler_parser
        create_scheduler_parser(subparsers)
    except ImportError:
        pass

    # TOS integration (optional - requires tostools)
    try:
        from .tos import create_tos_parser
        create_tos_parser(subparsers)
    except ImportError:
        pass

    return parser
