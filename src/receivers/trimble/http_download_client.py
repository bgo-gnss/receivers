"""HTTP download client for Trimble NetR9 receivers.

This module provides HTTP-based file download functionality for NetR9 receivers,
replacing FTP-based downloads with the receiver's HTTP API endpoints.
"""

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, quote

from .http_client import TrimbleHTTPClient
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

        # Build complete progress line
        progress_line = (
            f"\r{self.filename}: {bar} "
            f"{progress*100:.1f}% "
            f"({current_mb:.1f}/{total_mb:.1f}MB) "
            f"@ {speed_kbps:.1f} KB/s{eta_str}"
        )

        # Write to stderr to avoid interfering with logging
        sys.stderr.write(progress_line)
        sys.stderr.flush()

    def finish(self) -> None:
        """Complete the progress bar and add newline."""
        self._display_progress()
        sys.stderr.write("\n")
        sys.stderr.flush()


class NetR9HTTPDownloader:
    """HTTP-based file downloader for NetR9 receivers.

    Uses the NetR9's HTTP API for directory listing and file downloads,
    implementing the same pattern as the legacy rek_scripts.
    """

    def __init__(self, station_id: str, station_config: Dict[str, Any]):
        """Initialize HTTP downloader with station configuration.

        Args:
            station_id: Station identifier
            station_config: Station configuration dictionary
        """
        self.station_id = station_id.upper()

        # Set up logging (matching PolaRX5 pattern)
        self.logger = self._get_logger()

        # Initialize HTTP client
        self.http_client = TrimbleHTTPClient(station_id, station_config)

        # Initialize file validator for resume capability
        self.file_validator = FileValidator(self.logger)

        # Get timeout settings from configuration
        from ..config.receivers_config import get_receivers_config
        receivers_config = get_receivers_config()
        netr9_config = receivers_config.get_receiver_config("netr9")
        self.connect_timeout = netr9_config.get("http_timeout_connect", 30)
        self.stall_timeout = netr9_config.get("http_stall_timeout", 120)

        # Track connection time for metrics
        self._last_connection_time = 0.0

        self.logger.info(f"Initialized NetR9 HTTP downloader for {self.station_id}")

    def _get_logger(self, level: int = logging.WARNING) -> logging.Logger:
        """Set up logger for this receiver instance."""
        logger_name = f"{__name__}.{self.station_id}"
        logger = logging.getLogger(logger_name)

        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(level)
            logger.propagate = False

        return logger

    def get_directory_listing(self, remote_path: str) -> List[Tuple[str, int]]:
        """Get directory listing from NetR9 receiver.

        Args:
            remote_path: Remote directory path (e.g., '/Internal/202509/15s_24h')

        Returns:
            List of (filename, filesize) tuples
        """
        endpoint = f"/prog/show?directory&path={quote(remote_path)}"

        self.logger.debug(f"Getting directory listing for {remote_path}")
        success, response, error = self.http_client.get_url(endpoint)

        if not success or not response:
            self.logger.error(f"Failed to get directory listing: {error}")
            return []

        # Parse the directory response (based on actual NetR9 response format)
        files = []
        try:
            for line in response.split('\n'):
                line = line.strip()
                if 'name=' in line and 'size=' in line and self.station_id in line:
                    # NetR9 format: name=SJUK202509010000a.T02 size=2982914  ctime=1440720000 attr=0000818
                    # Extract filename (after 'name=')
                    name_start = line.find('name=') + 5
                    name_end = line.find(' ', name_start)
                    filename = line[name_start:name_end]

                    # Extract size (after 'size=')
                    size_start = line.find('size=') + 5
                    size_end = line.find(' ', size_start)
                    filesize = int(line[size_start:size_end])

                    # Filter out .T0B files (as in rek_scripts)
                    if not filename.endswith('.T0B'):
                        files.append((filename, filesize))

        except (ValueError, IndexError) as e:
            self.logger.warning(f"Error parsing directory listing: {e}")

        self.logger.debug(f"Found {len(files)} files in {remote_path}")
        return files

    def download_file(self, remote_path: str, filename: str, local_path: Path,
                     expected_size: Optional[int] = None) -> bool:
        """Download a single file from NetR9 receiver.

        Args:
            remote_path: Remote directory path
            filename: Filename to download
            local_path: Local file path to save to
            expected_size: Expected file size for validation

        Returns:
            True if download successful, False otherwise
        """
        full_remote_path = f"{remote_path.rstrip('/')}/{filename}"
        # NetR9 uses direct download path format: /download/Internal/path/file.T02
        endpoint = f"/download{full_remote_path}"

        # Check if we should resume download
        should_resume, resume_offset = self.file_validator.should_resume_download(
            str(local_path), expected_size
        )

        # Ensure local_path is a Path object
        if isinstance(local_path, str):
            local_path = Path(local_path)

        if should_resume:
            self.logger.info(f"Resuming download from byte {resume_offset}: {filename}")
            # NetR9 HTTP API doesn't support range requests, so we can't resume
            # Remove partial file and start fresh
            try:
                local_path.unlink()
                self.logger.info(f"Removed partial file for fresh download: {filename}")
            except OSError as e:
                self.logger.warning(f"Could not remove partial file {local_path}: {e}")

        # Download the file
        start_time = time.time()
        self.logger.info(f"Downloading {filename} from {remote_path}")

        try:
            # Use simple requests.get() like syncdata script - proven to work
            import requests
            full_url = f"http://{self.http_client.ip}:{self.http_client.http_port}{endpoint}"
            self.logger.info(f"HTTP URL: {full_url}")
            self.logger.debug(f"Downloading from: {full_url}")

            # Use progress-based timeout: only timeout if no data received for stall_timeout seconds
            # Simple streaming download like syncdata with progress-based timeout
            response = requests.get(full_url, stream=True, timeout=(self.connect_timeout, None))  # No read timeout
            response.raise_for_status()

            # Write response to file
            local_path.parent.mkdir(parents=True, exist_ok=True)
            bytes_written = 0
            last_progress_time = time.time()

            # Initialize progress bar if we have expected size
            progress_bar = None
            if expected_size and expected_size > 0:
                progress_bar = ProgressBar(expected_size, filename)

            with open(local_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024):  # Same chunk size as syncdata
                    if chunk:
                        f.write(chunk)
                        f.flush()  # Flush like syncdata
                        bytes_written += len(chunk)
                        current_time = time.time()

                        # Reset progress timer when data is received
                        last_progress_time = current_time

                        # Update progress bar
                        if progress_bar:
                            progress_bar.update(bytes_written)

                    else:
                        # Check for stall timeout (no data received)
                        current_time = time.time()
                        if current_time - last_progress_time > self.stall_timeout:
                            # Clean up progress bar before raising error
                            if progress_bar:
                                progress_bar.finish()
                            raise TimeoutError(
                                f"Download stalled: no data received for {self.stall_timeout}s "
                                f"(downloaded {bytes_written:,} bytes)"
                            )

            # Complete progress bar
            if progress_bar:
                progress_bar.finish()

            # Validate downloaded file and log detailed size comparison (like PolaRX5)
            download_time = time.time() - start_time
            local_file_size = bytes_written  # Use actual bytes written

            if expected_size is not None:
                # Log file size comparison (matching PolaRX5 pattern)
                self.logger.info(f"Remote file size: {expected_size} bytes, Local file size: {local_file_size} bytes")
                size_diff = local_file_size - expected_size
                self.logger.info(f"Difference between remote and downloaded file: {size_diff} bytes")

                if size_diff == 0:
                    self.logger.info(f"✅ Successfully downloaded {filename} ({local_file_size:,} bytes)")

                    # Validate file integrity (matching PolaRX5 pattern)
                    validation = self.file_validator.validate_file(str(local_path), expected_size)
                    if not validation['valid']:
                        self.logger.warning(f"Downloaded file failed validation: {validation['error']}")
                        self.logger.info(f"Removing invalid downloaded file: {local_path}")
                        try:
                            local_path.unlink()
                            return False
                        except OSError as e:
                            self.logger.error(f"Could not remove invalid file {local_path}: {e}")
                            return False
                    else:
                        self.logger.debug(f"Downloaded file validated: {validation['compression']} compression, {validation['size']} bytes")

                    return True
                else:
                    self.logger.error(f"❌ Download incomplete for {filename}: size mismatch of {size_diff} bytes")
                    self.logger.error(f"   Expected: {expected_size:,} bytes, Got: {local_file_size:,} bytes")
                    self.logger.info(f"   Partial file kept for resume: {local_path}")
                    return False
            else:
                # No expected size - just validate what we can
                validation = self.file_validator.validate_file(str(local_path))
                if validation['valid']:
                    self.logger.info(f"✅ Downloaded {filename} ({local_file_size:,} bytes) - integrity validated")
                    return True
                else:
                    self.logger.error(f"❌ Download validation failed for {filename}: {validation['error']}")
                    try:
                        local_path.unlink()
                    except OSError:
                        pass
                    return False

        except TimeoutError as e:
            self.logger.error(f"Download stalled for {filename}: {e}")
            return False
        except requests.exceptions.RequestException as e:
            self.logger.error(f"HTTP error downloading {filename}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Error downloading {filename}: {e}")
            return False

    def download_files(self, files_dict: Dict[str, str], tmp_dir: Path,
                      clean_tmp: bool = True) -> List[str]:
        """Download multiple files from NetR9 receiver.

        Args:
            files_dict: Dictionary mapping filename -> remote_directory
            tmp_dir: Temporary download directory
            clean_tmp: Whether to clean temporary directory first

        Returns:
            List of successfully downloaded file paths
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

        # Log station connection details (matching PolaRX5 pattern)
        self.logger.info(f"Station connection: {self.http_client.ip}:{self.http_client.http_port}")

        # Track unique paths to log each only once (matching PolaRX5 pattern)
        logged_paths = set()

        # Log remote paths like PolaRX5 does
        for filename, remote_dir in sorted(files_dict.items(), reverse=True):
            # Log remote directory path only once per unique path
            if remote_dir not in logged_paths:
                self.logger.info(f"Remote path: {remote_dir}")
                logged_paths.add(remote_dir)

        for filename, remote_dir in sorted(files_dict.items(), reverse=True):
            # Individual file download logging (matching PolaRX5 pattern)
            self.logger.info(f"Downloading {filename}")

            # Get directory listing to find file size
            dir_files = self.get_directory_listing(remote_dir)
            file_info = next((f for f in dir_files if f[0] == filename), None)
            expected_size = file_info[1] if file_info else None

            local_file_path = tmp_dir / filename

            # Check if file already exists and is complete
            if local_file_path.exists() and expected_size:
                validation = self.file_validator.validate_file(str(local_file_path), expected_size)
                if validation['valid']:
                    self.logger.info(f"📁 Local file already exists and is valid ({expected_size:,} bytes): {filename}")
                    downloaded_files.append(str(local_file_path))
                    continue

            # Download the file
            success = self.download_file(remote_dir, filename, local_file_path, expected_size)

            if success:
                downloaded_files.append(str(local_file_path))
            else:
                self.logger.error(f"❌ Failed to download {filename}")

        self.logger.info(f"Download complete: {len(downloaded_files)}/{total_files} files successful")
        return downloaded_files

    def test_connection(self) -> Dict[str, Any]:
        """Test HTTP connection to NetR9 receiver.

        Returns:
            Dictionary with connection test results
        """
        self.logger.info("Testing HTTP connection...")
        start_time = time.time()

        # Test basic connectivity
        basic_test = self.http_client.test_connection()

        if basic_test["success"]:
            # Test specific NetR9 endpoints
            endpoints_to_test = [
                "/prog/show?temperature",
                "/prog/show?voltages",
                "/prog/show?directory&path=/Internal/"
            ]

            endpoint_results = {}
            for endpoint in endpoints_to_test:
                success, response, error = self.http_client.get_url(endpoint)
                endpoint_results[endpoint] = {
                    "success": success,
                    "error": error,
                    "response_size": len(response) if response else 0
                }

        connection_time = time.time() - start_time
        self._last_connection_time = connection_time

        result = {
            "success": basic_test["success"],
            "duration": connection_time,
            "basic_test": basic_test,
            "error": basic_test.get("error")
        }

        if basic_test["success"]:
            result["endpoint_tests"] = endpoint_results

        return result

    def close(self):
        """Close HTTP connections."""
        if hasattr(self, 'http_client'):
            self.http_client.close()

    def __del__(self):
        """Clean up resources."""
        self.close()