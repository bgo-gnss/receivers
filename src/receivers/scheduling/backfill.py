"""Backfill status_1hr health data from PolaRX5 receivers.

Downloads historical status_1hr SBF files, extracts health metrics via RxTools,
and writes them to the PostgreSQL database. Progress is tracked in the
backfill_progress table so the process can resume after restarts.

Designed to run as a low-priority scheduled job alongside live downloads,
processing one day per station per invocation to limit FTP session duration.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("gps_scheduler.backfill")


def _backfill_next_station() -> None:
    """Pick the next station needing backfill and process one day.

    This is the APScheduler job entry point. It:
    1. Queries backfill_progress for the least-recently-processed station
    2. Calls _backfill_station_day() to process one day
    3. Updates progress in the database

    Module-level function for APScheduler serialization.
    """
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                # Pick station processed least recently (fair round-robin)
                cur.execute("""
                    SELECT sid, next_date, backfill_start, backfill_end
                    FROM backfill_progress
                    WHERE status IN ('pending', 'in_progress')
                    ORDER BY last_run ASC NULLS FIRST, sid
                    LIMIT 1
                """)
                row = cur.fetchone()

            if not row:
                logger.debug("No stations pending backfill")
                return

            station_id = row[0]
            current_dt = row[1]
            # row[2] is backfill_start (not needed here)
            backfill_end = row[3]

        logger.info(f"Backfill: {station_id} processing {current_dt}")
        has_more = _backfill_station_day(station_id, current_dt, backfill_end)

        if not has_more:
            logger.info(f"Backfill complete for {station_id}")

    except ImportError:
        logger.debug("psycopg2 not available - backfill disabled")
    except Exception as e:
        logger.error(f"Backfill job error: {type(e).__name__}: {e}")


def _backfill_station_day(
    station_id: str, process_date: date, backfill_end: date
) -> bool:
    """Process one day of backfill for a station.

    Downloads that day's status_1hr files, extracts health data, writes to DB,
    and advances the backfill_progress cursor.

    Args:
        station_id: Station identifier (e.g., 'ELDC')
        process_date: The date to process
        backfill_end: End date of backfill range

    Returns:
        True if there's more work to do, False if backfill is complete
    """
    from ..health.database_factory import DatabaseConnectionFactory

    start_time = time.time()
    files_found = 0
    files_imported = 0
    files_missing = 0
    files_error = 0

    try:
        # Mark as in_progress
        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE backfill_progress
                    SET status = 'in_progress', updated_at = NOW()
                    WHERE sid = %s AND session_type = 'status_1hr'
                """, (station_id,))

        # Download status_1hr files for this single day
        result = _download_day(station_id, process_date)

        if result is None:
            # Could not connect or create receiver - mark error and move on
            files_error = 1
        else:
            status = result.get("status", "failed")
            downloaded_files = result.get("downloaded_files", [])
            files_downloaded = result.get("files_downloaded", 0)

            if status == "failed" or status == "unreachable":
                files_error = 1
            elif files_downloaded == 0 and status in ("up_to_date", "completed"):
                # No files found for this day - that's expected for historical data
                files_missing = 1
            else:
                files_found = files_downloaded

                # Extract health data from downloaded SBF files and write to DB
                if downloaded_files:
                    imported = _extract_and_store_health(
                        station_id, downloaded_files, logger
                    )
                    files_imported = imported

        # Advance cursor and update progress
        next_date = process_date + timedelta(days=1)
        is_complete = next_date > backfill_end
        new_status = "completed" if is_complete else "in_progress"
        duration = time.time() - start_time

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE backfill_progress
                    SET next_date = %s,
                        status = %s,
                        files_found = files_found + %s,
                        files_imported = files_imported + %s,
                        files_missing = files_missing + %s,
                        files_error = files_error + %s,
                        last_run = NOW(),
                        last_duration_seconds = %s,
                        updated_at = NOW()
                    WHERE sid = %s AND session_type = 'status_1hr'
                """, (
                    next_date, new_status,
                    files_found, files_imported, files_missing, files_error,
                    duration,
                    station_id,
                ))

        logger.info(
            f"Backfill {station_id}/{process_date}: "
            f"found={files_found} imported={files_imported} "
            f"missing={files_missing} errors={files_error} "
            f"({duration:.1f}s)"
        )

        return not is_complete

    except Exception as e:
        logger.error(f"Backfill error {station_id}/{process_date}: {e}")
        # Don't advance cursor on error - will retry this date next time
        try:
            with DatabaseConnectionFactory.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE backfill_progress
                        SET files_error = files_error + 1,
                            last_run = NOW(),
                            last_duration_seconds = %s,
                            updated_at = NOW()
                        WHERE sid = %s AND session_type = 'status_1hr'
                    """, (time.time() - start_time, station_id))
        except Exception:
            pass
        return True  # More work to do (retry this date)


