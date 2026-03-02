"""Periodic file integrity checker for GPS archive.

Validates downloaded/archived files by:
1. Finding untracked files (on disk but not in file_tracking DB)
2. Size consistency check (flag files deviating from station/session median)
3. Remote receiver comparison for flagged files (FTP SIZE / HTTP Content-Length)

Runs on the 'backfill' executor at a configurable interval (default every 6h).
"""

import logging
import os
import statistics
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("receivers.scheduler.integrity")


def _run_integrity_check_job(
    session_types: List[str],
    days_back: int = 7,
    check_receiver: bool = True,
    size_tolerance_pct: float = 50.0,
    station_filter: Optional[List[str]] = None,
) -> None:
    """APScheduler job: periodic file integrity check.

    For each active station and session type, scans the last N days:
    1. Find untracked files (on disk, not in DB) and register them
    2. Size consistency check (flag outliers vs median)
    3. For flagged files only: compare against receiver (FTP SIZE)

    Args:
        session_types: Session types to check (e.g., ['15s_24hr', '1Hz_1hr'])
        days_back: Number of days to look back from yesterday
        check_receiver: Whether to do FTP SIZE comparison for flagged files
        size_tolerance_pct: Flag files deviating more than this % from median
        station_filter: If set, only check these station IDs (CLI use)
    """
    try:
        from ..cli.main import get_all_station_configs
        from ..health.file_tracker import ArchiveFileChecker, FileTracker
    except ImportError as e:
        logger.debug(f"Integrity checker dependencies not available: {e}")
        return

    start_time = time.time()
    logger.info(
        f"Starting integrity check: {days_back} days back, "
        f"sessions={session_types}, tolerance={size_tolerance_pct}%"
    )

    # Get active stations
    all_stations = get_all_station_configs()
    active_stations = {
        sid: cfg
        for sid, cfg in all_stations.items()
        if cfg.get("enabled", True)
        and cfg.get("station_status") not in ("discontinued", "inactive")
    }

    # Apply station filter if provided (CLI mode)
    if station_filter:
        active_stations = {
            sid: cfg for sid, cfg in active_stations.items()
            if sid in station_filter
        }

    if not active_stations:
        logger.warning("No active stations found for integrity check")
        return

    # Date range
    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days_back - 1)

    checker = ArchiveFileChecker()
    tracker = FileTracker()
    if not tracker.connect():
        logger.warning("Cannot connect to database for integrity check")
        return

    total_untracked = 0
    total_registered = 0
    total_suspect = 0
    total_checked = 0

    try:
        for session_type in session_types:
            for station_id in sorted(active_stations):
                try:
                    result = _check_station_session(
                        station_id,
                        session_type,
                        start_date,
                        end_date,
                        checker,
                        tracker,
                        active_stations[station_id],
                        check_receiver=check_receiver,
                        size_tolerance_pct=size_tolerance_pct,
                    )
                    total_untracked += result["untracked"]
                    total_registered += result["registered"]
                    total_suspect += result["suspect"]
                    total_checked += result["checked"]
                except Exception as e:
                    logger.debug(
                        f"Integrity check failed for {station_id}/{session_type}: {e}"
                    )
    finally:
        tracker.close()

    duration = time.time() - start_time
    logger.info(
        f"Integrity check complete in {duration:.1f}s: "
        f"{total_checked} files checked, {total_untracked} untracked found, "
        f"{total_registered} registered, {total_suspect} suspect"
    )


