"""Shared download tracking utility for all receiver types.

Provides receiver-independent file tracking for:
- Skipping known missing files before download
- Marking files as downloaded after success
- Marking files as missing when not found on server
"""

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class DownloadTracker:
    """Track file downloads across all receiver types.

    Provides a unified interface for file tracking that can be used
    by PolaRX5, NetR9, NetRS, G10, and other receivers.
    """

    def __init__(self, station_id: str, session: str):
        """Initialize download tracker.

        Args:
            station_id: Station identifier (e.g., 'ISFS', 'MANA')
            session: Session type (e.g., '15s_24hr', '1Hz_1hr')
        """
        self.station_id = station_id
        self.session = session
        self.is_hourly = "1hr" in session.lower()
        self._tracker = None
        self._connected = False

    def connect(self) -> bool:
        """Connect to file tracking database.

        Returns:
            True if connected successfully
        """
        try:
            from ..health import FileTracker

            self._tracker = FileTracker()
            self._connected = self._tracker.connect()
            if self._connected:
                logger.debug(
                    f"Download tracker connected for {self.station_id}/{self.session}"
                )
            else:
                logger.debug("Download tracking disabled (database unavailable)")
            return self._connected
        except ImportError:
            logger.debug("Download tracking disabled (psycopg2 not installed)")
            return False
        except Exception as e:
            logger.debug(f"Download tracking disabled: {e}")
            return False

    def close(self):
        """Close database connection."""
        if self._tracker:
            self._tracker.close()
            self._tracker = None
            self._connected = False

    def is_file_missing(self, file_date: date, file_hour: Optional[int] = None) -> bool:
        """Check if a file is known to be missing.

        Args:
            file_date: Date of the file
            file_hour: Hour for hourly files (0-23), None for daily

        Returns:
            True if file is known to be missing (should skip)
        """
        if not self._connected:
            return False

        hour = file_hour if self.is_hourly else None
        return self._tracker.is_file_missing(
            self.station_id, self.session, file_date, hour
        )

    def mark_downloaded(
        self,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
        file_size: Optional[int] = None,
        remote_file_size: Optional[int] = None,
    ) -> bool:
        """Mark a file as successfully downloaded.

        Args:
            file_date: Date of the file
            file_hour: Hour for hourly files (0-23), None for daily
            filename: Original filename
            file_size: File size in bytes
            remote_file_size: File size reported by receiver (FTP SIZE / HTTP Content-Length)

        Returns:
            True if successfully recorded
        """
        if not self._connected:
            return False

        hour = file_hour if self.is_hourly else None
        return self._tracker.mark_file_downloaded(
            self.station_id,
            self.session,
            file_date,
            hour,
            filename,
            file_size,
            remote_file_size=remote_file_size,
        )

    def mark_archived(
        self,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
        file_size: Optional[int] = None,
        remote_file_size: Optional[int] = None,
    ) -> bool:
        """Mark a file as successfully archived.

        Args:
            file_date: Date of the file
            file_hour: Hour for hourly files (0-23), None for daily
            filename: Original filename
            file_size: File size in bytes
            remote_file_size: File size reported by receiver

        Returns:
            True if successfully recorded
        """
        if not self._connected:
            return False

        hour = file_hour if self.is_hourly else None
        return self._tracker.mark_file_archived(
            self.station_id,
            self.session,
            file_date,
            hour,
            filename,
            file_size,
            remote_file_size=remote_file_size,
        )

    def mark_missing(
        self,
        file_date: date,
        file_hour: Optional[int] = None,
        filename: Optional[str] = None,
    ) -> bool:
        """Mark a file as missing on the receiver.

        Args:
            file_date: Date of the file
            file_hour: Hour for hourly files (0-23), None for daily
            filename: Expected filename

        Returns:
            True if successfully recorded
        """
        if not self._connected:
            return False

        hour = file_hour if self.is_hourly else None
        return self._tracker.mark_file_missing(
            self.station_id, self.session, file_date, hour, filename
        )

    def filter_known_missing(
        self, file_datetime_dict: Dict[datetime, Any]
    ) -> Tuple[Dict[datetime, Any], List[datetime]]:
        """Filter out files known to be missing.

        Args:
            file_datetime_dict: Dictionary with datetime keys

        Returns:
            Tuple of (filtered_dict, skipped_datetimes)
        """
        if not self._connected:
            return file_datetime_dict, []

        filtered = {}
        skipped = []

        for dt, value in file_datetime_dict.items():
            file_date = dt.date() if hasattr(dt, "date") else dt
            file_hour = dt.hour if self.is_hourly and hasattr(dt, "hour") else None

            if self.is_file_missing(file_date, file_hour):
                skipped.append(dt)
                logger.info(f"⏭️  Skipping {dt} (known missing, not retrying)")
            else:
                filtered[dt] = value

        return filtered, skipped

    def track_download_results(
        self,
        requested_files: Dict[datetime, Any],
        downloaded_files: List[str],
        failed_files: Optional[Set[datetime]] = None,
    ) -> Dict[str, int]:
        """Track results of a download operation.

        Args:
            requested_files: Dict of datetime -> file info that were requested
            downloaded_files: List of successfully downloaded file paths
            failed_files: Optional set of datetimes that failed (404/missing)

        Returns:
            Dict with counts: {'downloaded': N, 'missing': M}
        """
        if not self._connected:
            return {"downloaded": 0, "missing": 0}

        downloaded_count = 0
        missing_count = 0

        # Track downloaded files
        for dt, value in requested_files.items():
            file_date = dt.date() if hasattr(dt, "date") else dt
            file_hour = dt.hour if self.is_hourly and hasattr(dt, "hour") else None

            # Check if this datetime's file was downloaded
            # Value structure varies by receiver, try to extract filename
            filename = None
            if isinstance(value, tuple) and len(value) >= 2:
                filename = value[1]  # (archive_path, filename) structure
            elif isinstance(value, str):
                filename = Path(value).name

            # Check if file was downloaded (by matching filename in downloaded_files)
            was_downloaded = False
            file_size = None
            if filename:
                for downloaded_path in downloaded_files:
                    if (
                        filename in downloaded_path
                        or Path(downloaded_path).name == filename
                    ):
                        was_downloaded = True
                        try:
                            file_size = Path(downloaded_path).stat().st_size
                        except OSError:
                            pass
                        break

            if was_downloaded:
                self.mark_downloaded(file_date, file_hour, filename, file_size)
                downloaded_count += 1
            elif failed_files and dt in failed_files:
                self.mark_missing(file_date, file_hour, filename)
                missing_count += 1

        return {"downloaded": downloaded_count, "missing": missing_count}

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


