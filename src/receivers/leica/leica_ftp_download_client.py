"""FTP download client for Leica G10 receivers.

This module provides FTP-based file download functionality for Leica G10 receivers,
using anonymous FTP access to download compressed .m00.zip files from the receiver's
SD Card storage path: /SD Card/Data/15s_24hr/
"""

import gzip
import logging
import os
import shutil
import sys
import subprocess
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
                eta_str = f" ETA: {int(eta_seconds)}s" if eta_seconds > 0 else " ETA: --"
            else:
                eta_str = " ETA: --"
        else:
            speed_kbps = 0
            eta_str = " ETA: --"

        # Build progress bar
        bar = '█' * filled_width + '░' * (self.width - filled_width)

        # Format sizes
        current_mb = self.current_size / (1024 * 1024)
        total_mb = self.total_size / (1024 * 1024)

        # Truncate filename if too long to prevent line wrapping
        display_filename = self.filename[:12] if len(self.filename) > 12 else self.filename

        progress_line = (
            f"\r{display_filename}: {bar} "
            f"{progress*100:.0f}% "
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

    def __init__(self, station_id: str, station_config: Dict[str, Any], loglevel: int = logging.INFO):
        """Initialize FTP downloader with station configuration.

        Args:
            station_id: Station identifier
            station_config: Station configuration dictionary
            loglevel: Logging level
        """
        self.station_id = station_id.upper()

        # Set up logging
        self.logger = self._get_logger(loglevel)

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

        # Get timeout settings from configuration
        from ..config.receivers_config import get_receivers_config
        receivers_config = get_receivers_config()
        leica_config = receivers_config.get_receiver_config("g10")
        self.ftp_port = int(leica_config.get("ftp_port", 2160))
        self.connect_timeout = leica_config.get("ftp_timeout_connect", 30)
        self.data_timeout = leica_config.get("ftp_timeout_data", 120)
        self.use_passive = leica_config.get("ftp_passive", "false").lower() == "true"

        # Track connection time for metrics
        self._last_connection_time = 0.0

        self.logger.info(f"Initialized Leica FTP downloader for {self.station_id}")

    def _get_logger(self, level: int = logging.INFO) -> logging.Logger:
        """Set up logger for this receiver instance."""
        logger_name = f"{__name__}.{self.station_id}"
        logger = logging.getLogger(logger_name)

        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(level)
            logger.propagate = False

        return logger

    def _download_file_single_attempt(self, remote_filename: str, local_path: Path,
                                    remote_dir: str = "/SD Card/Data/15s_24hr/",
                                    expected_size: Optional[int] = None) -> bool:
        """Single attempt to download a file from Leica receiver via FTP."""
        # This is the original download logic without retry wrapper
        start_time = time.time()

        # Log connection details for debugging - BEFORE attempting connection
        self.logger.info(f"🔗 Attempting FTP connection to: {self.ip}:{self.ftp_port}")
        self.logger.info(f"📁 Target directory: {remote_dir}")
        self.logger.info(f"📄 Target filename: {remote_filename}")
        self.logger.info(f"⚙️  Connection settings: timeout={self.connect_timeout}s, passive={self.use_passive}")

        try:
            # Connect to FTP server
            ftp = FTP()
            self.logger.info(f"🔄 Establishing TCP connection...")
            ftp.connect(self.ip, self.ftp_port, timeout=self.connect_timeout)
            self.logger.info(f"✅ FTP TCP connection established")

            ftp.login()  # Anonymous login
            self.logger.info(f"✅ Anonymous login successful")

            ftp.set_pasv(self.use_passive)
            self.logger.info(f"✅ Passive mode set to: {self.use_passive}")

            # Set binary mode explicitly for zip files
            ftp.voidcmd('TYPE I')
            self.logger.info(f"✅ Binary mode set for file transfer")

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

            with open(local_path, 'wb') as f:
                ftp.retrbinary(f'RETR {remote_filename}',
                             lambda chunk: f.write(progress_callback(chunk)))

            ftp.quit()

            # Complete progress bar
            if progress_bar:
                progress_bar.finish()

            # Validate downloaded file
            download_time = time.time() - start_time
            local_file_size = bytes_written

            if expected_size is not None and expected_size > 0:
                # Log file size comparison
                self.logger.info(f"Remote file size: {expected_size} bytes, Local file size: {local_file_size} bytes")
                size_diff = local_file_size - expected_size

                if size_diff == 0:
                    self.logger.info(f"✅ Successfully downloaded {remote_filename} ({local_file_size:,} bytes)")
                    return True
                else:
                    self.logger.error(f"❌ Download incomplete for {remote_filename}: size mismatch of {size_diff} bytes")
                    self.logger.error(f"   Expected: {expected_size:,} bytes, Got: {local_file_size:,} bytes")
                    return False
            else:
                # No expected size - basic validation
                if local_file_size > 0:
                    self.logger.info(f"✅ Downloaded {remote_filename} ({local_file_size:,} bytes)")
                    return True
                else:
                    self.logger.error(f"❌ Download failed for {remote_filename}: empty file")
                    return False

        except TimeoutError as e:
            if 'progress_bar' in locals() and progress_bar:
                progress_bar.finish()
            self.logger.error(f"FTP download stalled for {remote_filename}: {e}")
            return False
        except Exception as e:
            if 'progress_bar' in locals() and progress_bar:
                progress_bar.finish()
            self.logger.error(f"FTP error downloading {remote_filename}: {e}")
            return False

    def download_file(self, remote_filename: str, local_path: Path,
                     remote_dir: str = "/SD Card/Data/15s_24hr/",
                     expected_size: Optional[int] = None, retry_count: int = 1) -> bool:
        """Download a single file from Leica receiver via FTP with retry logic.

        Args:
            remote_filename: Remote filename to download (e.g., 'SKFC265a.m00.zip')
            local_path: Local file path to save to
            remote_dir: Remote directory path (e.g., '/SD Card/Data/1s_1hr/')
            expected_size: Expected file size for validation (optional)
            retry_count: Number of retries on timeout (default: 1)

        Returns:
            True if download successful, False otherwise
        """
        # Ensure local_path is a Path object
        if isinstance(local_path, str):
            local_path = Path(local_path)

        # Create parent directory
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Try download with retries
        for attempt in range(retry_count + 1):
            if attempt > 0:
                self.logger.info(f"🔄 Retry attempt {attempt}/{retry_count} for {remote_filename}")
                time.sleep(10)  # Wait before retrying

            try:
                # Attempt single download
                success = self._download_file_single_attempt(remote_filename, local_path, remote_dir, expected_size)
                if success:
                    return True
                elif attempt < retry_count:
                    self.logger.warning(f"⚠️ Download failed, retrying {remote_filename} (attempt {attempt + 1}/{retry_count})...")
                    continue
                else:
                    return False

            except (TimeoutError, ConnectionError) as e:
                if attempt < retry_count:
                    self.logger.warning(f"⚠️ Connection error for {remote_filename} (attempt {attempt + 1}), retrying: {e}")
                    continue
                else:
                    self.logger.error(f"❌ Final attempt failed for {remote_filename}: {e}")
                    return False
            except Exception as e:
                if attempt < retry_count:
                    self.logger.warning(f"⚠️ Unexpected error for {remote_filename} (attempt {attempt + 1}), retrying: {e}")
                    continue
                else:
                    self.logger.error(f"❌ Final attempt failed for {remote_filename}: {e}")
                    return False

        return False

    def download_files(self, files_dict: Dict[str, str], tmp_dir: Path,
                      clean_tmp: bool = True,
                      process_callback: Optional[callable] = None) -> List[str]:
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
                if validation['valid']:
                    file_size = local_file_path.stat().st_size
                    self.logger.info(f"📁 Local file already exists and is valid ({file_size:,} bytes): {filename}")
                    downloaded_files.append(str(local_file_path))
                    continue

            # Download the file with correct remote directory
            success = self.download_file(filename, local_file_path, remote_dir=remote_directory, expected_size=None)

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

        self.logger.info(f"Download complete: {len(downloaded_files)}/{total_files} files successful")
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
                    self.logger.info(f"Directory accessible: {test_dir} ({len(dir_files)} files)")
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
                "sample_files": file_list[:5] if file_list else []
            }

        except Exception as e:
            connection_time = time.time() - start_time
            self._last_connection_time = connection_time

            return {
                "success": False,
                "duration": connection_time,
                "error": str(e)
            }

    def close(self):
        """Close FTP connections."""
        # FTP connections are closed after each operation
        pass

    def __del__(self):
        """Clean up resources."""
        self.close()