def _check_station_session(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    checker: "ArchiveFileChecker",
    tracker: "FileTracker",
    station_config: Dict[str, Any],
    check_receiver: bool = True,
    size_tolerance_pct: float = 50.0,
) -> Dict[str, int]:
    """Check integrity for a single station/session combination.

    Returns:
        Dict with counts: untracked, registered, suspect, checked
    """
    result = {"untracked": 0, "registered": 0, "suspect": 0, "checked": 0}

    receiver_type = station_config.get("receiver_type", "").lower() or None

    # Phase 1: Scan archive for untracked files
    untracked = _find_untracked_files(
        station_id, session_type, start_date, end_date, checker, tracker,
        receiver_type=receiver_type,
    )
    result["untracked"] = len(untracked)

    # Register untracked files that pass validation
    from ..utils.archive_validator import ArchiveValidator
    validator = ArchiveValidator()

    for file_path, file_date, file_hour in untracked:
        try:
            if validator.validate_archived_file(Path(file_path)):
                file_size = os.path.getsize(file_path)
                filename = os.path.basename(file_path)
                tracker.mark_file_archived(
                    station_id, session_type, file_date, file_hour,
                    filename, file_size,
                )
                tracker.mark_integrity_checked(
                    station_id, session_type, file_date, file_hour,
                )
                result["registered"] += 1
                logger.debug(f"Registered untracked file: {file_path}")
            else:
                file_size = os.path.getsize(file_path)
                filename = os.path.basename(file_path)
                tracker.mark_file_suspect(
                    station_id, session_type, file_date, file_hour,
                    filename, file_size,
                    reason="untracked file failed archive validation",
                )
                result["suspect"] += 1
                logger.warning(f"Suspect untracked file (validation failed): {file_path}")
        except Exception as e:
            logger.debug(f"Error processing untracked file {file_path}: {e}")

    # Phase 2: Size consistency check for tracked files
    flagged = _size_consistency_check(
        station_id, session_type, start_date, end_date, tracker,
        tolerance_pct=size_tolerance_pct,
    )
    result["checked"] = flagged["total_checked"]

    if flagged["outliers"] and check_receiver:
        # Phase 3: Remote receiver comparison for outliers only
        for file_date, file_hour, file_size, median_size in flagged["outliers"]:
            try:
                remote_size = _get_remote_file_size(
                    station_id, session_type, file_date, file_hour, station_config,
                )
                if remote_size is None:
                    # File no longer on receiver — mark suspect but don't delete
                    tracker.mark_file_suspect(
                        station_id, session_type, file_date, file_hour,
                        reason=f"size outlier ({file_size} vs median {median_size}), "
                        f"no longer on receiver",
                    )
                    result["suspect"] += 1
                elif abs(remote_size - file_size) < 100:
                    # Sizes match (within rounding) — file is OK
                    tracker.mark_integrity_checked(
                        station_id, session_type, file_date, file_hour,
                    )
                else:
                    # Sizes differ — mark suspect
                    tracker.mark_file_suspect(
                        station_id, session_type, file_date, file_hour,
                        reason=f"size mismatch: local={file_size}, "
                        f"remote={remote_size}, median={median_size}",
                    )
                    result["suspect"] += 1
            except Exception as e:
                logger.debug(
                    f"Remote check failed for {station_id}/{session_type}/"
                    f"{file_date}: {e}"
                )
    elif not flagged["outliers"]:
        # No outliers — mark all checked files as integrity verified
        _mark_all_integrity_checked(
            station_id, session_type, start_date, end_date, tracker,
        )

    return result


def _find_untracked_files(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    checker: "ArchiveFileChecker",
    tracker: "FileTracker",
    receiver_type: Optional[str] = None,
) -> List[Tuple[str, date, Optional[int]]]:
    """Find files on disk that have no file_tracking entry.

    Returns:
        List of (file_path, file_date, file_hour) tuples
    """
    import glob as glob_mod

    untracked = []

    # Generate all dates and check archive directories
    unique_months: Set[Tuple[int, str]] = set()
    current = start_date
    while current <= end_date:
        dt = datetime.combine(current, datetime.min.time())
        unique_months.add((current.year, dt.strftime("%b").lower()))
        current += timedelta(days=1)

    for year, month in unique_months:
        archive_dir = checker.get_archive_directory(
            station_id, session_type, year=year, month=month,
        )
        if not os.path.isdir(archive_dir):
            continue

        pattern = os.path.join(archive_dir, f"{station_id}*")
        for filepath in glob_mod.glob(pattern):
            if not os.path.isfile(filepath):
                continue

            fsize = os.path.getsize(filepath)
            if fsize < 50:  # MIN_ARCHIVE_FILE_SIZE
                continue

            filename = os.path.basename(filepath)

            # Parse date from filename
            from ..utils.download_tracker import parse_date_from_filename

            parsed = parse_date_from_filename(filename, station_id)
            if parsed is None:
                continue

            file_date, file_hour = parsed

            # Check date range
            if file_date < start_date or file_date > end_date:
                continue

            # Check if already tracked
            if _is_file_tracked(tracker, station_id, session_type, file_date, file_hour):
                continue

            untracked.append((filepath, file_date, file_hour))

    return untracked


