#!/usr/bin/env python3
"""Standalone NATT stations download script for rek.vedur.is production.

This script downloads GPS data from NATT (Landmælingar Íslands) stations
using HTTP Basic Authentication. It is designed to run on rek.vedur.is
production server where the full receivers package is not installed.

Features:
- Self-contained with no external dependencies beyond standard library + requests
- Hard-coded station configurations for production reliability
- HTTP Basic Auth support for all NATT receivers
- NetR5 CACHEDIR auto-discovery for ISAF station
- Direct writing to /mnt/gpsdata/ production archive
- Cron-compatible with proper exit codes and logging
- Downloads last 5 days of 15s_24hr data (daily files)

Usage:
    # Download all NATT stations (default)
    python3 download_natt_stations.py

    # Download specific stations
    python3 download_natt_stations.py ISAF AKUR BLON

    # Custom lookback period
    python3 download_natt_stations.py --days 7

    # Test mode (dry run)
    python3 download_natt_stations.py --test

Requirements:
    - Python 3.4+
    - requests library (pip install requests)
    - Write access to /mnt/gpsdata/

Production Deployment:
    1. Copy this script to rek.vedur.is:/usr/local/bin/
    2. chmod +x /usr/local/bin/download_natt_stations.py
    3. Add to cron: 0 1 * * * /usr/local/bin/download_natt_stations.py

Exit Codes:
    0: Success (all stations downloaded successfully)
    1: Partial failure (some stations failed)
    2: Complete failure (all stations failed)
    3: Configuration error

Author: Benedikt Gunnar Ófeigsson
Date: 2025-10-13
"""

import argparse
import gzip
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

# External dependency
try:
    import requests
    from requests.auth import HTTPBasicAuth
except ImportError:
    print("ERROR: requests library not installed. Install with: pip install requests")
    sys.exit(3)

# NATT Station Configurations (hard-coded for production reliability)
NATT_STATIONS = {
    "AKUR": {
        "name": "Akureyri",
        "ip": "130.208.224.220",
        "port": 80,
        "type": "NetR9",
        "user": "LMI",
        "password": "mano1gps",
        "underscore_pad": False,
    },
    "ALHV": {
        "name": "Álftavatnsheidi",
        "ip": "157.157.171.152",
        "port": 7000,
        "type": "NetR9",
        "user": "LMI",
        "password": "sniegas14",
        "underscore_pad": False,
        "nonstandard_daily_time": True,  # Uses creation time instead of 0000
    },
    "BLON": {
        "name": "Blönduós",
        "ip": "157.157.171.244",
        "port": 7000,
        "type": "NetR9",
        "user": "LMI",
        "password": "mano1717",
        "underscore_pad": False,
    },
    "BJTV": {
        "name": "Bjartarstaðir",
        "ip": "194.144.208.152",
        "port": 7000,
        "type": "NetR9",
        "user": "LMI",
        "password": "21sako5",
        "underscore_pad": False,
    },
    "FIHO": {
        "name": "Fíflholt",
        "ip": "157.157.171.245",
        "port": 7000,
        "type": "NetR9",
        "user": "lmi",
        "password": "2naktis",
        "underscore_pad": False,
    },
    "GJFV": {
        "name": "Gjáfell",
        "ip": "157.157.249.56",
        "port": 7000,
        "type": "NetR9",
        "user": "LMI",
        "password": "25nieko12",
        "underscore_pad": False,
    },
    "GUSK": {
        "name": "Gufuskálar",
        "ip": "157.157.248.168",
        "port": 7000,
        "type": "NetR9",
        "user": "LMI",
        "password": "lietus14",
        "underscore_pad": False,
    },
    "HEID": {
        "name": "Heiðarsel",
        "ip": "157.157.145.17",
        "port": 7000,
        "type": "NetR9",
        "user": "LMI",
        "password": "saule12",
        "underscore_pad": False,
    },
    "ISAF": {
        "name": "Ísafjörður",
        "ip": "193.109.17.51",
        "port": 80,
        "type": "NetR5",
        "user": "LMI",
        "password": "piene16",
        "underscore_pad": True,  # NetR5 firmware bug
    },
    "LAVI": {
        "name": "Laugarvatn",
        "ip": "157.157.171.246",
        "port": 7000,
        "type": "NetR9",
        "user": "lmi",
        "password": "zebrasDE",
        "underscore_pad": False,
    },
    "RHOL": {
        "name": "Reykjahóll",
        "ip": "157.157.21.13",
        "port": 7000,
        "type": "NetR9",
        "user": "LMI",
        "password": "diena17",
        "underscore_pad": False,
    },
    "SKHA": {
        "name": "Skálholt",
        "ip": "157.157.145.85",
        "port": 7000,
        "type": "NetR9",
        "user": "LMI",
        "password": "vakaras12",
        "underscore_pad": False,
    },
    "VOFJ": {
        "name": "Vopnafjörður",
        "ip": "157.157.249.161",
        "port": 7000,
        "type": "NetR9",
        "user": "LMI",
        "password": "zebras5",
        "underscore_pad": False,
    },
}

