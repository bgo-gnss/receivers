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
    - Python 3.6+
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
from datetime import datetime, timedelta, timezone
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
ARCHIVE_BASE_PATH = "/mnt/datadiskur/data"

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


def calculate_download_dates(lookback_days: int, session: str = "15s_24hr") -> List[datetime]:
    """Calculate dates/times to download based on session type.

    Args:
        lookback_days: Number of days to look back
        session: Session type (15s_24hr or 1Hz_1hr)

    Returns:
        List of datetime objects for files to download
    """
    now = datetime.now(timezone.utc)

    if session == "15s_24hr":
        # Daily session - generate one timestamp per day at 00:00:00
        end_date = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        start_date = end_date - timedelta(days=lookback_days)

        dates = []
        current = start_date
        while current < end_date:  # Exclude today
            dates.append(current)
            current += timedelta(days=1)

        return dates

    elif session == "1Hz_1hr":
        # Hourly session - generate one timestamp per hour
        # End at previous complete hour (not current incomplete hour)
        end_time = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
        # Start lookback_days * 24 hours before that
        start_time = end_time - timedelta(hours=lookback_days * 24)

        dates = []
        current = start_time
        while current < end_time:  # Exclude current incomplete hour
            dates.append(current)
            current += timedelta(hours=1)

        return dates

    else:
        raise ValueError(f"Unknown session type: {session}")


def discover_cachedir_prefix(ip: str, port: int, auth: HTTPBasicAuth,
                             logger: logging.Logger) -> str:
    """Auto-discover CACHEDIR prefix for NetR5 receivers.

    Returns:
        Base path prefix (empty for NetR9, /CACHEDIR{number}/download for NetR5)
    """
    try:
        # Test standard NetR9 path
        test_url = f"http://{ip}:{port}/prog/show?directory&path={quote('/Internal/')}"
        response = requests.get(test_url, auth=auth, timeout=30)

        if response.status_code == 200 and "ERROR" not in response.text.upper():
            # Standard NetR9 - no prefix
            logger.debug(f"Detected NetR9 receiver at {ip}:{port}")
            return ""

        # Try to find CACHEDIR prefix
        root_url = f"http://{ip}:{port}/"
        response = requests.get(root_url, auth=auth, timeout=30)

        if response.status_code == 200:
            # Parse HTML for CACHEDIR links
            match = re.search(r'CACHEDIR\d+', response.text)
            if match:
                cachedir = match.group(0)
                base_path = f"/{cachedir}/download"
                logger.info(f"✅ Detected NetR5 with CACHEDIR: {base_path}")
                return base_path

        # Fall back to no prefix
        logger.warning(f"Could not detect CACHEDIR for {ip}:{port}, using standard paths")
        return ""

    except Exception as e:
        logger.warning(f"CACHEDIR discovery failed for {ip}:{port}: {e}")
        return ""