def parse_date_from_filename(
    filename: str, station_id: str
) -> Optional[Tuple[date, Optional[int]]]:
    """Parse date and hour from various filename formats.

    Supports formats:
    - Septentrio: ISFS202601170000a.sbf.gz -> 2026-01-17, None (daily)
    - Septentrio hourly: ISFS202601170100b.sbf.gz -> 2026-01-17, 1
    - Leica: SKFC266a.m00 -> day 266 of current year
    - Trimble: Similar patterns

    Args:
        filename: Filename to parse
        station_id: Station ID for validation

    Returns:
        Tuple of (date, hour) or None if cannot parse
    """
    # Try Septentrio format: SSSS20260117HHMM[a|b].sbf.gz
    # a = daily, b = hourly
    septentrio_match = re.match(
        rf"^{re.escape(station_id)}(\d{{4}})(\d{{2}})(\d{{2}})(\d{{2}})(\d{{2}})([ab])",
        filename,
        re.IGNORECASE,
    )
    if septentrio_match:
        year = int(septentrio_match.group(1))
        month = int(septentrio_match.group(2))
        day = int(septentrio_match.group(3))
        hour = int(septentrio_match.group(4))
        session_type = septentrio_match.group(6)  # 'a' = daily, 'b' = hourly
        file_date = date(year, month, day)
        file_hour = hour if session_type == "b" else None
        return file_date, file_hour

    # Try Leica format: SSSS[DDD][letter].m00
    # DDD = day of year, letter = session (a=daily) or hour (a-x for 0-23)
    leica_match = re.match(
        rf"^{re.escape(station_id)}(\d{{3}})([a-x])",
        filename,
        re.IGNORECASE,
    )
    if leica_match:
        day_of_year = int(leica_match.group(1))
        session_letter = leica_match.group(2).lower()
        # Assume current year for Leica files
        current_year = datetime.now().year
        try:
            file_date = date(current_year, 1, 1) + timedelta(days=day_of_year - 1)
            # If letter is 'a', it's daily; otherwise b-x maps to hours 1-23
            file_hour = None
            if session_letter != "a":
                file_hour = ord(session_letter) - ord("a")
            return file_date, file_hour
        except ValueError:
            pass

    return None
