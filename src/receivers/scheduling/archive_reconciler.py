"""Archive reconciler: find SBF files missing their RINEX counterpart.

Scans archive directories for PolaRX5 stations and triggers SBF→RINEX
conversion for any raw files that lack a corresponding RINEX file.

Runs on the 'backfill' executor at a configurable interval (default every 6h).
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("gps_scheduler.archive_reconciler")


def _run_archive_reconciler_job(
    session_types: List[str],
    days_back: int = 30,
) -> None:
    """APScheduler job: reconcile SBF archives with RINEX output.

    For each active PolaRX5 station, scans archive directories for SBF files
    that have no corresponding RINEX file and triggers conversion.

    Args:
        session_types: Session types to reconcile (e.g., ['15s_24hr', '1Hz_1hr'])
        days_back: Number of days to look back from yesterday
    """
    try:
        from ..cli.main import get_all_station_configs
        from ..health.file_tracker import ArchiveFileChecker
    except ImportError as e:
        logger.debug(f"Archive reconciler dependencies not available: {e}")
        return

    try:
        start_time = time.time()

        # Get active PolaRX5 stations
        all_stations = get_all_station_configs()
        polarx5_stations = [
            sid for sid, cfg in all_stations.items()
            if cfg.get('enabled', True)
            and cfg.get('receiver_type', '').lower() == 'polarx5'
            and cfg.get('station_status') not in ('discontinued', 'inactive')
            and cfg.get('health_check') != 'passive'
        ]

        if not polarx5_stations:
            logger.info("Archive reconciler: no active PolaRX5 stations")
            return

        logger.info(
            f"Archive reconciler: scanning {len(polarx5_stations)} stations, "
            f"{len(session_types)} sessions, {days_back} days back"
        )

        total_missing = 0
        total_converted = 0
        total_errors = 0

        checker = ArchiveFileChecker()
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=days_back - 1)

        for station_id in sorted(polarx5_stations):
            for session_type in session_types:
                missing, converted, errors = _reconcile_station_session(
                    station_id, session_type, start_date, end_date, checker
                )
                total_missing += missing
                total_converted += converted
                total_errors += errors

        duration = time.time() - start_time
        logger.info(
            f"Archive reconciler complete: "
            f"{total_missing} missing RINEX, {total_converted} converted, "
            f"{total_errors} errors ({duration:.1f}s)"
        )

    except Exception as e:
        logger.error(f"Archive reconciler failed: {type(e).__name__}: {e}")


def _reconcile_station_session(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    checker: "ArchiveFileChecker",
) -> tuple:
    """Check one station/session for SBF files missing RINEX.

    Args:
        station_id: Station identifier
        session_type: Session type
        start_date: Start of date range
        end_date: End of date range
        checker: ArchiveFileChecker instance

    Returns:
        Tuple of (missing_count, converted_count, error_count)
    """
    missing = 0
    converted = 0
    errors = 0

    try:
        current = start_date
        while current <= end_date:
            dt = datetime.combine(current, datetime.min.time()).replace(
                tzinfo=timezone.utc
            )

            if session_type == "15s_24hr":
                # Daily file: check one file per day
                hours = [0]
            else:
                # Hourly file: check 24 hours
                hours = list(range(24))

            for hour in hours:
                file_dt = dt.replace(hour=hour)
                sbf_path = _find_sbf_file(
                    station_id, session_type, file_dt, checker
                )
                if sbf_path is None:
                    continue

                rinex_path = _find_rinex_file(sbf_path)
                if rinex_path is not None:
                    continue

                # SBF exists but RINEX missing
                missing += 1
                success = _convert_sbf_to_rinex(station_id, sbf_path)
                if success:
                    converted += 1
                else:
                    errors += 1

            current += timedelta(days=1)

    except Exception as e:
        logger.warning(f"Reconciler error {station_id}/{session_type}: {e}")
        errors += 1

    if missing > 0:
        logger.info(
            f"Reconciler {station_id}/{session_type}: "
            f"{missing} missing, {converted} converted, {errors} errors"
        )

    return missing, converted, errors


def _find_sbf_file(
    station_id: str,
    session_type: str,
    dt: datetime,
    checker: "ArchiveFileChecker",
) -> Optional[Path]:
    """Find an SBF archive file for the given station/session/datetime.

    Looks in the archive directory for .sbf or .sbf.gz files.
    """
    try:
        archive_dir = checker.get_archive_directory(
            station_id, session_type,
            year=dt.year,
            month=dt.strftime("%b").lower(),
        )
        archive_path = Path(archive_dir) / "raw"

        if not archive_path.exists():
            return None

        # Build expected filename patterns
        doy = dt.strftime("%j")
        year2 = dt.strftime("%y")
        hour_letter = chr(ord('a') + dt.hour) if session_type != "15s_24hr" else "0"

        # Try common SBF naming patterns
        patterns = [
            f"{station_id}{dt.strftime('%Y%m%d')}{dt.hour:02d}*.sbf*",
            f"{station_id.lower()}{doy}{hour_letter}.{year2}_*",
        ]

        for pattern in patterns:
            matches = list(archive_path.glob(pattern))
            # Filter for SBF files specifically
            sbf_matches = [
                m for m in matches
                if m.name.endswith('.sbf') or m.name.endswith('.sbf.gz')
            ]
            if sbf_matches:
                return sbf_matches[0]

        return None

    except Exception:
        return None


def _find_rinex_file(sbf_path: Path) -> Optional[Path]:
    """Check if a RINEX file exists for the given SBF file.

    Looks in parent directory and sibling 'rinex' directory for
    corresponding observation files (.obs, .rnx, .crx, .gz variants).
    """
    stem = sbf_path.stem
    if stem.endswith('.sbf'):
        stem = stem[:-4]

    # Check in same directory and rinex subdirectory
    search_dirs = [sbf_path.parent]
    rinex_dir = sbf_path.parent.parent / "rinex"
    if rinex_dir.exists():
        search_dirs.append(rinex_dir)

    rinex_extensions = ['.obs', '.rnx', '.crx', '.obs.gz', '.rnx.gz', '.crx.gz']

    for search_dir in search_dirs:
        for ext in rinex_extensions:
            # Check various naming conventions
            candidates = list(search_dir.glob(f"{stem[:4]}*{ext}"))
            if candidates:
                return candidates[0]

    return None


def _convert_sbf_to_rinex(station_id: str, sbf_path: Path) -> bool:
    """Convert a single SBF file to RINEX.

    Args:
        station_id: Station identifier
        sbf_path: Path to SBF file

    Returns:
        True if conversion succeeded
    """
    try:
        from ..rinex.sbf_converter import SBFConverter

        converter = SBFConverter(station_id=station_id)
        result = converter.convert_file(sbf_path)

        if result.success:
            logger.debug(f"Converted {sbf_path.name} to RINEX")
            return True
        else:
            logger.warning(f"RINEX conversion failed for {sbf_path.name}: {result.message}")
            return False

    except ImportError:
        logger.debug("SBFConverter not available")
        return False
    except Exception as e:
        logger.warning(f"RINEX conversion error for {sbf_path.name}: {e}")
        return False
