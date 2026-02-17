"""Archive reconciler: find raw files missing their RINEX counterpart.

Scans archive directories for all receiver types with RINEX converters
and triggers raw→RINEX conversion for any raw files that lack a
corresponding RINEX file.

Runs on the 'backfill' executor at a configurable interval (default every 6h).

When FormatResolver is available (archive_format table populated), uses
format templates for RINEX directory and file path construction. Falls back
to ArchiveFileChecker + filesystem glob when format data is unavailable.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("receivers.scheduler.reconciler")


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
    """APScheduler job: reconcile raw archives with RINEX output.

    For each active station with a RINEX converter, scans archive directories
    for raw files that have no corresponding RINEX file and triggers conversion.

    Args:
        session_types: Session types to reconcile (e.g., ['15s_24hr', '1Hz_1hr'])
        days_back: Number of days to look back from yesterday
    """
    try:
        from ..cli.main import get_all_station_configs
        from ..config.receiver_registry import has_rinex_converter
        from ..health.file_tracker import ArchiveFileChecker
    except ImportError as e:
        logger.debug(f"Archive reconciler dependencies not available: {e}")
        return

    resolver = None
    try:
        start_time = time.time()

        # Get active stations with RINEX converters (all receiver types)
        all_stations = get_all_station_configs()
        convertible_stations: Dict[str, str] = {
            sid: cfg.get('receiver_type', '').lower()
            for sid, cfg in all_stations.items()
            if cfg.get('enabled', True)
            and has_rinex_converter(cfg.get('receiver_type', ''))
            and cfg.get('station_status') not in ('discontinued', 'inactive')
            and cfg.get('health_check') != 'passive'
        }

        if not convertible_stations:
            logger.info("Archive reconciler: no active stations with RINEX converters")
            return

        logger.info(
            f"Archive reconciler: scanning {len(convertible_stations)} stations, "
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

        for station_id in sorted(convertible_stations):
            receiver_type = convertible_stations[station_id]
            for session_type in session_types:
                missing, converted, errors = _reconcile_station_session(
                    station_id, session_type, start_date, end_date,
                    checker, resolver, receiver_type=receiver_type,
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
    receiver_type: str = "polarx5",
) -> Tuple[int, int, int]:
    """Check one station/session for raw files missing RINEX.

    Args:
        station_id: Station identifier
        session_type: Session type
        start_date: Start of date range
        end_date: End of date range
        checker: ArchiveFileChecker instance
        resolver: Optional FormatResolver for format-aware path building
        receiver_type: Receiver type key (e.g., 'polarx5', 'netr9')

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
            receiver_type=receiver_type,
        )

    try:
        # Iterate newest-first so recent files (1-3 days old) get converted first
        current = end_date
        while current >= start_date:
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
                raw_path = _find_raw_file(
                    station_id, session_type, file_dt, checker,
                    receiver_type=receiver_type,
                )
                if raw_path is None:
                    continue

                # Check for existing RINEX — format-aware or glob fallback
                rinex_path = None
                if rinex_format and resolver:
                    # FormatResolver available: use template-based path check
                    rinex_path = _find_rinex_file_by_format(
                        station_id, file_dt, rinex_format, resolver, checker,
                    )
                else:
                    # No FormatResolver: fall back to filesystem glob
                    rinex_path = _find_rinex_file(raw_path)

                if rinex_path is not None:
                    continue

                # Raw file exists but RINEX missing
                missing += 1
                success = _convert_raw_to_rinex(
                    station_id, raw_path, receiver_type=receiver_type,
                )
                if success:
                    converted += 1
                else:
                    errors += 1

            current -= timedelta(days=1)

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


def _find_raw_file(
    station_id: str,
    session_type: str,
    dt: datetime,
    checker: "ArchiveFileChecker",
    receiver_type: str = "polarx5",
) -> Optional[Path]:
    """Find a raw archive file for the given station/session/datetime.

    Works for all receiver types by using the registry to determine
    valid file extensions.
    """
    from ..config.receiver_registry import get_capability

    cap = get_capability(receiver_type)
    if cap is None:
        return None

    try:
        archive_dir = checker.get_archive_directory(
            station_id, session_type,
            year=dt.year,
            month=dt.strftime("%b").lower(),
        )
        archive_path = Path(archive_dir)

        if not archive_path.exists():
            return None

        # Primary pattern: SSSSYYYYMMDDHHMMX.ext (used by all receiver types)
        pattern = f"{station_id}{dt.strftime('%Y%m%d')}{dt.hour:02d}*"
        matches = list(archive_path.glob(pattern))

        # Filter matches against known extensions for this receiver type
        for match in matches:
            name = match.name
            if any(name.endswith(ext) for ext in cap.raw_extensions):
                return match

        # Fallback: DOY-based naming pattern (some older archives)
        doy = dt.strftime("%j")
        hour_letter = chr(ord('a') + dt.hour) if session_type != "15s_24hr" else "0"
        year2 = dt.strftime("%y")
        fallback_pattern = f"{station_id.lower()}{doy}{hour_letter}.{year2}_*"
        fallback_matches = list(archive_path.glob(fallback_pattern))
        for match in fallback_matches:
            name = match.name
            if any(name.endswith(ext) for ext in cap.raw_extensions):
                return match

        return None

    except Exception:
        return None