def _download_day(
    station_id: str, process_date: date
) -> Optional[Dict[str, Any]]:
    """Download status_1hr files for a single day.

    Args:
        station_id: Station identifier
        process_date: Date to download

    Returns:
        Download result dictionary, or None on setup error
    """
    try:
        from ..cli.main import get_station_config, create_receiver

        station_config = get_station_config(station_id)
        if not station_config:
            logger.warning(f"No config for {station_id}")
            return None

        receiver = create_receiver(station_id, station_config)

        # Build time range for the single day
        start_time = datetime.combine(process_date, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        end_time = start_time + timedelta(days=1)

        result = receiver.download_data(
            start=start_time,
            end=end_time,
            session="status_1hr",
            ffrequency="1H",
            sync=True,
            archive=True,
            immediate_archive=True,
            clean_tmp=True,
            compression=".gz",
            reverse_chronological=False,  # Oldest first for backfill
            loglevel=logging.INFO,
        )

        return result

    except Exception as e:
        logger.error(f"Download error {station_id}/{process_date}: {e}")
        return None


def _extract_and_store_health(
    station_id: str,
    file_paths: List[str],
    log: logging.Logger,
) -> int:
    """Extract health data from SBF files and write to database.

    Processes each file through RxToolsExtractor, writes metrics to block tables
    via HealthDatabaseWriter, and updates file_tracking.

    Args:
        station_id: Station identifier
        file_paths: List of archive file paths (may be .sbf, .sbf.gz)
        log: Logger instance

    Returns:
        Number of files successfully imported
    """
    imported_count = 0

    try:
        from ..health.rxtools_extractor import RxToolsExtractor
        from ..health.db_writer import HealthDatabaseWriter
        from ..health.file_tracker import FileTracker

        extractor = RxToolsExtractor(station_id)
        if not extractor.check_rxtools_available():
            log.warning(f"RxTools not available - cannot extract health from SBF files")
            return 0

        writer = HealthDatabaseWriter()
        tracker = FileTracker()

        for file_path_str in file_paths:
            file_path = Path(file_path_str)

            try:
                # Check if already imported
                file_date = _extract_date_from_path(file_path)
                file_hour = _extract_hour_from_path(file_path)

                if file_date and tracker.is_file_missing(
                    station_id, "status_1hr", file_date, file_hour
                ):
                    continue

                # Extract health data from SBF
                health_data = extractor.extract_health_from_sbf(file_path)
                if not health_data or not health_data.get("metrics"):
                    log.debug(f"No health metrics in {file_path.name}")
                    # Don't mark as 'missing' — the file was downloaded successfully,
                    # it just didn't contain extractable health metrics. Marking it
                    # 'missing' would overwrite the 'downloaded' status in file_tracking.
                    continue

                # Add station metadata for db_writer
                health_data["station_id"] = station_id
                health_data["receiver_type"] = "PolaRX5"

                # Use file timestamp if no timestamp in data
                if "timestamp" not in health_data:
                    health_data["timestamp"] = datetime.now(timezone.utc).isoformat()

                # Write to database
                success = writer.write_health_data(health_data)

                if success:
                    imported_count += 1
                    # Update file_tracking
                    if file_date:
                        samples = _count_samples(health_data)
                        tracker.mark_health_imported(
                            station_id, file_date, samples
                        )
                    log.debug(f"Imported health data from {file_path.name}")
                else:
                    log.warning(f"Failed to write health data from {file_path.name}")

            except Exception as e:
                log.warning(f"Error processing {file_path.name}: {e}")

    except ImportError as e:
        log.warning(f"Health extraction dependencies not available: {e}")
    except Exception as e:
        log.error(f"Health extraction error for {station_id}: {e}")

    return imported_count


def _extract_date_from_path(file_path: Path) -> Optional[date]:
    """Extract date from archive file path.

    Archive paths contain date info in the filename, e.g.:
    ELDC20260115_status.sbf.gz or similar patterns with YYYYMMDD.

    Args:
        file_path: Path to archived file

    Returns:
        Extracted date or None
    """
    import re

    name = file_path.name
    # Try YYYYMMDD pattern in filename (common in SBF archives)
    match = re.search(r"(\d{4})(\d{2})(\d{2})", name)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass

    # Try YYDOY pattern (2-digit year + day of year)
    match = re.search(r"(\d{2})(\d{3})", name)
    if match:
        try:
            year = 2000 + int(match.group(1))
            doy = int(match.group(2))
            if 1 <= doy <= 366:
                return (datetime(year, 1, 1) + timedelta(days=doy - 1)).date()
        except ValueError:
            pass

    return None


def _extract_hour_from_path(file_path: Path) -> Optional[int]:
    """Extract hour from archive file path.

    For hourly files, the hour may be encoded as a letter (a=0, b=1, ..., x=23)
    or as HH in the filename.

    Args:
        file_path: Path to archived file

    Returns:
        Hour (0-23) or None
    """
    import re

    name = file_path.name

    # Try HH pattern after date digits (e.g., ELDC2026011512)
    match = re.search(r"\d{8}(\d{2})", name)
    if match:
        hour = int(match.group(1))
        if 0 <= hour <= 23:
            return hour

    # Try IGS letter convention (a=0, b=1, ..., x=23)
    # Pattern: 4-char station + DOY + letter
    match = re.search(r"[A-Z]{4}\d{3,}([a-x])", name, re.IGNORECASE)
    if match:
        letter = match.group(1).lower()
        return ord(letter) - ord("a")

    return None


def _count_samples(health_data: Dict[str, Any]) -> int:
    """Count number of data samples in health data.

    Args:
        health_data: Extracted health data dictionary

    Returns:
        Approximate sample count
    """
    count = 0
    metrics = health_data.get("metrics", {})
    if "power" in metrics:
        count += 1
    if "cpu_load" in metrics:
        count += 1
    if "temperature" in metrics:
        count += 1
    if "position" in metrics:
        count += 1
    if "satellites" in metrics:
        count += 1
    data_quality = health_data.get("data_quality", {})
    if data_quality:
        count += 1
    network = health_data.get("network", {})
    if network:
        count += 1
    return max(count, 1)  # At least 1 if we got any data