def _is_file_tracked(
    tracker: "FileTracker",
    station_id: str,
    session_type: str,
    file_date: date,
    file_hour: Optional[int],
) -> bool:
    """Check if a file has a file_tracking entry (any status)."""
    if not tracker._conn:
        return False

    try:
        with tracker._conn.cursor() as cur:
            if file_hour is None:
                cur.execute(
                    """SELECT 1 FROM file_tracking
                    WHERE sid = %s AND session_type = %s
                    AND file_date = %s AND file_hour IS NULL""",
                    (station_id, session_type, file_date),
                )
            else:
                cur.execute(
                    """SELECT 1 FROM file_tracking
                    WHERE sid = %s AND session_type = %s
                    AND file_date = %s AND file_hour = %s""",
                    (station_id, session_type, file_date, file_hour),
                )
            return cur.fetchone() is not None
    except Exception:
        return False


def _size_consistency_check(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    tracker: "FileTracker",
    tolerance_pct: float = 50.0,
) -> Dict[str, Any]:
    """Check file sizes against median for the station/session.

    Returns:
        Dict with:
        - total_checked: number of files checked
        - outliers: list of (file_date, file_hour, file_size, median_size) tuples
    """
    result: Dict[str, Any] = {"total_checked": 0, "outliers": []}

    if not tracker._conn:
        return result

    try:
        with tracker._conn.cursor() as cur:
            # Get all file sizes for this station/session in the date range
            cur.execute(
                """SELECT file_date, file_hour, file_size
                FROM file_tracking
                WHERE sid = %s AND session_type = %s
                AND file_date >= %s AND file_date <= %s
                AND status IN ('downloaded', 'archived')
                AND file_size IS NOT NULL AND file_size > 0
                ORDER BY file_date, file_hour""",
                (station_id, session_type, start_date, end_date),
            )
            rows = cur.fetchall()

        if not rows:
            return result

        result["total_checked"] = len(rows)

        # Compute median file size
        sizes = [row[2] for row in rows]
        if len(sizes) < 3:
            # Too few files to compute meaningful median
            return result

        median_size = statistics.median(sizes)
        if median_size == 0:
            return result

        # Find outliers
        threshold = tolerance_pct / 100.0
        for file_date, file_hour, file_size in rows:
            deviation = abs(file_size - median_size) / median_size
            if deviation > threshold:
                result["outliers"].append(
                    (file_date, file_hour, file_size, int(median_size))
                )

        if result["outliers"]:
            logger.info(
                f"{station_id}/{session_type}: {len(result['outliers'])} size outliers "
                f"(median={int(median_size):,}, tolerance={tolerance_pct}%)"
            )

    except Exception as e:
        logger.debug(f"Size consistency check failed for {station_id}/{session_type}: {e}")

    return result


def _get_remote_file_size(
    station_id: str,
    session_type: str,
    file_date: date,
    file_hour: Optional[int],
    station_config: Dict[str, Any],
) -> Optional[int]:
    """Get file size from receiver via FTP SIZE or HTTP Content-Length.

    Cheap operation — no data transfer, just a SIZE check.

    Returns:
        Remote file size in bytes, or None if file not found/unavailable
    """
    receiver_type = station_config.get("receiver_type", "").lower()

    if receiver_type in ("polarx5", ""):
        return _get_remote_size_ftp(station_id, session_type, file_date, file_hour, station_config)
    elif receiver_type in ("netr9", "netr5", "netrs"):
        return _get_remote_size_http(station_id, session_type, file_date, file_hour, station_config)
    elif receiver_type == "g10":
        return _get_remote_size_ftp(station_id, session_type, file_date, file_hour, station_config)
    else:
        logger.debug(f"Unknown receiver type for remote size check: {receiver_type}")
        return None