def _find_rinex_file(raw_path: Path) -> Optional[Path]:
    """Check if a RINEX file exists for the given raw file.

    Looks in parent directory and sibling 'rinex' directory for
    corresponding observation files (.obs, .rnx, .crx, .gz variants).

    Uses DOY-based pattern matching to avoid false positives where a RINEX
    file for a different date (same station) is incorrectly matched.

    Raw filename formats:
      SBF:     SSSSYYYYMMDDHHMMX.sbf.gz  (X = session letter)
      Trimble: SSSSYYYYMMDDHHMMX.T02     (same pattern)
      Leica:   SSSSYYYYMMDDHHMMX.m00.gz  (same pattern)

    RINEX 2 short name: SSSSdddS.YYd.Z (ddd = DOY, S = RINEX session char)

    Note: Raw session letter (a/b/...) does NOT correspond to RINEX session
    character. RINEX uses '0' for daily and 'a'-'x' for hourly (hour-mapped).
    Current converters produce '0' for both daily and hourly sessions.
    """
    # Strip all known raw extensions to get the stem
    stem = raw_path.name
    for ext in ('.sbf.gz', '.sbf', '.T02.gz', '.T02', '.t02',
                '.T00.gz', '.T00', '.t00', '.m00.gz', '.m00', '.M00'):
        if stem.endswith(ext):
            stem = stem[:-len(ext)]
            break

    station = stem[:4]

    # Extract date + hour from filename (SSSSYYYYMMDDHHMMX)
    doy_patterns: List[str] = []
    try:
        date_str = stem[4:12]  # YYYYMMDD
        hour_str = stem[12:14]  # HH
        dt = datetime.strptime(date_str, "%Y%m%d")
        doy = dt.strftime("%j")  # e.g., "042"
        hour = int(hour_str)

        # Try '0' first — current converter convention for both daily and hourly
        doy_patterns.append(f"{station}{doy}0")
        # Also try hour-mapped letter (a-x) for standard hourly RINEX naming
        hour_letter = chr(ord("a") + hour)
        if hour_letter != "0":
            doy_patterns.append(f"{station}{doy}{hour_letter}")
    except (ValueError, IndexError):
        pass

    # Check in same directory and rinex subdirectory
    search_dirs = [raw_path.parent]
    rinex_dir = raw_path.parent.parent / "rinex"
    if rinex_dir.exists():
        search_dirs.append(rinex_dir)

    rinex_extensions = ['.obs', '.rnx', '.crx', '.obs.gz', '.rnx.gz', '.crx.gz']

    for search_dir in search_dirs:
        if doy_patterns:
            # Date-specific search: try each RINEX session pattern
            for doy_pattern in doy_patterns:
                for ext in rinex_extensions:
                    candidates = list(search_dir.glob(f"{doy_pattern}*{ext}"))
                    if candidates:
                        return candidates[0]

                # Hatanaka compressed RINEX (.d.Z, .d.gz)
                hatanaka = list(search_dir.glob(f"{doy_pattern}*d.Z"))
                hatanaka += list(search_dir.glob(f"{doy_pattern}*d.gz"))
                if hatanaka:
                    return hatanaka[0]
        else:
            # Fallback: no date extracted, use station-only (legacy behavior)
            for ext in rinex_extensions:
                candidates = list(search_dir.glob(f"{station}*{ext}"))
                if candidates:
                    return candidates[0]

            hatanaka = list(search_dir.glob(f"{station}*d.Z"))
            hatanaka += list(search_dir.glob(f"{station}*d.gz"))
            if hatanaka:
                return hatanaka[0]

    return None


def _convert_raw_to_rinex(
    station_id: str,
    raw_path: Path,
    receiver_type: str = "polarx5",
) -> bool:
    """Convert a single raw file to RINEX using the appropriate converter.

    Dynamically loads the converter class from the receiver registry.

    Args:
        station_id: Station identifier
        raw_path: Path to raw file
        receiver_type: Receiver type key

    Returns:
        True if conversion succeeded
    """
    try:
        from ..config.receiver_registry import get_converter_class

        converter_class = get_converter_class(receiver_type)
        if converter_class is None:
            logger.debug(f"No converter available for {receiver_type}")
            return False

        # Output to rinex/ sibling directory instead of raw/
        rinex_dir = raw_path.parent.parent / "rinex"
        rinex_dir.mkdir(parents=True, exist_ok=True)

        converter = converter_class(station_id=station_id)
        result = converter.convert_file(raw_path, output_dir=rinex_dir)

        if result.success:
            logger.debug(f"Converted {raw_path.name} to RINEX ({receiver_type})")
            return True
        else:
            logger.warning(
                f"RINEX conversion failed for {raw_path.name}: {result.message}"
            )
            return False

    except ImportError:
        logger.debug(f"Converter not available for {receiver_type}")
        return False
    except Exception as e:
        logger.warning(f"RINEX conversion error for {raw_path.name}: {e}")
        return False


# Backward-compatible aliases for imports in cli/scheduler.py
_find_sbf_file = _find_raw_file
_convert_sbf_to_rinex = _convert_raw_to_rinex