# Production archive path on rek.vedur.is
# Can be overridden with NATT_ARCHIVE_PATH environment variable for testing
ARCHIVE_BASE_PATH = os.environ.get("NATT_ARCHIVE_PATH", "/mnt/datadiskur/data")

# Session configurations (session_type -> (session_letter, remote_subdir))
SESSION_CONFIG = {
    "15s_24hr": ("a", "15s_24hr"),  # Daily 15-second data files
    "1Hz_1hr": ("b", "1Hz_1hr"),    # Hourly 1Hz data files
}

# Stations that don't support certain sessions
STATION_SESSION_EXCLUSIONS = {
    "ISAF": ["1Hz_1hr"],  # ISAF (NetR5) only has 15s_24hr session
}


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Set up logging for cron compatibility."""
    logger = logging.getLogger("natt_download")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                   datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def calculate_download_dates(lookback: int, session: str = "15s_24hr") -> List[datetime]:
    """Calculate dates/times to download based on session type.

    Args:
        lookback: Number of days (for 15s_24hr) or hours (for 1Hz_1hr) to look back
        session: Session type (15s_24hr or 1Hz_1hr)

    Returns:
        List of datetime objects for files to download (newest first)
    """
    now = datetime.utcnow()

    if session == "15s_24hr":
        # Daily session - lookback is in DAYS
        # Generate one timestamp per day at 00:00:00
        end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = end_date - timedelta(days=lookback)

        dates = []
        current = start_date
        while current < end_date:  # Exclude today
            dates.append(current)
            current += timedelta(days=1)

        # Return in reverse chronological order (newest first)
        return list(reversed(dates))

    elif session == "1Hz_1hr":
        # Hourly session - lookback is in HOURS
        # Generate one timestamp per hour
        # End at previous complete hour (not current incomplete hour)
        end_time = now.replace(minute=0, second=0, microsecond=0)
        # Start lookback hours before that
        start_time = end_time - timedelta(hours=lookback)

        dates = []
        current = start_time
        while current < end_time:  # Exclude current incomplete hour
            dates.append(current)
            current += timedelta(hours=1)

        # Return in reverse chronological order (newest first)
        return list(reversed(dates))

    else:
        raise ValueError("Unknown session type: {}".format(session))


def get_average_file_size(station_id: str, session: str, lookback_days: int,
                         logger: logging.Logger) -> Optional[int]:
    """Calculate average file size from recent archive files.

    Args:
        station_id: Station ID
        session: Session type (15s_24hr or 1Hz_1hr)
        lookback_days: Number of days to look back for samples
        logger: Logger instance

    Returns:
        Average file size in bytes, or None if insufficient data
    """
    try:
        now = datetime.utcnow()
        sample_sizes = []

        # Check each day in lookback period
        for days_back in range(lookback_days):
            check_date = now - timedelta(days=days_back)
            month_name = check_date.strftime("%b").lower()
            year = check_date.strftime("%Y")

            archive_dir = Path("{}/{}/{}/{}/{}/raw".format(
                ARCHIVE_BASE_PATH, year, month_name, station_id, session))

            if not archive_dir.exists():
                continue

            # Sample files from this day
            try:
                day_pattern = check_date.strftime("{}%Y%m%d*.T02.gz".format(station_id))
                files = list(archive_dir.glob(day_pattern))
                for filepath in files:
                    size = os.path.getsize(str(filepath))
                    if size > 1024:  # Ignore tiny files (likely corrupt)
                        sample_sizes.append(size)
            except (OSError, IOError):
                continue

        if len(sample_sizes) >= 3:  # Need at least 3 samples
            avg_size = sum(sample_sizes) / len(sample_sizes)
            logger.debug("Average file size for {}/{}: {:.1f} KB (based on {} samples over {} days)".format(
                station_id, session, avg_size/1024, len(sample_sizes), lookback_days))
            return int(avg_size)
        else:
            logger.debug("Not enough historical data for {}/{} (only {} samples)".format(
                station_id, session, len(sample_sizes)))
            return None

    except Exception as e:
        logger.debug("Could not calculate average file size for {}/{}: {}".format(
            station_id, session, e))
        return None


def get_remote_file_size(download_url: str, auth: HTTPBasicAuth,
                        logger: logging.Logger) -> Optional[int]:
    """Get file size from remote server using HEAD request.

    Args:
        download_url: URL to check
        auth: HTTP Basic Auth
        logger: Logger instance

    Returns:
        File size in bytes, or None if not available
    """
    try:
        response = requests.head(download_url, auth=auth, timeout=10)
        if response.status_code == 200:
            content_length = response.headers.get('Content-Length')
            if content_length:
                return int(content_length)
    except Exception as e:
        logger.debug("Could not get remote file size: {}".format(e))
    return None


def find_actual_filename(station_id: str, file_date: datetime, session: str,
                        config: Dict, base_path: str, auth: HTTPBasicAuth,
                        logger: logging.Logger) -> Optional[str]:
    """Find actual filename on receiver for stations with non-standard naming.

    For stations like ALHV that use creation time instead of 0000 for daily files.

    Args:
        station_id: Station ID
        file_date: Date to search for
        session: Session type
        config: Station configuration
        base_path: Base path prefix
        auth: HTTP Basic Auth
        logger: Logger instance

    Returns:
        Actual filename found on receiver, or None if not found
    """
    try:
        session_letter, remote_subdir = SESSION_CONFIG[session]
        remote_dir = file_date.strftime("/Internal/%Y%m/{}".format(remote_subdir))

        # Build directory listing URL
        if base_path:
            # NetR5 with CACHEDIR
            list_url = "http://{}:{}/prog/show?directory&path={}".format(
                config['ip'], config['port'], quote(base_path + remote_dir))
        else:
            # NetR9 standard
            list_url = "http://{}:{}/prog/show?directory&path={}".format(
                config['ip'], config['port'], quote(remote_dir))

        logger.debug("Listing directory for {}: {}".format(station_id, list_url))
        response = requests.get(list_url, auth=auth, timeout=30)
        response.raise_for_status()

        # Parse directory listing for files matching date pattern
        # Looking for: ALHV20251013****a.T02 (any time for that date)
        date_pattern = file_date.strftime("{}%Y%m%d".format(station_id))

        # Parse HTML/text response for matching filenames
        for line in response.text.split('\n'):
            # Look for lines containing our date pattern
            if date_pattern in line and session_letter + '.T02' in line:
                # Extract filename using regex
                match = re.search(r'{}\d{{12}}{}\.(T02|t02)'.format(station_id, session_letter), line)
                if match:
                    found_filename = match.group(0)
                    logger.info("Found non-standard filename for {}: {}".format(station_id, found_filename))
                    return found_filename

        logger.warning("Could not find file for {} on {}".format(station_id, file_date.strftime("%Y-%m-%d")))
        return None

    except Exception as e:
        logger.debug("Could not discover filename for {}: {}".format(station_id, e))
        return None


def discover_cachedir_prefix(ip: str, port: int, auth: HTTPBasicAuth,
                             logger: logging.Logger) -> str:
    """Auto-discover CACHEDIR prefix for NetR5 receivers.

    Returns:
        Base path prefix (empty for NetR9, /CACHEDIR{number}/download for NetR5)
    """
    try:
        # Test standard NetR9 path
        test_url = "http://{}:{}/prog/show?directory&path={}".format(ip, port, quote('/Internal/'))
        response = requests.get(test_url, auth=auth, timeout=30)

        if response.status_code == 200 and "ERROR" not in response.text.upper():
            # Standard NetR9 - no prefix
            logger.debug("Detected NetR9 receiver at {}:{}".format(ip, port))
            return ""

        # Try to find CACHEDIR prefix
        root_url = "http://{}:{}/".format(ip, port)
        response = requests.get(root_url, auth=auth, timeout=30)

        if response.status_code == 200:
            # Parse HTML for CACHEDIR links
            match = re.search(r'CACHEDIR\d+', response.text)
            if match:
                cachedir = match.group(0)
                base_path = "/{}/download".format(cachedir)
                logger.info("✅ Detected NetR5 with CACHEDIR: {}".format(base_path))
                return base_path

        # Fall back to no prefix
        logger.warning("Could not detect CACHEDIR for {}:{}, using standard paths".format(ip, port))
        return ""

    except Exception as e:
        logger.warning("CACHEDIR discovery failed for {}:{}: {}".format(ip, port, e))
        return ""


def download_station_file(station_id: str, config: Dict, file_date: datetime,
                          base_path: str, auth: HTTPBasicAuth, session: str,
                          logger: logging.Logger, test_mode: bool = False,
                          avg_file_size: Optional[int] = None) -> bool:
    """Download a single file from a NATT station.

    Args:
        station_id: Station ID (e.g., 'ISAF')
        config: Station configuration dictionary
        file_date: Date for file to download
        base_path: Base path prefix (for NetR5 CACHEDIR)
        auth: HTTP Basic Auth object
        session: Session type (15s_24hr or 1Hz_1hr)
        logger: Logger instance
        test_mode: If True, don't actually download
        avg_file_size: Average file size from recent data (optional)

    Returns:
        True if download successful, False otherwise
    """
    # Get session configuration
    session_letter, remote_subdir = SESSION_CONFIG[session]

    # Build filename with optional underscore padding
    station_name = station_id
    if config["underscore_pad"]:
        station_name = station_id.ljust(10, '_')

    # Check if station has non-standard daily naming (e.g., ALHV uses creation time)
    actual_filename = None
    if config.get("nonstandard_daily_time", False) and session == "15s_24hr":
        # Need to discover actual filename on receiver
        actual_filename = find_actual_filename(station_id, file_date, session, config, base_path, auth, logger)
        if not actual_filename:
            logger.warning("⚠ {}: Could not find file for {}".format(station_id, file_date.strftime("%Y-%m-%d")))
            return False
        filename = actual_filename
    else:
        # Standard filename
        filename = file_date.strftime("{}%Y%m%d%H%M{}.T02".format(station_name, session_letter))

    # Build remote path with session-specific subdirectory
    remote_dir = file_date.strftime("/Internal/%Y%m/{}".format(remote_subdir))

    # Build download URL
    if base_path:
        # NetR5 with CACHEDIR
        download_url = "http://{}:{}{}{}/{}".format(config['ip'], config['port'], base_path, remote_dir, filename)
    else:
        # NetR9 standard
        download_url = "http://{}:{}/download{}/{}".format(config['ip'], config['port'], remote_dir, filename)

    # Build archive path: /mnt/gpsdata/YYYY/mon/STATION/15s_24hr/raw/STATION*.T02.gz
    month_name = file_date.strftime("%b").lower()
    year = file_date.strftime("%Y")

    # Archive filename (always standardized - without underscore padding, with correct time)
    # Note: Even if receiver uses non-standard naming (e.g., ALHV with creation time),
    # we save to archive with standardized naming (0000 for daily, correct hour for hourly)
    archive_filename = file_date.strftime("{}%Y%m%d%H%M{}.T02.gz".format(station_id, session_letter))
    archive_path = Path("{}/{}/{}/{}/{}/raw/{}".format(ARCHIVE_BASE_PATH, year, month_name, station_id, session, archive_filename))

    # Check if file already exists
    if archive_path.exists():
        local_size = os.path.getsize(str(archive_path))

        # Get remote file size for comparison
        remote_size = get_remote_file_size(download_url, auth, logger)

        if remote_size is not None and remote_size > 0:
            # Compare local to remote size (allow 30% deviation)
            size_ratio = float(local_size) / float(remote_size)
            if 0.7 <= size_ratio <= 1.3:
                logger.debug("✓ {}: {} already exists (local: {:.1f} KB, remote: {:.1f} KB)".format(
                    station_id, archive_filename, local_size/1024, remote_size/1024))
                return True
            else:
                logger.warning("⚠ {}: {} size mismatch (local: {:.1f} KB, remote: {:.1f} KB, ratio: {:.1%}), re-downloading".format(
                    station_id, archive_filename, local_size/1024, remote_size/1024, size_ratio))
                # File will be overwritten below
        elif avg_file_size is not None and avg_file_size > 0:
            # Fallback: compare to 7-day average (allow 30% deviation)
            size_ratio = float(local_size) / float(avg_file_size)
            if 0.7 <= size_ratio <= 1.3:
                logger.debug("✓ {}: {} already exists (local: {:.1f} KB, avg: {:.1f} KB)".format(
                    station_id, archive_filename, local_size/1024, avg_file_size/1024))
                return True
            else:
                logger.warning("⚠ {}: {} deviates from average (local: {:.1f} KB, avg: {:.1f} KB, ratio: {:.1%}), re-downloading".format(
                    station_id, archive_filename, local_size/1024, avg_file_size/1024, size_ratio))
                # File will be overwritten below
        else:
            # No size data available - use simple threshold
            if local_size > 10240:  # 10KB minimum
                logger.debug("✓ {}: {} already exists ({:.1f} KB, no size data for comparison)".format(
                    station_id, archive_filename, local_size/1024))
                return True
            else:
                logger.warning("⚠ {}: {} exists but too small ({} bytes), re-downloading".format(
                    station_id, archive_filename, local_size))
                # File will be overwritten below

    if test_mode:
        logger.info("TEST: Would download {}/{} from {}".format(station_id, filename, config['name']))
        logger.info("      URL: {}".format(download_url))
        logger.info("      Archive: {}".format(archive_path))
        return True

    # Download file
    try:
        if actual_filename:
            # Show both actual and standardized filenames for clarity
            logger.info("Downloading {}/{} -> saving as {}".format(station_id, filename, archive_filename))
        else:
            logger.info("Downloading {}/{}".format(station_id, filename))

        response = requests.get(download_url, auth=auth, stream=True, timeout=(30, 180))
        response.raise_for_status()

        # Create archive directory
        os.makedirs(str(archive_path.parent), exist_ok=True)

        # Write compressed file
        bytes_written = 0
        with gzip.open(str(archive_path), 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)

        if actual_filename:
            logger.info("✓ {}: Downloaded {} -> saved as {} ({:.1f} MB)".format(
                station_id, filename, archive_filename, bytes_written/1024/1024))
        else:
            logger.info("✓ {}: Downloaded {} ({:.1f} MB)".format(
                station_id, archive_filename, bytes_written/1024/1024))
        return True

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.warning("⚠ {}: {} not found on receiver".format(station_id, filename))
        else:
            logger.error("✗ {}: HTTP error {}: {}".format(station_id, e.response.status_code, e))
        return False
    except Exception as e:
        logger.error("✗ {}: Download failed: {}".format(station_id, e))
        return False


def download_station(station_id: str, dates: List[datetime], session: str,
                     logger: logging.Logger, test_mode: bool = False) -> Tuple[int, int]:
    """Download files for a single NATT station.

    Args:
        station_id: Station ID
        dates: List of dates to download
        session: Session type (15s_24hr or 1Hz_1hr)
        logger: Logger instance
        test_mode: If True, don't actually download

    Returns:
        Tuple of (successful_downloads, total_files)
    """
    config = NATT_STATIONS[station_id]

    # Check if station supports this session
    if station_id in STATION_SESSION_EXCLUSIONS:
        if session in STATION_SESSION_EXCLUSIONS[station_id]:
            logger.info("Skipping {}: Station does not support {} session".format(station_id, session))
            return 0, 0

    logger.info("{}".format('='*60))
    logger.info("Station: {} ({}) - {}".format(station_id, config['name'], config['type']))
    logger.info("Connection: {}:{}".format(config['ip'], config['port']))
    logger.info("Session: {}".format(session))
    logger.info("Files to download: {}".format(len(dates)))
    logger.info("{}".format('='*60))

    # Set up authentication
    auth = HTTPBasicAuth(config["user"], config["password"])

    # Calculate 7-day average file size for validation
    avg_file_size = get_average_file_size(station_id, session, 7, logger)

    # Discover CACHEDIR prefix for NetR5 receivers
    base_path = ""
    if config["type"] == "NetR5":
        base_path = discover_cachedir_prefix(config["ip"], config["port"], auth, logger)

    # Download files
    successful = 0
    for file_date in dates:
        if download_station_file(station_id, config, file_date, base_path, auth, session, logger, test_mode, avg_file_size):
            successful += 1

    logger.info("Station {}: {}/{} files downloaded".format(station_id, successful, len(dates)))
    return successful, len(dates)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Download GPS data from NATT stations with HTTP Basic Auth",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download all NATT stations (default: last 5 days, 15s_24hr session)
  %(prog)s

  # Download specific stations
  %(prog)s ISAF AKUR BLON

  # Download 1Hz hourly data - last 6 hours (ISAF will be skipped - no 1Hz session)
  %(prog)s --session 1Hz_1hr --days 6

  # Download last 7 days of 15s data
  %(prog)s --days 7 --session 15s_24hr

  # Download last 24 hours of 1Hz data
  %(prog)s --days 24 --session 1Hz_1hr

  # Test mode (dry run)
  %(prog)s --test --session 1Hz_1hr
        """
    )

    parser.add_argument("stations", nargs="*",
                       help="Station IDs to download (default: all NATT stations)")
    parser.add_argument("-d", "--days", type=int, default=5,
                       help="Lookback period: DAYS for 15s_24hr, HOURS for 1Hz_1hr (default: 5)")
    parser.add_argument("-s", "--session", type=str, default="15s_24hr",
                       choices=["15s_24hr", "1Hz_1hr"],
                       help="Session type to download (default: 15s_24hr)")
    parser.add_argument("-v", "--verbose", action="store_true",
                       help="Verbose logging")
    parser.add_argument("-t", "--test", action="store_true",
                       help="Test mode (dry run - don't download)")

    args = parser.parse_args()

    # Set up logging
    logger = setup_logging(args.verbose)

    # Determine which stations to download
    if args.stations:
        # Validate station IDs
        invalid_stations = [s for s in args.stations if s not in NATT_STATIONS]
        if invalid_stations:
            logger.error("Invalid station IDs: {}".format(', '.join(invalid_stations)))
            logger.error("Valid stations: {}".format(', '.join(sorted(NATT_STATIONS.keys()))))
            sys.exit(3)
        stations_to_download = args.stations
    else:
        # Download all NATT stations
        stations_to_download = sorted(NATT_STATIONS.keys())

    # Calculate download dates (timestamps for hourly, dates for daily)
    dates = calculate_download_dates(args.days, args.session)

    logger.info("NATT Stations Download Script")
    logger.info("{}".format('='*60))
    logger.info("Stations: {}".format(', '.join(stations_to_download)))
    logger.info("Session: {}".format(args.session))

    if args.session == "15s_24hr":
        logger.info("Lookback: {} days".format(args.days))
        logger.info("Date range: {} to {}".format(dates[0].date(), dates[-1].date()))
    elif args.session == "1Hz_1hr":
        logger.info("Lookback: {} hours".format(args.days))
        logger.info("Time range: {} to {}".format(dates[0].strftime("%Y-%m-%d %H:%M"), dates[-1].strftime("%Y-%m-%d %H:%M")))

    logger.info("Total files: {}".format(len(dates)))
    logger.info("Test mode: {}".format(args.test))
    logger.info("{}".format('='*60))

    # Download each station
    start_time = time.time()
    total_successful = 0
    total_files = 0
    failed_stations = []

    for station_id in stations_to_download:
        try:
            successful, total = download_station(station_id, dates, args.session, logger, args.test)
            total_successful += successful
            total_files += total

            if successful < total:
                failed_stations.append(station_id)

        except Exception as e:
            logger.error("✗ Station {} failed completely: {}".format(station_id, e))
            failed_stations.append(station_id)
            total_files += len(dates)

    # Summary
    duration = time.time() - start_time
    logger.info("{}".format('='*60))
    logger.info("DOWNLOAD SUMMARY")
    logger.info("{}".format('='*60))
    logger.info("Total files: {}/{} successful".format(total_successful, total_files))
    logger.info("Duration: {:.1f} seconds".format(duration))

    if failed_stations:
        logger.warning("Failed stations: {}".format(', '.join(failed_stations)))

    # Exit codes
    if total_successful == total_files:
        logger.info("✓ All downloads successful")
        sys.exit(0)
    elif total_successful > 0:
        logger.warning("⚠ Partial success")
        sys.exit(1)
    else:
        logger.error("✗ All downloads failed")
        sys.exit(2)


if __name__ == "__main__":
    main()
