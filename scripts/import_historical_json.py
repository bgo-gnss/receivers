#!/usr/bin/env python3
"""Import historical JSON health data to PostgreSQL block tables.

This script reads JSON health files from the status_1hr/json directories
and imports the timeseries data to the new block-aligned database schema.

Usage:
    python scripts/import_historical_json.py [--station STATION] [--data-path PATH] [--dry-run]

Examples:
    # Import all stations
    python scripts/import_historical_json.py

    # Import specific station
    python scripts/import_historical_json.py --station ISFS

    # Dry run (show what would be imported)
    python scripts/import_historical_json.py --dry-run
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from receivers.health.db_writer import HealthDatabaseWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


def find_json_files(data_path: Path, station: Optional[str] = None) -> List[Path]:
    """Find all health JSON files in the data directory.

    Args:
        data_path: Base data path (e.g., /home/bgo/tmp/gpsdata)
        station: Optional station filter

    Returns:
        List of JSON file paths
    """
    json_files = []

    # Search pattern: {year}/{month}/{station}/status_1hr/json/*_health.json
    for year_dir in sorted(data_path.glob("20*")):
        if not year_dir.is_dir():
            continue
        for month_dir in sorted(year_dir.glob("*")):
            if not month_dir.is_dir():
                continue
            for station_dir in sorted(month_dir.glob("*")):
                if not station_dir.is_dir():
                    continue
                if station and station_dir.name != station:
                    continue

                json_dir = station_dir / "status_1hr" / "json"
                if json_dir.exists():
                    for json_file in sorted(json_dir.glob("*_health.json")):
                        json_files.append(json_file)

    return json_files


def parse_health_json(json_path: Path) -> Dict[str, Any]:
    """Parse a health JSON file.

    Args:
        json_path: Path to JSON file

    Returns:
        Parsed JSON data
    """
    with open(json_path) as f:
        return json.load(f)


def import_json_file(
    writer: HealthDatabaseWriter,
    json_path: Path,
    dry_run: bool = False
) -> int:
    """Import a single JSON file to the database.

    Args:
        writer: Database writer instance
        json_path: Path to JSON file
        dry_run: If True, don't actually write to database

    Returns:
        Number of samples imported
    """
    try:
        data = parse_health_json(json_path)

        station_id = data.get("station_id")
        receiver_type = data.get("receiver_type", "PolaRX5")
        date = data.get("date")
        timeseries = data.get("timeseries", [])

        if not station_id or not timeseries:
            logger.warning(f"Skipping {json_path.name}: missing station_id or timeseries")
            return 0

        logger.info(f"Importing {json_path.name}: {station_id} {date} ({len(timeseries)} samples)")

        if dry_run:
            return len(timeseries)

        # Use batch write for efficiency
        written = writer.write_timeseries_batch(
            station_id=station_id,
            samples=timeseries,
            receiver_type=receiver_type,
            commit_interval=100
        )

        return written

    except Exception as e:
        logger.error(f"Error importing {json_path}: {e}")
        return 0


def import_all(
    data_path: Path,
    station: Optional[str] = None,
    dry_run: bool = False
) -> Dict[str, int]:
    """Import all JSON files to the database.

    Args:
        data_path: Base data path
        station: Optional station filter
        dry_run: If True, don't write to database

    Returns:
        Dictionary of station -> samples imported
    """
    json_files = find_json_files(data_path, station)

    if not json_files:
        logger.warning(f"No JSON files found in {data_path}")
        return {}

    logger.info(f"Found {len(json_files)} JSON files to import")

    results: Dict[str, int] = {}
    total_samples = 0

    with HealthDatabaseWriter() as writer:
        for json_path in json_files:
            samples = import_json_file(writer, json_path, dry_run)

            # Extract station from filename
            station_id = json_path.name.split("_")[0]
            results[station_id] = results.get(station_id, 0) + samples
            total_samples += samples

    logger.info(f"\nImport complete: {total_samples} total samples")
    for sid, count in sorted(results.items()):
        logger.info(f"  {sid}: {count} samples")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Import historical JSON health data to PostgreSQL"
    )
    parser.add_argument(
        "--station", "-s",
        help="Import only this station (e.g., ISFS)"
    )
    parser.add_argument(
        "--data-path", "-d",
        default="/home/bgo/tmp/gpsdata",
        help="Base data path (default: /home/bgo/tmp/gpsdata)"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be imported without writing to database"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    data_path = Path(args.data_path)
    if not data_path.exists():
        logger.error(f"Data path not found: {data_path}")
        sys.exit(1)

    if args.dry_run:
        logger.info("DRY RUN - no data will be written to database")

    results = import_all(data_path, args.station, args.dry_run)

    if not results:
        sys.exit(1)


if __name__ == "__main__":
    main()