def _get_remote_size_ftp(
    station_id: str,
    session_type: str,
    file_date: date,
    file_hour: Optional[int],
    station_config: Dict[str, Any],
) -> Optional[int]:
    """Get file size via FTP SIZE command."""
    from ftplib import FTP

    try:
        ip = station_config.get("router", {}).get("ip")
        if not ip:
            return None

        receiver_config = station_config.get("receiver", {})
        ftp_port = int(receiver_config.get("ftpport", 2160))

        ftp = None
        ftp = FTP()
        ftp.connect(ip, ftp_port, timeout=15)
        ftp.login()

        # Build remote path based on receiver type
        receiver_type = station_config.get("receiver_type", "").lower()
        if receiver_type == "g10":
            # Leica G10 uses SD Card path
            doy = (file_date - date(file_date.year, 1, 1)).days + 1
            session_letter = "a"  # daily
            if file_hour is not None and file_hour > 0:
                session_letter = chr(ord("a") + file_hour)
            remote_file = f"/SD Card/Data/{session_type}/{station_id}{doy:03d}{session_letter}.m00.zip"
        else:
            # PolaRX5 uses GPS week directories
            try:
                import gtimes.timefunc as gt

                dt = datetime.combine(file_date, datetime.min.time())
                if file_hour is not None:
                    dt = dt.replace(hour=file_hour)
                gps_week = gt.date2gpsWeek(dt)[0]

                session_map = {
                    "15s_24hr": ("a", "15s_24hr"),
                    "1Hz_1hr": ("b", "1Hz_1hr"),
                    "status_1hr": ("c", "status_1hr"),
                }
                letter, session_path = session_map.get(session_type, ("a", session_type))

                if "24hr" in session_type:
                    file_name = f"{station_id}{dt.strftime('%Y%m%d')}0000{letter}.sbf.gz"
                else:
                    file_name = f"{station_id}{dt.strftime('%Y%m%d%H')}00{letter}.sbf.gz"

                remote_file = f"/DSK1/SSN/{session_path}/{gps_week:05d}/{file_name}"
            except ImportError:
                logger.debug("gtimes not available for remote path construction")
                ftp.quit()
                return None

        try:
            size = ftp.size(remote_file)
            ftp.quit()
            return size
        except Exception:
            ftp.quit()
            return None

    except Exception as e:
        # Close FTP socket if connect() succeeded but a later call raised
        if ftp is not None:
            try:
                ftp.close()
            except Exception:
                pass
        logger.debug(f"FTP SIZE check failed for {station_id}: {e}")
        return None


def _get_remote_size_http(
    station_id: str,
    session_type: str,
    file_date: date,
    file_hour: Optional[int],
    station_config: Dict[str, Any],
) -> Optional[int]:
    """Get file size via HTTP HEAD / Content-Length."""
    try:
        import requests

        ip = station_config.get("router", {}).get("ip")
        if not ip:
            return None

        receiver_config = station_config.get("receiver", {})
        http_port = int(receiver_config.get("httpport", 80))

        # Build remote path for Trimble
        if file_hour is not None:
            date_str = f"{file_date.strftime('%Y%m%d')}{file_hour:02d}00"
        else:
            date_str = f"{file_date.strftime('%Y%m%d')}0000"

        receiver_type = station_config.get("receiver_type", "").lower()
        if receiver_type == "netrs":
            ext = ".T00"
        else:
            ext = ".T02"

        filename = f"{station_id}{date_str}a{ext}"
        month_dir = file_date.strftime("%Y%m")
        remote_path = f"/Internal/{month_dir}/{session_type}/{filename}"
        url = f"http://{ip}:{http_port}/download{remote_path}"

        response = requests.head(url, timeout=15)
        if response.status_code == 200:
            content_length = response.headers.get("content-length")
            if content_length:
                return int(content_length)

        return None

    except Exception as e:
        logger.debug(f"HTTP HEAD check failed for {station_id}: {e}")
        return None


def _mark_all_integrity_checked(
    station_id: str,
    session_type: str,
    start_date: date,
    end_date: date,
    tracker: "FileTracker",
) -> None:
    """Mark all tracked files in range as integrity checked (batch update)."""
    if not tracker._conn:
        return

    try:
        with tracker._conn.cursor() as cur:
            cur.execute(
                """UPDATE file_tracking
                SET integrity_checked_at = NOW(), updated_at = NOW()
                WHERE sid = %s AND session_type = %s
                AND file_date >= %s AND file_date <= %s
                AND status IN ('downloaded', 'archived')
                AND integrity_checked_at IS NULL""",
                (station_id, session_type, start_date, end_date),
            )
            updated = cur.rowcount
        tracker._conn.commit()
        if updated > 0:
            logger.debug(
                f"Marked {updated} files as integrity checked: "
                f"{station_id}/{session_type}"
            )
    except Exception as e:
        logger.debug(f"Batch integrity mark failed: {e}")
        if tracker._conn:
            tracker._conn.rollback()
