"""FTP download client for Leica G10 receivers.

This module provides FTP-based file download functionality for Leica G10 receivers,
using anonymous FTP access to download compressed .m00.zip files from the receiver's
SD Card storage path: /SD Card/Data/15s_24hr/
"""

import gzip
import logging
import os
import shutil
import subprocess
import sys
import time
from ftplib import FTP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..utils.file_validator import FileValidator


class ProgressBar:
    """Simple ASCII progress bar for file downloads."""

    def __init__(self, total_size: int, filename: str, width: int = 50):
        self.total_size = total_size
        self.filename = filename
        self.width = width
        self.current_size = 0
        self.start_time = time.time()
        self.last_update = 0

    def update(self, bytes_downloaded: int) -> None:
        """Update progress bar with current download progress."""
        self.current_size = bytes_downloaded
        current_time = time.time()

        # Only update display every 0.5 seconds to avoid flickering
        if current_time - self.last_update < 0.5 and bytes_downloaded < self.total_size:
            return

        self.last_update = current_time
        self._display_progress()

    def _display_progress(self) -> None:
        """Display the progress bar."""
        if self.total_size == 0:
            return

        # Calculate progress
        progress = min(self.current_size / self.total_size, 1.0)
        filled_width = int(progress * self.width)

        # Calculate speed and ETA
        elapsed_time = time.time() - self.start_time
        if elapsed_time > 0 and self.current_size > 0:
            speed_kbps = self.current_size / elapsed_time / 1024
            if progress > 0:
                eta_seconds = (elapsed_time / progress) - elapsed_time
                eta_str = (
                    f" ETA: {int(eta_seconds)}s" if eta_seconds > 0 else " ETA: --"
                )
            else:
                eta_str = " ETA: --"
        else:
            speed_kbps = 0
            eta_str = " ETA: --"

        # Build progress bar
        bar = "█" * filled_width + "░" * (self.width - filled_width)

        # Format sizes
        current_mb = self.current_size / (1024 * 1024)
        total_mb = self.total_size / (1024 * 1024)

        # Truncate filename if too long to prevent line wrapping
        display_filename = (
            self.filename[:12] if len(self.filename) > 12 else self.filename
        )

        progress_line = (
            f"\r{display_filename}: {bar} "
            f"{progress * 100:.0f}% "
            f"({current_mb:.1f}/{total_mb:.1f}MB) "
            f"{speed_kbps:.0f}KB/s{eta_str}"
        )

        # Write to stderr to avoid interfering with logging
        sys.stderr.write(progress_line)
        sys.stderr.flush()

    def finish(self) -> None:
        """Complete the progress bar and add newline."""
        self._display_progress()
        sys.stderr.write("\n")
        sys.stderr.flush()


