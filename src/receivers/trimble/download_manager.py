"""Trimble-specific download manager implementation.

Provides unified download management for Trimble receivers (NetR9, NetR5, NetRS)
using existing HTTP download clients with enhanced BaseDownloadManager capabilities.
"""

import logging
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import gtimes.timefunc as gt

from ..base.download_manager import BaseDownloadManager
from ..base.exceptions import ConfigurationError, ConnectionError


class TrimbleDownloadManager(BaseDownloadManager):
    """Download manager for Trimble receivers (NetR9, NetR5, NetRS).

    This manager wraps the existing Trimble HTTP download clients
    (NetR9HTTPDownloader, NetRSHTTPDownloader) and provides unified
    BaseDownloadManager interface with Phase 1 enhancements.

    Architecture:
        - Uses composition to delegate to existing HTTP downloaders
        - Adds Phase 1 validation and archiving (Fix #1)
        - Adds protocol-agnostic retry with reconnection (Fix #2)
    """

    def __init__(
        self,
        station_id: str,
        station_config: Dict[str, Any],
        downloader: Any,  # NetR9HTTPDownloader or NetRSHTTPDownloader
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize Trimble download manager.

        Args:
            station_id: Station identifier
            station_config: Station configuration
            downloader: Existing HTTP downloader instance (NetR9/NetRS)
            logger: Optional logger instance
        """
        super().__init__(station_id, station_config, logger)
        self.downloader = downloader
        self._connection = None  # HTTP connection state

        self.logger.debug(
            f"Trimble download manager initialized with {type(downloader).__name__}"
        )

    def test_connection(self) -> Dict[str, Any]:
        """Test HTTP connection to Trimble receiver."""
        try:
            # Use downloader's HTTP client to test connection
            result = self.downloader.http_client.test_connection()
            return {
                "success": result.get("success", False),
                "ip": self.ip_address,
                "port": self.port,
                "timestamp": datetime.now(UTC).isoformat(),
                "error": result.get("error"),
            }
        except Exception as e:
            return {
                "success": False,
                "ip": self.ip_address,
                "port": self.port,
                "timestamp": datetime.now(UTC).isoformat(),
                "error": str(e),
            }

    def establish_connection(self) -> Any:
        """Establish HTTP connection to Trimble receiver.

        For HTTP-based receivers, this is a lightweight operation as
        connections are managed per-request by the HTTP client.

        Returns:
            HTTP client instance (connection state)
        """
        try:
            # For HTTP, we just return the client as "connection"
            # The actual connection is managed by requests library
            self._connection = self.downloader.http_client
            self.logger.debug(f"HTTP connection established to {self.ip_address}")
            return self._connection

        except Exception as e:
            self.logger.error(f"Failed to establish HTTP connection: {e}")
            raise ConnectionError(f"Could not connect to {self.ip_address}: {e}")

    def close_connection(self, connection: Any) -> None:
        """Close HTTP connection.

        For HTTP-based receivers, this is a no-op as connections
        are managed by the requests library's connection pooling.

        Args:
            connection: HTTP client instance (ignored)
        """
        self.logger.debug(
            "HTTP connection closed (connection pool managed by requests)"
        )
        self._connection = None

    def get_remote_file_list(self, connection: Any, remote_path: str) -> List[str]:
        """Get list of files in remote HTTP directory.

        Args:
            connection: HTTP client instance
            remote_path: Remote directory path

        Returns:
            List of filenames in remote directory
        """
        try:
            # Use downloader's method to list directory
            files = self.downloader.list_directory(remote_path)
            return files
        except Exception as e:
            self.logger.warning(f"Could not list remote directory {remote_path}: {e}")
            return []

    def download_file(
        self,
        connection: Any,
        remote_file_path: str,
        local_file_path: str,
        resume_offset: int = 0,
    ) -> Dict[str, Any]:
        """Download file from Trimble receiver via HTTP.

        This delegates to the existing HTTP downloader with enhanced
        error handling for BaseDownloadManager compatibility.

        Args:
            connection: HTTP client instance
            remote_file_path: Full path to remote file
            local_file_path: Full path for local file
            resume_offset: Byte offset to resume from

        Returns:
            Dictionary with download results including 'success' key
        """
        try:
            # Use downloader's download method
            result = self.downloader.download_single_file(
                remote_file_path, local_file_path, resume_offset=resume_offset
            )

            # Ensure result has 'success' key for BaseDownloadManager compatibility
            if "success" not in result:
                result["success"] = result.get("complete", False)

            return result

        except Exception as e:
            self.logger.error(f"Download failed for {remote_file_path}: {e}")
            raise  # Let download_with_retry handle the exception

    def _generate_archive_path(self, dt: datetime, session: str) -> str:
        """Generate archive path for Trimble file.

        Args:
            dt: File datetime
            session: Session type (15s_24hr, 1Hz_1hr, etc.)

        Returns:
            Full archive path
        """
        # Use gtimes to generate proper archive path
        # Trimble format: /data_prepath/YEAR/month/STATION/session/raw/filename
        year = dt.year
        month = dt.strftime("%b").lower()  # "jan", "feb", etc.

        # Session-specific path
        session_dir = session  # "15s_24hr", "1Hz_1hr", etc.

        # Generate filename using gtimes
        # Trimble uses .T02 extension
        filename_format = f"{self.station_id}%Y%m%d%H00a.T02"
        filenames = gt.datepathlist(
            filename_format,
            "1H" if "1hr" in session.lower() else "1D",
            datelist=[dt],
            closed="both",
        )

        archive_path = f"{self.data_prepath}/{year}/{month}/{self.station_id}/{session_dir}/raw/{filenames[0]}"
        return archive_path

    def _generate_remote_filename(self, dt: datetime, session: str) -> str:
        """Generate remote filename for Trimble file.

        Args:
            dt: File datetime
            session: Session type

        Returns:
            Remote filename
        """
        # Trimble filename format: STATION YYYYMMDDHHMMSS.T02
        # Example: MANA 20251101000000.T02
        filename = f"{self.station_id} {dt.strftime('%Y%m%d%H%M%S')}.T02"
        return filename

    def _get_remote_file_path(self, dt: datetime) -> str:
        """Get remote directory path for Trimble file.

        Args:
            dt: File datetime

        Returns:
            Remote directory path
        """
        # Trimble path format: /Internal/YYYYMM/session_type/
        year_month = dt.strftime("%Y%m")
        # Use default session for path calculation
        session_dir = "15s_24hr"  # Can be overridden

        return f"/Internal/{year_month}/{session_dir}/"
