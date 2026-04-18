"""Septentrio-specific download manager implementation."""

import logging
import os
import time
from datetime import datetime, timezone
from ftplib import FTP
from pathlib import Path
from typing import Any, Dict, List, Optional

import gtimes.timefunc as gt

from ..base.download_manager import BaseDownloadManager
from ..base.exceptions import ConfigurationError, ConnectionError

try:
    from tqdm import tqdm

    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


class SeptentrioDownloadManager(BaseDownloadManager):
    """Download manager for Septentrio receivers (PolaRX5, etc).

    Implements Septentrio-specific FTP download logic, file naming
    conventions, and directory structures.
    """

    def __init__(
        self,
        station_id: str,
        station_config: Dict[str, Any],
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize Septentrio download manager."""
        super().__init__(station_id, station_config, logger)
        self._setup_septentrio_config()

    def _setup_septentrio_config(self) -> None:
        """Set up Septentrio-specific configuration."""
        # FTP mode configuration
        ftp_mode = self.station_config.get("router", {}).get("ftp_mode", "passive")
        self.use_passive_ftp = ftp_mode == "passive"

        # Session map for Septentrio receivers
        self.session_map = {
            "15s_24hr": ("a", "LOG1_15s_24hr"),
            "1Hz_1hr": ("b", "LOG2_1Hz_1hr"),
            "status_1hr": ("c", "LOG5_status_1hr"),
        }

        self.logger.debug(f"Septentrio config - FTP passive: {self.use_passive_ftp}")

    def test_connection(self) -> Dict[str, Any]:
        """Test FTP connection to Septentrio receiver."""
        try:
            ftp = FTP()
            ftp.connect(self.ip_address, self.port, timeout=self.connection_timeout)
            ftp.login("anonymous")
            ftp.set_pasv(self.use_passive_ftp)
            ftp.quit()

            return {
                "success": True,
                "ip": self.ip_address,
                "port": self.port,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "error": None,
            }

        except Exception as e:
            return {
                "success": False,
                "ip": self.ip_address,
                "port": self.port,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
            }

    def _is_ftp_mode_error(self, error: Exception) -> bool:
        """Check if error is related to FTP passive/active mode issues.

        Common FTP mode errors:
        - "I won't open a connection" (passive mode with unreachable data IP)
        - "No route to host" (network routing issues)
        - "Connection timed out" during data transfer (might be mode issue)
        """
        error_str = str(error).lower()
        ftp_mode_indicators = [
            "won't open a connection",
            "only to",  # Part of "I won't open a connection to X (only to Y)"
            "no route to host",
            "connection refused",  # Can indicate passive mode port blocked
        ]
        return any(indicator in error_str for indicator in ftp_mode_indicators)

    def establish_connection(self) -> FTP:
        """Establish FTP connection to Septentrio receiver.

        Automatically retries with opposite FTP mode if connection fails
        with FTP mode-related errors.
        """
        connection_start = time.time()
        original_mode = self.use_passive_ftp
        mode_name = "passive" if self.use_passive_ftp else "active"

        try:
            self.logger.info(
                f"Connecting to {self.ip_address}:{self.port} (FTP {mode_name})..."
            )
            ftp = FTP()
            ftp.connect(self.ip_address, self.port, timeout=self.connection_timeout)
            ftp.login("anonymous")
            ftp.set_pasv(self.use_passive_ftp)

            connection_time = time.time() - connection_start
            self.logger.info(f"✅ Connected in {connection_time:.2f}s")

            return ftp

        except Exception as e:
            connection_time = time.time() - connection_start

            # Check if this looks like an FTP mode issue
            if self._is_ftp_mode_error(e):
                # Try opposite FTP mode
                self.use_passive_ftp = not self.use_passive_ftp
                fallback_mode = "passive" if self.use_passive_ftp else "active"

                self.logger.warning(
                    f"⚠️  FTP {mode_name} mode failed, retrying with {fallback_mode} mode..."
                )

                try:
                    ftp = FTP()
                    ftp.connect(
                        self.ip_address, self.port, timeout=self.connection_timeout
                    )
                    ftp.login("anonymous")
                    ftp.set_pasv(self.use_passive_ftp)

                    fallback_time = time.time() - connection_start
                    self.logger.info(
                        f"✅ Connected with {fallback_mode} mode in {fallback_time:.2f}s"
                    )
                    return ftp

                except Exception as fallback_error:
                    # Both modes failed, restore original and raise
                    self.use_passive_ftp = original_mode
                    self.logger.error(f"❌ Both FTP modes failed. Original error: {e}")
                    self.logger.error(f"❌ Fallback error: {fallback_error}")
                    raise ConnectionError(
                        f"Could not connect to {self.ip_address}:{self.port} (tried both FTP modes): {e}"
                    )
            else:
                # Not an FTP mode issue, raise original error
                self.logger.error(
                    f"❌ Connection failed after {connection_time:.2f}s: {e}"
                )
                raise ConnectionError(
                    f"Could not connect to {self.ip_address}:{self.port}: {e}"
                )

    def close_connection(self, connection: FTP) -> None:
        """Close FTP connection."""
        try:
            connection.quit()
            self.logger.debug("FTP connection closed")
        except Exception as e:
            self.logger.debug(f"Error closing FTP connection: {e}")

    def get_remote_file_list(self, connection: FTP, remote_path: str) -> List[str]:
        """Get list of files in remote FTP directory."""
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
        resume_offset: int = 0,
    ) -> Dict[str, Any]:
        """Download file from Septentrio receiver via FTP."""
        try:
            # Check if remote file exists and get size
            try:
                remote_size = connection.size(remote_file_path)
            except Exception as e:
                if "550" in str(e) or "not found" in str(e).lower():
                    return {
                        "success": False,
                        "error": f"Remote file not found: {remote_file_path}",
                        "remote_size": None,
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
                    }
                elif remote_size and local_size > remote_size:
                    self.logger.warning(
                        f"Local file larger than remote - removing: {local_file.name}"
                    )
                    local_file.unlink()
                    resume_offset = 0
                else:
                    resume_offset = local_size

            # Download with progress bar if available
            if HAS_TQDM and remote_size:
                return self._download_with_progress(
                    connection,
                    remote_file_path,
                    local_file_path,
                    remote_size,
                    resume_offset,
                )
            else:
                return self._download_simple(
                    connection, remote_file_path, local_file_path, resume_offset
                )

        except Exception as e:
            self.logger.error(f"Download failed for {remote_file_path}: {e}")
            return {"success": False, "error": str(e), "remote_size": None}

    def _download_with_progress(
        self,
        connection: FTP,
        remote_file_path: str,
        local_file_path: str,
        remote_size: int,
        resume_offset: int,
    ) -> Dict[str, Any]:
        """Download with tqdm progress bar."""
        filename = Path(remote_file_path).name
        desc = f"Downloading {filename}"

        with tqdm(
            total=remote_size,
            initial=resume_offset,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=desc,
        ) as pbar:
            with open(local_file_path, "ab") as f:

                def callback(chunk):
                    f.write(chunk)
                    pbar.update(len(chunk))

                connection.retrbinary(
                    f"RETR {remote_file_path}", callback, rest=resume_offset
                )

        # Verify download
        local_size = os.path.getsize(local_file_path)
        bytes_downloaded = local_size - resume_offset

        return {
            "success": local_size == remote_size,
            "remote_size": remote_size,
            "local_size": local_size,
            "bytes_downloaded": bytes_downloaded,
            "complete": local_size == remote_size,
        }

    def _download_simple(
        self,
        connection: FTP,
        remote_file_path: str,
        local_file_path: str,
        resume_offset: int,
    ) -> Dict[str, Any]:
        """Simple download without progress bar."""
        initial_size = resume_offset

        with open(local_file_path, "ab") as f:
            connection.retrbinary(
                f"RETR {remote_file_path}", f.write, rest=resume_offset
            )

        final_size = os.path.getsize(local_file_path)
        bytes_downloaded = final_size - initial_size

        return {
            "success": bytes_downloaded > 0,
            "local_size": final_size,
            "bytes_downloaded": bytes_downloaded,
        }

    def _generate_archive_path(self, dt: datetime, session: str) -> str:
        """Generate archive path for Septentrio file."""
        if session not in self.session_map:
            raise ConfigurationError(f"Unknown session type: {session}")

        # Use gtimes to generate proper archive path
        archive_format = f"{self.data_prepath}%Y/#b/{self.station_id}/{session}/raw/{self.station_id}%Y%m%d%H00a.sbf.gz"
        archive_paths = gt.datepathlist(
            archive_format, "1D", datelist=[dt], closed="both"
        )
        return archive_paths[0]

    def _generate_remote_filename(self, dt: datetime, session: str) -> str:
        """Generate remote filename for Septentrio file."""
        if session not in self.session_map:
            raise ConfigurationError(f"Unknown session type: {session}")

        # Septentrio uses RINEX-style naming: STATION#Rin2_.gz
        return f"{self.station_id}#Rin2_.gz"

    def _get_remote_file_path(self, dt: datetime) -> str:
        """Get remote directory path for Septentrio file."""
        # Calculate GPS week
        gps_week = gt.date2gpsWeek(dt)[0]

        # Use default session for path calculation (can be overridden)
        session_path = self.session_map["15s_24hr"][1]  # LOG1_15s_24hr

        return f"{self.receiver_base_path}{session_path}/{gps_week:05d}/"

    def get_session_remote_path(self, dt: datetime, session: str) -> str:
        """Get remote path for specific session type."""
        if session not in self.session_map:
            raise ConfigurationError(f"Unknown session type: {session}")

        gps_week = gt.date2gpsWeek(dt)[0]
        session_path = self.session_map[session][1]

        return f"{self.receiver_base_path}{session_path}/{gps_week:05d}/"