def download_station_file(station_id: str, config: Dict, file_date: datetime,
                          base_path: str, auth: HTTPBasicAuth, session: str,
                          logger: logging.Logger, test_mode: bool = False) -> bool:
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

    Returns:
        True if download successful, False otherwise
    """
    # Get session configuration
    session_letter, remote_subdir = SESSION_CONFIG[session]

    # Build filename with optional underscore padding
    station_name = station_id
    if config["underscore_pad"]:
        station_name = station_id.ljust(10, '_')

    filename = file_date.strftime(f"{station_name}%Y%m%d%H%M{session_letter}.T02")

    # Build remote path with session-specific subdirectory
    remote_dir = file_date.strftime(f"/Internal/%Y%m/{remote_subdir}")

    # Build download URL
    if base_path:
        # NetR5 with CACHEDIR
        download_url = f"http://{config['ip']}:{config['port']}{base_path}{remote_dir}/{filename}"
    else:
        # NetR9 standard
        download_url = f"http://{config['ip']}:{config['port']}/download{remote_dir}/{filename}"

    # Build archive path: /mnt/gpsdata/YYYY/mon/STATION/15s_24hr/raw/STATION*.T02.gz
    month_name = file_date.strftime("%b").lower()
    year = file_date.strftime("%Y")

    # Archive filename (without underscore padding)
    archive_filename = file_date.strftime(f"{station_id}%Y%m%d%H%M{session_letter}.T02.gz")
    archive_path = Path(f"{ARCHIVE_BASE_PATH}/{year}/{month_name}/{station_id}/{session}/raw/{archive_filename}")

    # Check if file already exists
    if archive_path.exists():
        logger.debug(f"✓ {station_id}: {archive_filename} already exists")
        return True

    if test_mode:
        logger.info(f"TEST: Would download {station_id}/{filename} from {config['name']}")
        logger.info(f"      URL: {download_url}")
        logger.info(f"      Archive: {archive_path}")
        return True

    # Download file
    try:
        logger.info(f"Downloading {station_id}/{filename}")
        response = requests.get(download_url, auth=auth, stream=True, timeout=(30, 180))
        response.raise_for_status()

        # Create archive directory
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        # Write compressed file
        bytes_written = 0
        with gzip.open(archive_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)

        logger.info(f"✓ {station_id}: Downloaded {archive_filename} ({bytes_written/1024/1024:.1f} MB)")
        return True

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.warning(f"⚠ {station_id}: {filename} not found on receiver")
        else:
            logger.error(f"✗ {station_id}: HTTP error {e.response.status_code}: {e}")
        return False
    except Exception as e:
        logger.error(f"✗ {station_id}: Download failed: {e}")
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
            logger.info(f"Skipping {station_id}: Station does not support {session} session")
            return 0, 0

    logger.info(f"{'='*60}")
    logger.info(f"Station: {station_id} ({config['name']}) - {config['type']}")
    logger.info(f"Connection: {config['ip']}:{config['port']}")
    logger.info(f"Session: {session}")
    logger.info(f"Files to download: {len(dates)}")
    logger.info(f"{'='*60}")

    # Set up authentication
    auth = HTTPBasicAuth(config["user"], config["password"])

    # Discover CACHEDIR prefix for NetR5 receivers
    base_path = ""
    if config["type"] == "NetR5":
        base_path = discover_cachedir_prefix(config["ip"], config["port"], auth, logger)

    # Download files
    successful = 0
    for file_date in dates:
        if download_station_file(station_id, config, file_date, base_path, auth, session, logger, test_mode):
            successful += 1

    logger.info(f"Station {station_id}: {successful}/{len(dates)} files downloaded")
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

  # Download 1Hz hourly data (ISAF will be skipped - no 1Hz session)
  %(prog)s --session 1Hz_1hr

  # Download last 7 days of 15s data
  %(prog)s --days 7 --session 15s_24hr

  # Test mode (dry run)
  %(prog)s --test --session 1Hz_1hr
        """
    )

    parser.add_argument("stations", nargs="*",
                       help="Station IDs to download (default: all NATT stations)")
    parser.add_argument("-d", "--days", type=int, default=5,
                       help="Number of days to look back (default: 5)")
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
            logger.error(f"Invalid station IDs: {', '.join(invalid_stations)}")
            logger.error(f"Valid stations: {', '.join(sorted(NATT_STATIONS.keys()))}")
            sys.exit(3)
        stations_to_download = args.stations
    else:
        # Download all NATT stations
        stations_to_download = sorted(NATT_STATIONS.keys())

    # Calculate download dates (timestamps for hourly, dates for daily)
    dates = calculate_download_dates(args.days, args.session)

    logger.info(f"NATT Stations Download Script")
    logger.info(f"{'='*60}")
    logger.info(f"Stations: {', '.join(stations_to_download)}")
    logger.info(f"Session: {args.session}")
    logger.info(f"Date range: {dates[0].date()} to {dates[-1].date()}")
    logger.info(f"Total dates: {len(dates)}")
    logger.info(f"Test mode: {args.test}")
    logger.info(f"{'='*60}")

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
            logger.error(f"✗ Station {station_id} failed completely: {e}")
            failed_stations.append(station_id)
            total_files += len(dates)

    # Summary
    duration = time.time() - start_time
    logger.info(f"{'='*60}")
    logger.info(f"DOWNLOAD SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total files: {total_successful}/{total_files} successful")
    logger.info(f"Duration: {duration:.1f} seconds")

    if failed_stations:
        logger.warning(f"Failed stations: {', '.join(failed_stations)}")

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
