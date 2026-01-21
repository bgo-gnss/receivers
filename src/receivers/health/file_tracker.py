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
