"""Leica-specific download manager implementation.

Provides unified download management for Leica receivers (G10)
using existing FTP download client with enhanced BaseDownloadManager capabilities.
"""

import logging
from datetime import datetime, timezone
from ftplib import FTP
from pathlib import Path
from typing import Any, Dict, List, Optional

import gtimes.timefunc as gt

from ..base.download_manager import BaseDownloadManager
from ..base.exceptions import ConnectionError, ConfigurationError


class LeicaDownloadManager(BaseDownloadManager):
    """Download manager for Leica receivers (G10).

    This manager wraps the existing Leica FTP download client
    (LeicaFTPDownloader) and provides unified BaseDownloadManager
    interface with Phase 1 enhancements.

    Architecture:
        - Uses composition to delegate to existing FTP downloader
        - Adds Phase 1 validation and archiving (Fix #1)
        - Adds protocol-agnostic retry with reconnection (Fix #2)
    """

    def __init__(
        self,
        station_id: str,
        station_config: Dict[str, Any],
        downloader: Any,  # LeicaFTPDownloader
        logger: Optional[logging.Logger] = None
    ):
        """Initialize Leica download manager.

        Args:
            station_id: Station identifier
            station_config: Station configuration
            downloader: Existing FTP downloader instance (LeicaFTPDownloader)
            logger: Optional logger instance
        """
        super().__init__(station_id, station_config, logger)
        self.downloader = downloader
        self._connection = None  # FTP connection

        self.logger.debug(f"Leica download manager initialized with {type(downloader).__name__}")

    def test_connection(self) -> Dict[str, Any]:
        """Test FTP connection to Leica receiver."""
        try:
            # Try to establish and close a test connection
            ftp = FTP()
            ftp.connect(self.ip_address, self.port, timeout=self.connection_timeout)
            ftp.login("anonymous")
            ftp.quit()

            return {
                "success": True,
                "ip": self.ip_address,
                "port": self.port,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "error": None
            }

        except Exception as e:
            return {
                "success": False,
                "ip": self.ip_address,
                "port": self.port,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "error": str(e)
            }

    def establish_connection(self) -> FTP:
        """Establish FTP connection to Leica receiver.

        Returns:
            FTP connection object
        """
        try:
            self.logger.info(f"Connecting to {self.ip_address}:{self.port} (FTP)...")

            ftp = FTP()
            ftp.connect(self.ip_address, self.port, timeout=self.connection_timeout)
            ftp.login("anonymous")

            self.logger.info(f"✅ Connected to Leica receiver")
            self._connection = ftp
            return ftp

        except Exception as e:
            self.logger.error(f"❌ Connection failed: {e}")
            raise ConnectionError(f"Could not connect to {self.ip_address}:{self.port}: {e}")

    def close_connection(self, connection: FTP) -> None:
        """Close FTP connection.

        Args:
            connection: FTP connection to close
        """
        try:
            connection.quit()
            self.logger.debug("FTP connection closed")
            self._connection = None
        except Exception as e:
            self.logger.debug(f"Error closing FTP connection: {e}")

    def get_remote_file_list(self, connection: FTP, remote_path: str) -> List[str]:
        """Get list of files in remote FTP directory.

        Args:
            connection: Active FTP connection
            remote_path: Remote directory path

        Returns:
            List of filenames in remote directory
        """
        try:
            return connection.nlst(remote_path)
        except Exception as e:
            self.logger.warning(f"Could not list remote directory {remote_path}: {e}")
            return []

    def download_file(
        self,
        connection: FTP,
        remote_file_path: str,
        local_file_path: str,
        resume_offset: int = 0
    ) -> Dict[str, Any]:
        """Download file from Leica receiver via FTP.

        This delegates to the existing FTP downloader with enhanced
        error handling for BaseDownloadManager compatibility.

        Args:
            connection: Active FTP connection
            remote_file_path: Full path to remote file
            local_file_path: Full path for local file
            resume_offset: Byte offset to resume from

        Returns:
            Dictionary with download results including 'success' key
        """
        try:
            # Check if remote file exists and get size
            try:
                remote_size = connection.size(remote_file_path)
            except Exception as e:
                if "550" in str(e) or "not found" in str(e).lower():
                    return {
                        "success": False,
                        "error": f"Remote file not found: {remote_file_path}",
                        "remote_size": None
                    }
                else:
                    self.logger.warning(f"Could not get remote file size: {e}")
                    remote_size = None

            local_file = Path(local_file_path)

            # Handle existing local file
            if local_file.exists():
                local_size = local_file.stat().st_size
                if remote_size and local_size == remote_size:
                    self.logger.info(f"✅ File already complete: {local_file.name}")
                    return {
                        "success": True,
                        "remote_size": remote_size,
                        "local_size": local_size,
                        "bytes_downloaded": 0,
                        "complete": True
                    }
                elif remote_size and local_size > remote_size:
                    self.logger.warning(f"Local file larger than remote - removing: {local_file.name}")
                    local_file.unlink()
                    resume_offset = 0
                else:
                    resume_offset = local_size

            # Download file
            with open(local_file_path, "ab") as f:
                connection.retrbinary(
                    f"RETR {remote_file_path}",
                    f.write,
                    rest=resume_offset
                )

            # Verify download
            final_size = local_file.stat().st_size
            bytes_downloaded = final_size - resume_offset

            success = (remote_size is None) or (final_size == remote_size)

            return {
                "success": success,
                "remote_size": remote_size,
                "local_size": final_size,
                "bytes_downloaded": bytes_downloaded,
                "complete": success
            }

        except Exception as e:
            self.logger.error(f"Download failed for {remote_file_path}: {e}")
            raise  # Let download_with_retry handle the exception

    def _generate_archive_path(self, dt: datetime, session: str) -> str:
        """Generate archive path for Leica file.

        Args:
            dt: File datetime
            session: Session type (15s_24hr, etc.)

        Returns:
            Full archive path
        """
        # Leica archive format: /data_prepath/YEAR/month/STATION/session/raw/filename
        year = dt.year
        month = dt.strftime("%b").lower()  # "jan", "feb", etc.

        # Session-specific path
        session_dir = session  # "15s_24hr", etc.

        # Generate filename using gtimes
        # Leica uses .m00.gz extension in archive
        filename_format = f"{self.station_id}%Y%m%d%H%M0000a.m00.gz"
        filenames = gt.datepathlist(
            filename_format, "1D",  # Leica typically daily files
            datelist=[dt], closed="both"
        )

        archive_path = f"{self.data_prepath}/{year}/{month}/{self.station_id}/{session_dir}/raw/{filenames[0]}"
        return archive_path

    def _generate_remote_filename(self, dt: datetime, session: str) -> str:
        """Generate remote filename for Leica file.

        Args:
            dt: File datetime
            session: Session type

        Returns:
            Remote filename
        """
        # Leica remote filename format: STATION{DOY}a.m00.zip
        # Example: SKFC123a.m00.zip
        day_of_year = dt.timetuple().tm_yday
        filename = f"{self.station_id}{day_of_year:03d}a.m00.zip"
        return filename

    def _get_remote_file_path(self, dt: datetime) -> str:
        """Get remote directory path for Leica file.

        Args:
            dt: File datetime

        Returns:
            Remote directory path
        """
        # Leica G10 path format: /SD Card/Data/session_type/
        session_dir = "15s_24hr"  # Default session
        return f"/SD Card/Data/{session_dir}/"
