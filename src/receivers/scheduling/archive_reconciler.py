"""Archive reconciler: find SBF files missing their RINEX counterpart.

Scans archive directories for PolaRX5 stations and triggers SBF→RINEX
conversion for any raw files that lack a corresponding RINEX file.

Runs on the 'backfill' executor at a configurable interval (default every 6h).

When FormatResolver is available (archive_format table populated), uses
format templates for RINEX directory and file path construction. Falls back
to ArchiveFileChecker + filesystem glob when format data is unavailable.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("gps_scheduler.archive_reconciler")


def _get_format_resolver():
    """Try to create a FormatResolver. Returns None if unavailable."""
    try:
        from ..health.file_tracker import FormatResolver

        resolver = FormatResolver()
        if resolver.connect():
            # Check if format data is actually loaded
            formats = resolver.list_formats(file_category="rinex")
            if formats:
                return resolver
        resolver.close()
    except Exception:
        pass
    return None


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

    resolver = None
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
        resolver = _get_format_resolver()
        if resolver:
            logger.debug("Using FormatResolver for RINEX path construction")

        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=days_back - 1)

        for station_id in sorted(polarx5_stations):
            for session_type in session_types:
                missing, converted, errors = _reconcile_station_session(
                    station_id, session_type, start_date, end_date,
                    checker, resolver,
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
    finally:
        if resolver:
            resolver.close()


def _reconcile_station_session(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    checker: "ArchiveFileChecker",
    resolver: Optional["FormatResolver"] = None,
) -> Tuple[int, int, int]:
    """Check one station/session for SBF files missing RINEX.

    Args:
        station_id: Station identifier
        session_type: Session type
        start_date: Start of date range
        end_date: End of date range
        checker: ArchiveFileChecker instance
        resolver: Optional FormatResolver for format-aware path building

    Returns:
        Tuple of (missing_count, converted_count, error_count)
    """
    missing = 0
    converted = 0
    errors = 0

    # Resolve RINEX format for this session (if FormatResolver available)
    rinex_format = None
    if resolver:
        rinex_format = resolver.find_format(
            session_type=session_type,
            file_category="rinex",
            receiver_type="polarx5",
        )

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

                # Check for existing RINEX — try format-aware lookup first
                rinex_path = None
                if rinex_format and resolver:
                    rinex_path = _find_rinex_file_by_format(
                        station_id, file_dt, rinex_format, resolver, checker,
                    )

                # Fall back to filesystem glob
                if rinex_path is None:
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


def _find_rinex_file_by_format(
    station_id: str,
    dt: datetime,
    rinex_format: "ArchiveFormat",
    resolver: "FormatResolver",
    checker: "ArchiveFileChecker",
) -> Optional[Path]:
    """Check if a RINEX file exists using format template path construction.

    Uses FormatResolver to build the expected RINEX path from the archive_format
    template, then checks if that file exists on disk.

    Args:
        station_id: Station identifier
        dt: File datetime
        rinex_format: ArchiveFormat for the RINEX output
        resolver: FormatResolver instance
        checker: ArchiveFileChecker for base path fallback

    Returns:
        Path to existing RINEX file, or None
    """
    try:
        # Build expected RINEX path using format template
        base_path = checker.data_prepath or "/mnt/gpsdata"
        expected_path = resolver._build_path_from_format(
            rinex_format, station_id, dt, base_path
        )
        if expected_path and Path(expected_path).exists():
            return Path(expected_path)
    except Exception:
        pass
    return None


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
        # get_archive_directory() already returns path ending in /raw
        archive_path = Path(archive_dir)

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

        # Also check for Hatanaka compressed RINEX files (.d.Z, .d.gz, .YYd.Z)
        hatanaka_candidates = list(search_dir.glob(f"{stem[:4]}*d.Z"))
        hatanaka_candidates += list(search_dir.glob(f"{stem[:4]}*d.gz"))
        if hatanaka_candidates:
            return hatanaka_candidates[0]

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

        # Output to rinex/ sibling directory instead of raw/
        rinex_dir = sbf_path.parent.parent / "rinex"
        rinex_dir.mkdir(parents=True, exist_ok=True)

        converter = SBFConverter(station_id=station_id)
        result = converter.convert_file(sbf_path, output_dir=rinex_dir)

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