class LeicaFTPDownloader:
    """FTP-based file downloader for Leica G10 receivers.

    Downloads compressed .m00.zip files via anonymous FTP from the receiver's
    SD Card storage, implementing the pattern: /SD Card/Data/15s_24hr/{STATION}{DOY}a.m00.zip
    """

    def __init__(
        self,
        station_id: str,
        station_config: Dict[str, Any],
        loglevel: int = logging.INFO,
    ):
        """Initialize FTP downloader with station configuration.

        Args:
            station_id: Station identifier
            station_config: Station configuration dictionary
            loglevel: Logging level
        """
        self.station_id = station_id.upper()

        self.logger = logging.getLogger(f"{__name__}.{self.station_id}")

        # Extract connection info from station config
        # Handle both new format and legacy gps_parser format
        if "router" in station_config and "receiver" in station_config:
            # New format
            self.ip = station_config["router"]["ip"]
        elif "station" in station_config:
            # Legacy gps_parser format
            self.ip = station_config["station"]["router_ip"]
        else:
            raise ValueError(f"Invalid station configuration for {station_id}")

        # Initialize file validator for resume capability
        self.file_validator = FileValidator(self.logger)

        # Last per-file error during a download_files() call. Read by the G10
        # wrapper so "All file downloads failed" surfaces the actual cause.
        self.last_file_error: Optional[str] = None

        # Get timeout settings from configuration
        from ..config.receivers_config import get_receivers_config
        from ..utils.stall_timeout import get_stall_timeout

        receivers_config = get_receivers_config()
        leica_config = receivers_config.get_receiver_config("g10")
        self.ftp_port = int(leica_config.get("ftp_port", 2160))
        self.connect_timeout = leica_config.get("ftp_timeout_connect", 30)
        cfg_data_timeout = leica_config.get("ftp_timeout_data", 120)
        self.data_timeout = get_stall_timeout(
            station_id, "g10", default=cfg_data_timeout
        )

        # Get FTP mode from station config (active/passive)
        # Station config has format: station_config['router']['ftp_mode'] = 'active'|'passive'|'auto'
        ftp_mode = station_config.get("router", {}).get("ftp_mode", "active")
        self.use_passive = ftp_mode == "passive"
        self.logger.debug(f"Leica FTP mode: {ftp_mode} (passive={self.use_passive})")

        # Track connection time for metrics
        self._last_connection_time = 0.0

        # Track remote file sizes from FTP SIZE (filename -> size in bytes)
        self.remote_sizes: Dict[str, int] = {}

        self.logger.info(f"Initialized Leica FTP downloader for {self.station_id}")

    @staticmethod
    def _safe_ftp_close(ftp: Optional[FTP]) -> None:
        """Close an FTP connection without raising.

        Used in except / finally paths to prevent zombie sockets when an
        exception fires after ftp.connect() succeeded but before quit().
        Calling close() on a stale or already-closed FTP is harmless.
        """
        if ftp is None:
            return
        try:
            ftp.close()
        except Exception:
            pass

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

    def _download_file_single_attempt(
        self,
        remote_filename: str,
        local_path: Path,
        remote_dir: str = "/SD Card/Data/15s_24hr/",
        expected_size: Optional[int] = None,
    ) -> bool:
        """Single attempt to download a file from Leica receiver via FTP.

        Automatically retries with opposite FTP mode if connection fails
        with FTP mode-related errors.
        """
        # This is the original download logic without retry wrapper
        start_time = time.time()
        original_mode = self.use_passive
        mode_name = "passive" if self.use_passive else "active"

        # Log connection details for debugging - BEFORE attempting connection
        self.logger.info(
            f"🔗 Attempting FTP connection to: {self.ip}:{self.ftp_port} (FTP {mode_name})"
        )
        self.logger.info(f"📁 Target directory: {remote_dir}")
        self.logger.info(f"📄 Target filename: {remote_filename}")
        self.logger.info(
            f"⚙️  Connection settings: timeout={self.connect_timeout}s, passive={self.use_passive}"
        )

        # ftp = None ensures the finally cleanup is safe even if FTP() raises
        # before assignment, and prevents the zombie-socket leak that fired on
        # any exception after connect() succeeded but before quit() ran.
        # Same pattern PolaRX5 uses (polarx5.py:_ftp_open_connection).
        ftp: Optional[FTP] = None
        try:
            # Connect to FTP server
            ftp = FTP()
            self.logger.info("🔄 Establishing TCP connection...")
            ftp.connect(self.ip, self.ftp_port, timeout=self.connect_timeout)
            self.logger.info("✅ FTP TCP connection established")

            ftp.login()  # Anonymous login
            self.logger.info("✅ Anonymous login successful")

            ftp.set_pasv(self.use_passive)
            self.logger.info(f"✅ Passive mode set to: {self.use_passive}")

            # Set binary mode explicitly for zip files
            ftp.voidcmd("TYPE I")
            self.logger.info("✅ Binary mode set for file transfer")

            # Change to remote directory
            self.logger.info(f"🔄 Changing to directory: {remote_dir}")
            ftp.cwd(remote_dir)
            self.logger.info(f"✅ Successfully changed to directory: {remote_dir}")

            # Get file size if not provided
            if not expected_size:
                try:
                    expected_size = ftp.size(remote_filename)
                except:
                    expected_size = 0

            # Store remote file size for tracking
            if expected_size and expected_size > 0:
                self.remote_sizes[remote_filename] = expected_size

            # Initialize progress bar
            progress_bar = None
            if expected_size and expected_size > 0:
                progress_bar = ProgressBar(expected_size, remote_filename)

            # Download with progress tracking
            bytes_written = 0
            last_progress_time = time.time()

            def progress_callback(chunk):
                nonlocal bytes_written, last_progress_time
                bytes_written += len(chunk)
                current_time = time.time()

                # Update progress bar
                if progress_bar:
                    progress_bar.update(bytes_written)

                # Check for stall timeout
                if chunk:
                    last_progress_time = current_time
                else:
                    if current_time - last_progress_time > self.data_timeout:
                        raise TimeoutError(
                            f"FTP download stalled: no data received for {self.data_timeout}s "
                            f"(downloaded {bytes_written:,} bytes)"
                        )

                return chunk

            with open(local_path, "wb") as f:
                ftp.retrbinary(
                    f"RETR {remote_filename}",
                    lambda chunk: f.write(progress_callback(chunk)),
                )

            ftp.quit()

            # Complete progress bar
            if progress_bar:
                progress_bar.finish()

            # Validate downloaded file
            download_time = time.time() - start_time
            local_file_size = bytes_written

            if expected_size is not None and expected_size > 0:
                # Log file size comparison
                self.logger.info(
                    f"Remote file size: {expected_size} bytes, Local file size: {local_file_size} bytes"
                )
                size_diff = local_file_size - expected_size

                if size_diff == 0:
                    self.logger.info(
                        f"✅ Successfully downloaded {remote_filename} ({local_file_size:,} bytes)"
                    )
                    return True
                else:
                    self.logger.error(
                        f"❌ Download incomplete for {remote_filename}: size mismatch of {size_diff} bytes"
                    )
                    self.logger.error(
                        f"   Expected: {expected_size:,} bytes, Got: {local_file_size:,} bytes"
                    )
                    return False
            else:
                # No expected size - basic validation
                if local_file_size > 0:
                    self.logger.info(
                        f"✅ Downloaded {remote_filename} ({local_file_size:,} bytes)"
                    )
                    return True
                else:
                    self.logger.error(
                        f"❌ Download failed for {remote_filename}: empty file"
                    )
                    return False

        except TimeoutError as e:
            if "progress_bar" in locals() and progress_bar:
                progress_bar.finish()
            self.logger.error(f"FTP download stalled for {remote_filename}: {e}")
            self._safe_ftp_close(ftp)
            return False
        except Exception as e:
            if "progress_bar" in locals() and progress_bar:
                progress_bar.finish()

            # Close the failed primary connection before opening the fallback —
            # otherwise we leak a TCP socket on every mode-switch retry.
            self._safe_ftp_close(ftp)
            ftp = None

            # Check if this is an FTP mode error and we should try fallback
            if self._is_ftp_mode_error(e):
                # Try opposite FTP mode
                self.use_passive = not original_mode
                fallback_mode = "passive" if self.use_passive else "active"

                self.logger.warning(
                    f"⚠️  FTP {mode_name} mode failed, retrying with {fallback_mode} mode..."
                )

                try:
                    # Retry with opposite mode
                    ftp = FTP()
                    ftp.connect(self.ip, self.ftp_port, timeout=self.connect_timeout)
                    ftp.login()
                    ftp.set_pasv(self.use_passive)
                    self.logger.info(f"✅ Connected with {fallback_mode} mode")

                    # Set binary mode
                    ftp.voidcmd("TYPE I")

                    # Change to remote directory
                    ftp.cwd(remote_dir)

                    # Get file size if not provided
                    if not expected_size:
                        try:
                            expected_size = ftp.size(remote_filename)
                        except:
                            expected_size = 0

                    # Store remote file size for tracking
                    if expected_size and expected_size > 0:
                        self.remote_sizes[remote_filename] = expected_size

                    # Download file
                    bytes_written = 0
                    with open(local_path, "wb") as f:

                        def simple_callback(chunk):
                            nonlocal bytes_written
                            bytes_written += len(chunk)
                            return chunk

                        ftp.retrbinary(
                            f"RETR {remote_filename}",
                            lambda chunk: f.write(simple_callback(chunk)),
                        )

                    ftp.quit()

                    # Validate download
                    if bytes_written > 0:
                        self.logger.info(
                            f"✅ Downloaded {remote_filename} with {fallback_mode} mode ({bytes_written:,} bytes)"
                        )
                        return True
                    else:
                        self.logger.error(
                            f"❌ Download failed with {fallback_mode} mode: empty file"
                        )
                        self.use_passive = original_mode
                        return False

                except Exception as fallback_error:
                    # Both modes failed, restore original
                    self.use_passive = original_mode
                    self.logger.error(f"❌ Both FTP modes failed. Original error: {e}")
                    self.logger.error(f"❌ Fallback error: {fallback_error}")
                    self._safe_ftp_close(ftp)
                    return False
            else:
                # Not an FTP mode issue — connection was already closed above,
                # nothing more to clean up here.
                self.logger.error(f"FTP error downloading {remote_filename}: {e}")
                return False

    def download_file(
        self,
        remote_filename: str,
        local_path: Path,
        remote_dir: str = "/SD Card/Data/15s_24hr/",
        expected_size: Optional[int] = None,
        retry_count: int = 3,
        session_type: str = "unknown",
    ) -> bool:
        """Download a single file from Leica receiver via FTP with retry and reconnection logic.

        Args:
            remote_filename: Remote filename to download (e.g., 'SKFC265a.m00.zip')
            local_path: Local file path to save to
            remote_dir: Remote directory path (e.g., '/SD Card/Data/1s_1hr/')
            expected_size: Expected file size for validation (optional)
            retry_count: Number of retries on timeout (default: 3)
            session_type: Session type for download logging (e.g. '15s_24hr')

        Returns:
            True if download successful, False otherwise
        """
        from ..utils.stall_timeout import record_download

        # Timeout/connection error patterns that require reconnection
        timeout_patterns = [
            "timed out",
            "timeout",
            "cannot read from timed out",
            "connection reset",
            "broken pipe",
            "connection refused",
        ]

        initial_delay = 0.5

        # Ensure local_path is a Path object
        if isinstance(local_path, str):
            local_path = Path(local_path)

        # Create parent directory
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Try download with retries and reconnection
        for attempt in range(retry_count + 1):
            start_time = time.time()
            if attempt == 0:
                self.logger.info(f"Downloading {remote_filename}")
            else:
                delay = initial_delay * attempt
                self.logger.info(f"🔄 Retrying in {delay:.1f}s...")
                time.sleep(delay)
                self.logger.info(
                    f"Downloading {remote_filename} (attempt {attempt + 1}/{retry_count + 1})"
                )

            try:
                # Attempt single download (this creates fresh FTP connection)
                success = self._download_file_single_attempt(
                    remote_filename, local_path, remote_dir, expected_size
                )
                duration = time.time() - start_time
                dl_size = local_path.stat().st_size if local_path.exists() else 0

                if success:
                    record_download(
                        self.station_id,
                        session_type,
                        "completed",
                        filename=remote_filename,
                        duration_seconds=duration,
                        bytes_downloaded=dl_size,
                        file_size=self.remote_sizes.get(remote_filename),
                        stall_timeout_used=self.data_timeout,
                        attempt=attempt + 1,
                    )
                    return True
                elif attempt < retry_count:
                    _err_msg = "Download returned failure"
                    self.last_file_error = _err_msg
                    record_download(
                        self.station_id,
                        session_type,
                        "failed",
                        filename=remote_filename,
                        duration_seconds=duration,
                        bytes_downloaded=dl_size,
                        file_size=self.remote_sizes.get(remote_filename),
                        stall_timeout_used=self.data_timeout,
                        attempt=attempt + 1,
                        message=_err_msg,
                    )
                    self.logger.warning(
                        f"⚠️ Download attempt {attempt + 1} failed, retrying {remote_filename}..."
                    )
                    continue
                else:
                    _err_msg = f"Failed after {retry_count + 1} attempts"
                    self.last_file_error = _err_msg
                    record_download(
                        self.station_id,
                        session_type,
                        "failed",
                        filename=remote_filename,
                        duration_seconds=duration,
                        bytes_downloaded=dl_size,
                        file_size=self.remote_sizes.get(remote_filename),
                        stall_timeout_used=self.data_timeout,
                        attempt=attempt + 1,
                        message=_err_msg,
                    )
                    self.logger.error(
                        f"❌ Download failed after {retry_count + 1} attempts: {remote_filename}"
                    )
                    return False

            except (TimeoutError, ConnectionError) as e:
                error_msg = str(e).lower()
                duration = time.time() - start_time
                is_stall = isinstance(e, TimeoutError) or "stall" in error_msg
                _err_msg = str(e)[:500]
                self.last_file_error = _err_msg
                record_download(
                    self.station_id,
                    session_type,
                    "stall_timeout" if is_stall else "failed",
                    filename=remote_filename,
                    duration_seconds=duration,
                    file_size=self.remote_sizes.get(remote_filename),
                    stall_timeout_used=self.data_timeout,
                    attempt=attempt + 1,
                    message=_err_msg,
                )

                # If this was the last attempt, give up
                if attempt >= retry_count:
                    self.logger.error(
                        f"❌ Download failed after {retry_count + 1} attempts: {e}"
                    )
                    return False

                # Log the failure
                self.logger.warning(f"⚠️ Download attempt {attempt + 1} failed: {e}")

                # Check if timeout pattern - always reconnect for FTP
                if any(pattern in error_msg for pattern in timeout_patterns):
                    self.logger.info(
                        "🔄 Connection timeout detected - fresh connection will be established on retry"
                    )

            except Exception as e:
                error_msg = str(e).lower()
                duration = time.time() - start_time
                _err_msg = str(e)[:500]
                self.last_file_error = _err_msg
                record_download(
                    self.station_id,
                    session_type,
                    "failed",
                    filename=remote_filename,
                    duration_seconds=duration,
                    file_size=self.remote_sizes.get(remote_filename),
                    stall_timeout_used=self.data_timeout,
                    attempt=attempt + 1,
                    message=_err_msg,
                )

                # If this was the last attempt, give up
                if attempt >= retry_count:
                    self.logger.error(
                        f"❌ Download failed after {retry_count + 1} attempts: {e}"
                    )
                    return False

                # Log the failure
                self.logger.warning(f"⚠️ Download attempt {attempt + 1} failed: {e}")

                # Check if timeout/connection error
                if any(pattern in error_msg for pattern in timeout_patterns):
                    self.logger.info(
                        "🔄 Connection error detected - fresh connection will be established on retry"
                    )

        return False

    def download_files(
        self,
        files_dict: Dict[str, str],
        tmp_dir: Path,
        clean_tmp: bool = True,
        process_callback: Optional[callable] = None,
        max_retries: int = 3,
    ) -> List[str]:
        """Download multiple files from Leica receiver.

        Args:
            files_dict: Dictionary mapping filename -> remote_directory
            tmp_dir: Temporary download directory
            clean_tmp: Whether to clean temporary directory first
            process_callback: Optional callback function(zip_path) -> processed_path
                            Called immediately after each successful download to unzip+archive

        Returns:
            List of successfully downloaded/processed file paths
        """
        if clean_tmp and tmp_dir.exists():
            self.logger.info(f"Cleaning temporary directory: {tmp_dir}")
            files_removed = self.file_validator.clean_directory(str(tmp_dir))
            if files_removed > 0:
                self.logger.info(f"Removed {files_removed} files from tmp directory")

        # Ensure tmp directory exists
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # Reset before each batch so the wrapper surfaces this run's last error.
        self.last_file_error = None

        downloaded_files = []
        total_files = len(files_dict)

        # Log station connection details
        self.logger.info(f"Station connection: {self.ip}:{self.ftp_port}")
        remote_paths = set(files_dict.values())
        for remote_path in remote_paths:
            self.logger.info(f"Remote path: {remote_path}")

        for filename, remote_directory in sorted(files_dict.items(), reverse=True):
            self.logger.info(f"📁 Attempting download: {filename}")
            self.logger.info(f"🌐 Remote path: {remote_directory}")
            self.logger.info(f"🔗 FTP server: {self.ip}:{self.ftp_port}")

            local_file_path = tmp_dir / filename

            # Check if file already exists and is complete
            if local_file_path.exists():
                validation = self.file_validator.validate_file(str(local_file_path))
                if validation["valid"]:
                    file_size = local_file_path.stat().st_size
                    self.logger.info(
                        f"📁 Local file already exists and is valid ({file_size:,} bytes): {filename}"
                    )
                    downloaded_files.append(str(local_file_path))
                    continue

            # Download the file with correct remote directory
            success = self.download_file(
                filename,
                local_file_path,
                remote_dir=remote_directory,
                expected_size=None,
                retry_count=max_retries,
            )

            if success:
                # If callback provided, process file immediately (unzip+archive)
                if process_callback:
                    processed_path = process_callback(str(local_file_path))
                    if processed_path:
                        downloaded_files.append(processed_path)
                    else:
                        # Callback failed, add original ZIP path
                        downloaded_files.append(str(local_file_path))
                else:
                    # No immediate processing, add ZIP path
                    downloaded_files.append(str(local_file_path))
            else:
                self.logger.error(f"❌ Failed to download {filename}")

            # Add longer delay between downloads to avoid overwhelming the receiver
            time.sleep(5)

        self.logger.info(
            f"Download complete: {len(downloaded_files)}/{total_files} files successful"
        )
        return downloaded_files

    def test_connection(self) -> Dict[str, Any]:
        """Test FTP connection to Leica receiver.

        Returns:
            Dictionary with connection test results
        """
        self.logger.info("Testing FTP connection...")
        start_time = time.time()

        try:
            # Test FTP connection
            ftp = FTP()
            ftp.connect(self.ip, self.ftp_port, timeout=self.connect_timeout)
            ftp.login()  # Anonymous login
            ftp.set_pasv(self.use_passive)

            # Test directory access - try common directories
            directories_to_test = ["/SD Card/Data/15s_24hr/", "/SD Card/Data/1s_1hr/"]
            file_list = []
            accessible_dirs = []

            for test_dir in directories_to_test:
                try:
                    ftp.cwd(test_dir)
                    dir_files = ftp.nlst()
                    file_list.extend(dir_files)
                    accessible_dirs.append(test_dir)
                    self.logger.info(
                        f"Directory accessible: {test_dir} ({len(dir_files)} files)"
                    )
                except Exception as e:
                    self.logger.debug(f"Directory not accessible: {test_dir} - {e}")

            ftp.quit()

            connection_time = time.time() - start_time
            self._last_connection_time = connection_time

            return {
                "success": True,
                "duration": connection_time,
                "directory_accessible": len(accessible_dirs) > 0,
                "accessible_directories": accessible_dirs,
                "files_found": len(file_list),
                "sample_files": file_list[:5] if file_list else [],
            }

        except Exception as e:
            connection_time = time.time() - start_time
            self._last_connection_time = connection_time

            return {"success": False, "duration": connection_time, "error": str(e)}

    def close(self):
        """Close FTP connections."""
        # FTP connections are closed after each operation
        pass

    def __del__(self):
        """Clean up resources."""
        self.close()
