"""File tracking for downloads and health imports.

Tracks file availability and import status to:
- Skip files known to be missing on receivers
- Avoid reimporting data already in the database
- Provide data availability statistics
"""

import hashlib
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class FileTracker:
    """Track file availability and import status.

    Uses the file_tracking table in the gps_health database.
    """

    def __init__(self, connection_string: Optional[str] = None):
        """Initialize file tracker.

        Args:
            connection_string: PostgreSQL connection string.
                             If None, uses environment variables.
        """
        self.connection_string = connection_string
        self._conn = None

    def connect(self, database: str = "gps_health") -> bool:
        """Connect to PostgreSQL database.

        Args:
            database: Database name

        Returns:
            True if connection successful
        """
        try:
            import psycopg2

            if self.connection_string:
                self._conn = psycopg2.connect(self.connection_string)
            else:
                db_host = os.getenv("POSTGRES_HOST", "localhost")
                db_port = os.getenv("POSTGRES_PORT", "5432")
                db_name = os.getenv("POSTGRES_DB", database)
                db_user = os.getenv("POSTGRES_USER", os.getenv("USER", "bgo"))
                db_pass = os.getenv("POSTGRES_PASSWORD", "")

                self._conn = psycopg2.connect(
                    host=db_host,
                    port=db_port,
                    database=db_name,
                    user=db_user,
                    password=db_pass,
                )

            logger.debug(f"Connected to PostgreSQL database: {db_name}")
            return True

        except ImportError:
            logger.warning("psycopg2 not installed - file tracking disabled")
            return False
        except Exception as e:
            logger.warning(f"Database connection failed: {e} - file tracking disabled")
            return False

    def is_file_missing(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int] = None,
    ) -> bool:
        """Check if a file is known to be missing.

        Args:
            station_id: Station identifier
            session_type: Session type ('15s_24hr', '1Hz_1hr', 'status_1hr')
            file_date: Date of the file
            file_hour: Hour for hourly files (0-23), None for daily

        Returns:
            True if file is known to be missing (skip download)
        """
        if not self._conn:
            if not self.connect():
                return False  # Can't check, don't skip

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT is_file_missing(%s, %s, %s, %s)",
                    (station_id, session_type, file_date, file_hour),
                )
                result = cur.fetchone()
                return result[0] if result else False
        except Exception as e:
            logger.debug(f"Error checking file status: {e}")
            return False

    def is_health_imported(
        self,
        station_id: str,
        file_date: date,
        checksum: Optional[str] = None,
    ) -> bool:
        """Check if health data is already imported.

        Args:
            station_id: Station identifier
            file_date: Date of the health data
            checksum: Optional checksum to verify data hasn't changed

        Returns:
            True if data is already imported (skip reimport)
        """
        if not self._conn:
            if not self.connect():
                return False  # Can't check, allow import

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT is_health_imported(%s, %s, %s)",
                    (station_id, file_date, checksum),
                )
                result = cur.fetchone()
                return result[0] if result else False
        except Exception as e:
            logger.debug(f"Error checking import status: {e}")
            return False

    def mark_file_missing(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
    ) -> bool:
        """Mark a file as missing on the receiver.

        Args:
            station_id: Station identifier
            session_type: Session type
            file_date: Date of the file
            file_hour: Hour for hourly files
            filename: Expected filename

        Returns:
            True if successfully recorded
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """SELECT upsert_file_tracking(%s, %s, %s, %s, %s, 'missing')""",
                    (station_id, session_type, file_date, file_hour, filename),
                )
            self._conn.commit()
            logger.debug(f"Marked file as missing: {station_id}/{session_type}/{file_date}")
            return True
        except Exception as e:
            logger.debug(f"Error marking file as missing: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def mark_file_downloaded(
        self,
        station_id: str,
        session_type: str,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> bool:
        """Mark a file as successfully downloaded.

        Args:
            station_id: Station identifier
            session_type: Session type
            file_date: Date of the file
            file_hour: Hour for hourly files
            filename: Filename
            file_size: File size in bytes

        Returns:
            True if successfully recorded
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """SELECT upsert_file_tracking(%s, %s, %s, %s, %s, 'downloaded', %s)""",
                    (station_id, session_type, file_date, file_hour, filename, file_size),
                )
            self._conn.commit()
            logger.debug(f"Marked file as downloaded: {station_id}/{session_type}/{file_date}")
            return True
        except Exception as e:
            logger.debug(f"Error marking file as downloaded: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def mark_health_imported(
        self,
        station_id: str,
        file_date: date,
        samples_imported: int,
        checksum: Optional[str] = None,
        json_path: Optional[str] = None,
    ) -> bool:
        """Mark health data as imported to database.

        Args:
            station_id: Station identifier
            file_date: Date of the health data
            samples_imported: Number of samples imported
            checksum: Checksum of the data
            json_path: Path to JSON file if written

        Returns:
            True if successfully recorded
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """SELECT upsert_file_tracking(
                        %s, 'status_1hr', %s, NULL, NULL, 'downloaded',
                        NULL, %s, %s, %s
                    )""",
                    (station_id, file_date, samples_imported, checksum, json_path),
                )
            self._conn.commit()
            logger.debug(f"Marked health as imported: {station_id}/{file_date} ({samples_imported} samples)")
            return True
        except Exception as e:
            logger.debug(f"Error marking health as imported: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def get_data_availability(
        self,
        station_id: Optional[str] = None,
        session_type: str = "status_1hr",
        days: int = 30,
    ) -> Dict[str, Any]:
        """Get data availability summary.

        Args:
            station_id: Optional station filter
            session_type: Session type to check
            days: Number of days to look back

        Returns:
            Dictionary with availability statistics
        """
        if not self._conn:
            if not self.connect():
                return {}

        try:
            with self._conn.cursor() as cur:
                if station_id:
                    cur.execute(
                        """
                        SELECT
                            COUNT(*) FILTER (WHERE status = 'downloaded') as downloaded,
                            COUNT(*) FILTER (WHERE status = 'missing') as missing,
                            COUNT(*) FILTER (WHERE imported_to_db) as imported,
                            AVG(samples_imported) FILTER (WHERE samples_imported IS NOT NULL) as avg_samples
                        FROM file_tracking
                        WHERE sid = %s
                          AND session_type = %s
                          AND file_date > CURRENT_DATE - %s
                          AND file_hour IS NULL
                        """,
                        (station_id, session_type, days),
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                            sid,
                            COUNT(*) FILTER (WHERE status = 'downloaded') as downloaded,
                            COUNT(*) FILTER (WHERE status = 'missing') as missing,
                            COUNT(*) FILTER (WHERE imported_to_db) as imported
                        FROM file_tracking
                        WHERE session_type = %s
                          AND file_date > CURRENT_DATE - %s
                          AND file_hour IS NULL
                        GROUP BY sid
                        ORDER BY sid
                        """,
                        (session_type, days),
                    )

                rows = cur.fetchall()
                if station_id and rows:
                    row = rows[0]
                    return {
                        "downloaded": row[0] or 0,
                        "missing": row[1] or 0,
                        "imported": row[2] or 0,
                        "avg_samples": float(row[3]) if row[3] else None,
                    }
                elif rows:
                    return {
                        row[0]: {
                            "downloaded": row[1] or 0,
                            "missing": row[2] or 0,
                            "imported": row[3] or 0,
                        }
                        for row in rows
                    }
                return {}
        except Exception as e:
            logger.debug(f"Error getting data availability: {e}")
            return {}

    def get_download_stats(
        self,
        station_id: str,
        session_type: str = "15s_24hr",
        days: int = 7,
    ) -> Dict[str, Any]:
        """Get download statistics for Icinga check.

        Args:
            station_id: Station identifier
            session_type: Session type ('15s_24hr', '1Hz_1hr', etc.)
            days: Number of days to look back

        Returns:
            Dictionary with download stats:
            - hours_since_download: Hours since last successful download
            - latest_download: Timestamp of last download
            - downloads_expected: Expected downloads in period
            - downloads_successful: Successful downloads
            - downloads_missing: Known missing files
            - error_count: Total errors in period
        """
        if not self._conn:
            if not self.connect():
                return {}

        try:
            with self._conn.cursor() as cur:
                # Get latest successful download
                cur.execute(
                    """
                    SELECT
                        last_checked,
                        EXTRACT(EPOCH FROM (NOW() - last_checked)) / 3600 as hours_ago
                    FROM file_tracking
                    WHERE sid = %s
                      AND session_type = %s
                      AND status = 'downloaded'
                    ORDER BY last_checked DESC
                    LIMIT 1
                    """,
                    (station_id, session_type),
                )
                latest = cur.fetchone()

                # Get counts for recent period
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'downloaded') as successful,
                        COUNT(*) FILTER (WHERE status = 'missing') as missing,
                        COALESCE(SUM(error_count), 0) as total_errors,
                        COUNT(*) as total_tracked
                    FROM file_tracking
                    WHERE sid = %s
                      AND session_type = %s
                      AND file_date >= CURRENT_DATE - %s
                    """,
                    (station_id, session_type, days),
                )
                counts = cur.fetchone()

                result = {
                    "hours_since_download": None,
                    "latest_download": None,
                    "downloads_expected": days,  # Rough estimate
                    "downloads_successful": 0,
                    "downloads_missing": 0,
                    "error_count": 0,
                }

                if latest:
                    result["latest_download"] = latest[0].isoformat() if latest[0] else None
                    result["hours_since_download"] = float(latest[1]) if latest[1] else None

                if counts:
                    result["downloads_successful"] = counts[0] or 0
                    result["downloads_missing"] = counts[1] or 0
                    result["error_count"] = int(counts[2]) if counts[2] else 0

                return result

        except Exception as e:
            logger.debug(f"Error getting download stats: {e}")
            return {}

    def close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


class ArchiveFileChecker:
    """Check archive file system for expected files.

    Used by Icinga checks to verify files exist on the archive.
    """

    def __init__(self, data_prepath: Optional[str] = None):
        """Initialize archive file checker.

        Args:
            data_prepath: Base archive path. If None, tries to load from config.
        """
        self.data_prepath = data_prepath
        self._config = None

    def _load_config(self):
        """Load receivers configuration."""
        if self._config is not None:
            return

        try:
            from ..config.receivers_config import ReceiversConfig
            self._config = ReceiversConfig()

            if self.data_prepath is None:
                self.data_prepath = self._config.get_data_prepath()
        except Exception as e:
            logger.debug(f"Could not load receivers config: {e}")
            # Fall back to common paths
            if self.data_prepath is None:
                for path in ["/mnt/gpsdata", "/tmp/gpsdata"]:
                    if os.path.isdir(path):
                        self.data_prepath = path
                        break

    def _get_archive_template(self) -> str:
        """Get archive template from config or use default."""
        self._load_config()
        if self._config:
            try:
                return self._config.get_archive_template()
            except Exception:
                pass
        # Default template
        return "{data_prepath}/%Y/#b/{station}/{session}/raw/{station}%Y%m%d%H00{session_letter}{extension}"

    def _get_session_letter(self, session_type: str) -> str:
        """Get session letter for session type."""
        session_letters = {
            "15s_24hr": "a",
            "1Hz_1hr": "b",
            "status_1hr": "c",
            "15s_24hr_rinex": "a",
            "1Hz_1hr_rinex": "b",
        }
        return session_letters.get(session_type, "a")

    def _get_extension(self, receiver_type: Optional[str] = None) -> str:
        """Get file extension based on receiver type."""
        if receiver_type:
            rt = receiver_type.lower()
            if "polarx" in rt or "septentrio" in rt:
                return ".sbf.gz"
            elif "netr9" in rt:
                return ".T02"
            elif "netrs" in rt:
                return ".T00"
            elif "g10" in rt or "leica" in rt:
                return ".m00.gz"
        return ".sbf.gz"  # Default

    def build_archive_path(
        self,
        station_id: str,
        session_type: str,
        dt: datetime,
        receiver_type: Optional[str] = None,
    ) -> str:
        """Build expected archive path for a file.

        Args:
            station_id: Station ID (e.g., 'THOB')
            session_type: Session type (e.g., '15s_24hr')
            dt: Datetime for the file
            receiver_type: Optional receiver type for extension

        Returns:
            Expected archive path
        """
        self._load_config()

        template = self._get_archive_template()
        session_letter = self._get_session_letter(session_type)
        extension = self._get_extension(receiver_type)

        # Substitute placeholders
        path = template.format(
            data_prepath=self.data_prepath or "/mnt/gpsdata",
            station=station_id,
            session=session_type,
            session_letter=session_letter,
            extension=extension,
        )

        # Use gtimes for date formatting
        try:
            import gtimes.timefunc as gt
            paths = gt.datepathlist(path, "1D", datelist=[dt])
            return paths[0] if paths else path
        except Exception:
            # Fallback: manual formatting
            path = path.replace("%Y", dt.strftime("%Y"))
            path = path.replace("%m", dt.strftime("%m"))
            path = path.replace("%d", dt.strftime("%d"))
            path = path.replace("%H", dt.strftime("%H"))
            path = path.replace("%j", dt.strftime("%j"))
            path = path.replace("#b", dt.strftime("%b"))
            return path

    def get_archive_directory(
        self,
        station_id: str,
        session_type: str,
        year: Optional[int] = None,
        month: Optional[str] = None,
    ) -> str:
        """Get archive directory path for a station/session.

        Args:
            station_id: Station ID (e.g., 'THOB')
            session_type: Session type (e.g., '15s_24hr')
            year: Optional year (default: current year)
            month: Optional month abbreviation (default: current month)

        Returns:
            Archive directory path
        """
        self._load_config()

        now = datetime.now()
        if year is None:
            year = now.year
        if month is None:
            month = now.strftime("%b").lower()  # Use lowercase month (jan, feb, etc.)

        # Build directory path: {data_prepath}/{year}/{month}/{station}/{session}/raw/
        return os.path.join(
            self.data_prepath or "/mnt/gpsdata",
            str(year),
            month,
            station_id,
            session_type,
            "raw"
        )

    def check_file_status(
        self,
        station_id: str,
        session_type: str,
        days_back: int = 2,
        receiver_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Check file status by searching archive directories.

        Searches the archive directory structure for files and reports
        on file count and age.

        Args:
            station_id: Station ID (e.g., 'THOB')
            session_type: Session type (e.g., '15s_24hr')
            days_back: Number of days to check for expected files
            receiver_type: Optional receiver type (for future use)

        Returns:
            Dict with:
            - files_found: Number of files found in recent directories
            - files_expected: Expected files based on days_back
            - latest_file: Path to most recent file found
            - latest_mtime: Modification time of most recent file
            - hours_since_file: Hours since most recent file
            - archive_dir: Archive directory searched
        """
        from datetime import timedelta
        import glob

        self._load_config()

        now = datetime.now()
        files_found = 0
        latest_file = None
        latest_mtime = None
        all_files = []

        # Search current month and previous month directories
        for month_offset in range(2):
            check_date = now - timedelta(days=month_offset * 30)
            archive_dir = self.get_archive_directory(
                station_id,
                session_type,
                year=check_date.year,
                month=check_date.strftime("%b").lower(),
            )

            if os.path.isdir(archive_dir):
                # Find all files in the directory
                pattern = os.path.join(archive_dir, f"{station_id}*")
                for filepath in glob.glob(pattern):
                    if os.path.isfile(filepath):
                        mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                        all_files.append((filepath, mtime))

        # Sort by modification time (newest first)
        all_files.sort(key=lambda x: x[1], reverse=True)

        # Count files and find latest
        files_found = len(all_files)
        if all_files:
            latest_file, latest_mtime = all_files[0]

        # Calculate hours since latest file
        hours_since_file = None
        if latest_mtime:
            hours_since_file = (now - latest_mtime).total_seconds() / 3600

        # Expected files based on session type
        if session_type in ("15s_24hr", "15s_24hr_rinex"):
            files_expected = days_back  # 1 file per day
        else:
            files_expected = days_back * 24  # 1 file per hour

        # Get current archive directory for reference
        archive_dir = self.get_archive_directory(station_id, session_type)

        return {
            "files_found": files_found,
            "files_expected": files_expected,
            "latest_file": latest_file,
            "latest_mtime": latest_mtime.isoformat() if latest_mtime else None,
            "hours_since_file": hours_since_file,
            "archive_dir": archive_dir,
        }


def compute_checksum(data: Dict[str, Any]) -> str:
    """Compute checksum for health data.

    Args:
        data: Health data dictionary

    Returns:
        SHA256 checksum string
    """
    import json

    # Create stable JSON string (sorted keys)
    json_str = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(json_str.encode()).hexdigest()[:16]
