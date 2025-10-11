"""Leica G10 GNSS receiver implementation.

This module provides support for Leica G10 receivers via FTP downloads of compressed
.m00.zip files from the receiver's SD Card storage path.

Based on the legacy getLeica script pattern:
- FTP anonymous login to port 2160
- Files stored at: /SD Card/Data/15s_24hr/
- Remote format: {STATION}{DOY}a.m00.zip
- Archive format: {STATION}YYYYMMDDHHMM0000a.m00.gz
"""

import gzip
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from ..base.receiver import BaseReceiver
from ..base.exceptions import ConfigurationError, ConnectionError, DownloadError
from ..utils.performance_recorder import (
    create_performance_metrics,
    record_performance_metrics
)
from ..utils.session_parser import parse_session_parameters
from .leica_ftp_download_client import LeicaFTPDownloader

# Phase 1 utilities (feature-flagged)
from ..utils.archive_validator import ArchiveValidator
from ..utils.time_processor import TimeParameterProcessor
from ..utils.file_archiver import FileArchiver, ArchiveMode


class LeicaG10(BaseReceiver):
    """Leica G10 GNSS receiver implementation.

    Provides FTP-based file downloads from Leica G10 receivers with:
    - Anonymous FTP access to SD Card storage
    - Automatic unzipping of compressed .m00.zip files
    - Progress tracking and error handling
    - Archive management with compression
    """

    def __init__(self, station_id: str, station_info: Dict[str, Any], loglevel: int = logging.INFO):
        """Initialize Leica G10 receiver.

        Args:
            station_id: Station identifier
            station_info: Station configuration from gps_parser
            loglevel: Logging level
        """
        super().__init__(station_id, station_info)

        self.loglevel = loglevel
        self.logger = self._get_logger(loglevel)
        self.logger.info(f"Initialized Leica G10 receiver for {station_id}")

        # Set up directories
        self.tmp_dir = self.receivers_config.get_tmp_dir()

        # Get G10-specific configuration
        self.leica_config = self.receivers_config.get_receiver_config("g10")

        # Validate station configuration
        self._validate_station_config()

        # Initialize FTP downloader with same log level
        self.ftp_downloader = LeicaFTPDownloader(station_id, station_info, loglevel)

        # Connection status
        self.connection_status = {"router": False, "receiver": False}

        # Phase 1 utilities (always enabled - Phase 3B)
        self.archive_validator = ArchiveValidator(logger=self.logger)
        self.time_processor = TimeParameterProcessor(logger=self.logger)

    def _get_logger(self, level: int = logging.INFO) -> logging.Logger:
        """Set up logger for this receiver instance."""
        logger_name = f"{__name__}.{self.station_id}"
        logger = logging.getLogger(logger_name)
        logger.setLevel(level)
        # Use parent logger's configuration for consistent formatting
        return logger

    def _validate_station_config(self):
        """Validate station configuration for Leica G10."""
        try:
            # Check for required configuration keys
            # Handle both new format and legacy gps_parser format
            if "router" in self.station_info and "receiver" in self.station_info:
                # New format - already validated in parent class
                pass
            elif "station" in self.station_info:
                # Legacy gps_parser format - convert to expected format
                station_config = self.station_info["station"]
                router_ip = station_config.get("router_ip")
                if not router_ip:
                    raise ConfigurationError(f"Missing router_ip for station {self.station_id}")

                # Leica uses FTP port from config
                ftp_port = self.leica_config.get("ftp_port", 2160)

                # Create expected structure
                self.station_info["router"] = {"ip": router_ip}
                self.station_info["receiver"] = {
                    "ftpport": int(ftp_port)
                }
            else:
                raise ConfigurationError(f"Invalid station configuration structure for {self.station_id}")

        except (KeyError, ValueError) as e:
            raise ConfigurationError(
                f"Invalid station configuration for {self.station_id}: {e}"
            )

    def get_connection_status(self) -> Dict[str, Any]:
        """Check connection status to Leica G10 receiver.

        Returns:
            Dictionary with connection status information
        """
        try:
            self.logger.debug(f"Testing connection to {self.station_id}")

            # Test FTP connection
            ftp_test = self.ftp_downloader.test_connection()

            # Update internal connection status
            self.connection_status = {
                "router": ftp_test["success"],
                "receiver": ftp_test["success"],
            }

            return {
                "station_id": self.station_id,
                "ip": self.station_info["router"]["ip"],
                "port": self.station_info["receiver"]["ftpport"],
                "router": ftp_test["success"],
                "receiver": ftp_test["success"],
                "ftp_test": ftp_test,
                "error": ftp_test.get("error"),
            }

        except Exception as e:
            error_msg = f"Connection test failed: {e}"
            self.logger.error(error_msg)

            self.connection_status = {"router": False, "receiver": False}

            return {
                "station_id": self.station_id,
                "router": False,
                "receiver": False,
                "error": error_msg,
            }

    def download_data(
        self,
        start: Union[datetime, str],
        end: Union[datetime, str],
        session: str = "15s_24hr",
        sync: bool = True,
        clean_tmp: bool = True,
        archive: bool = True,
        **kwargs,
    ) -> Dict[str, Any]:
        """Download data from Leica G10 receiver for specified time period.

        Args:
            start: Start time for data download
            end: End time for data download
            session: Data session type (only '15s_24hr' supported by Leica G10)
            sync: Whether to sync missing files
            clean_tmp: Whether to clean temporary download directory
            archive: Whether to archive downloaded files
            **kwargs: Additional receiver-specific parameters (including loglevel)

        Returns:
            Dictionary with download results and file information
        """
        start_time = time.time()

        # Check if loglevel was passed and create new FTP downloader with correct level
        loglevel = kwargs.get('loglevel', self.loglevel)
        if loglevel != self.loglevel:
            self.logger.debug(f"Updating log level from {self.loglevel} to {loglevel}")
            self.ftp_downloader = LeicaFTPDownloader(self.station_id, self.station_info, loglevel)

        try:
            self.logger.info(f"Starting download for Leica G10 {self.station_id}")

            # Parse start/end times
            if isinstance(start, str):
                start = datetime.fromisoformat(start.replace('Z', '+00:00'))
            if isinstance(end, str):
                end = datetime.fromisoformat(end.replace('Z', '+00:00'))

            # Use the same time range processing as other receivers
            self.logger.info(f"Checking {session} sessions from {start} to {end}")

            # Set up directories
            tmp_dir_path = Path(self.tmp_dir) / self.station_id
            tmp_dir_path.mkdir(parents=True, exist_ok=True)

            # Generate file list based on session type and time range
            files_dict, archive_files_dict = self._generate_file_list(
                start, end, session, **kwargs
            )

            # Log file generation info
            self.logger.info(f"Generated {len(files_dict)} timestamps")

            if not files_dict:
                self.logger.info("No files to check for this time range")
                return {
                    "station_id": self.station_id,
                    "receiver_type": "G10",
                    "status": "no_files",
                    "files_downloaded": 0,
                    "downloaded_files": [],
                    "duration": time.time() - start_time,
                }

            # Filter out files that already exist in archive
            missing_files_dict = {}
            validated_files = 0
            files_found_in_archive = 0

            for filename, remote_dir in files_dict.items():
                validated_files += 1
                archive_path = archive_files_dict.get(filename)
                if archive_path:
                    # Check if file already exists in archive (raw or compressed)
                    archive_path_obj = Path(archive_path)
                    if archive_path_obj.exists():
                        if self._validate_archived_file(archive_path_obj):
                            self.logger.debug(
                                f"Archive file exists: {archive_path_obj.name} ({archive_path_obj.stat().st_size} bytes)"
                            )
                            files_found_in_archive += 1
                            continue
                        else:
                            self.logger.warning(
                                f"Archived file failed sanity check, will re-download: {archive_path_obj}"
                            )
                            missing_files_dict[filename] = remote_dir
                            continue

                    # Check if compressed version exists (.m00.gz)
                    if not str(archive_path).endswith(".gz"):
                        archive_path_gz = archive_path + ".gz"
                        archive_path_gz_obj = Path(archive_path_gz)
                        if archive_path_gz_obj.exists():
                            if self._validate_archived_file(archive_path_gz_obj):
                                self.logger.debug(
                                    f"Archive file exists with compression: {archive_path_gz_obj.name} ({archive_path_gz_obj.stat().st_size} bytes)"
                                )
                                files_found_in_archive += 1
                                continue
                            else:
                                self.logger.warning(
                                    f"Compressed archived file failed sanity check, will re-download: {archive_path_gz}"
                                )
                                missing_files_dict[filename] = remote_dir
                                continue

                # Check if file exists in temporary directory first (as zip or uncompressed)
                tmp_zip_path = tmp_dir_path / filename  # .zip file
                tmp_m00_path = tmp_dir_path / filename.replace('.m00.zip', '.m00')  # .m00 file

                if tmp_zip_path.exists() and self._validate_archived_file(tmp_zip_path):
                    self.logger.debug(f"Zip file already exists in temp directory: {filename}")
                    files_found_in_archive += 1  # Count as found even though it's in temp
                    continue
                elif tmp_m00_path.exists() and self._validate_archived_file(tmp_m00_path):
                    self.logger.debug(f"Uncompressed file already exists in temp directory: {tmp_m00_path.name}")
                    files_found_in_archive += 1  # Count as found even though it's in temp
                    continue

                # File is missing from both archive and temp, add to download list
                missing_files_dict[filename] = remote_dir

            # Log validation results
            self.logger.info(f"Validated {validated_files} files total")
            if files_found_in_archive > 0:
                self.logger.info(
                    f"Found {files_found_in_archive} files already archived, skipping re-download"
                )

            if not missing_files_dict:
                self.logger.info("Archive is up to date")
                return {
                    "station_id": self.station_id,
                    "receiver_type": "G10",
                    "status": "up_to_date",
                    "files_checked": len(files_dict),
                    "files_missing": 0,
                    "files_downloaded": 0,
                    "duration": time.time() - start_time,
                }

            self.logger.info(f"Missing files: {len(missing_files_dict)} (out of {len(files_dict)} total)")

            # Download files if sync is enabled
            downloaded_files = []
            if sync:
                if missing_files_dict:
                    # Create callback for immediate unzip+archive when archiving enabled
                    process_callback = None
                    if archive:
                        def immediate_process_callback(zip_path: str) -> Optional[str]:
                            """Unzip and archive immediately after download."""
                            # Unzip the file
                            unzipped = self._unzip_single_file(zip_path)
                            if not unzipped:
                                return None

                            # Archive the unzipped .m00 file
                            m00_filename = Path(unzipped).name
                            if m00_filename in archive_files_dict:
                                archive_path = Path(archive_files_dict[m00_filename])

                                from ..utils.file_archiver import FileArchiver, ArchiveMode
                                with FileArchiver(mode=ArchiveMode.IMMEDIATE, logger=self.logger) as archiver:
                                    success = archiver.archive_file(
                                        Path(unzipped),
                                        archive_path,
                                        compress=True,
                                        remove_tmp=True
                                    )

                                if success:
                                    return str(archive_path)

                            return unzipped

                        process_callback = immediate_process_callback

                    downloaded_files = self.ftp_downloader.download_files(
                        missing_files_dict, tmp_dir_path, clean_tmp, process_callback
                    )
                else:
                    self.logger.info("Archive is up to date - no files to download")
            else:
                self.logger.info("Sync disabled - skipping actual download")

            # Files are processed (unzipped+archived) inline by callback when archive=True
            # downloaded_files contains archive paths when archiving is enabled
            processed_files = downloaded_files
            archived_files = []
            if archive and processed_files:
                # After archiving, create list of archived file paths
                for file_path in processed_files:
                    file_path_obj = Path(file_path)
                    filename = file_path_obj.name  # SKFC266a.m00
                    if filename in archive_files_dict:
                        archived_files.append(archive_files_dict[filename])

                # Use archived files for reporting if archiving was successful
                final_files = archived_files if archived_files else processed_files
            else:
                final_files = processed_files

            # Calculate bytes from final file locations
            final_bytes = 0
            for f in final_files:
                try:
                    if Path(f).exists():
                        final_bytes += Path(f).stat().st_size
                except (OSError, IOError):
                    pass

            # Record performance metrics
            duration = time.time() - start_time
            performance_metrics = create_performance_metrics(
                success=len(final_files) > 0 if sync else True,
                duration=duration,
                bytes_downloaded=final_bytes,
                connection_time=getattr(
                    self.ftp_downloader, "_last_connection_time", 0.0
                ),
            )
            record_performance_metrics(
                self.station_id, performance_metrics, self.logger
            )

            return {
                "station_id": self.station_id,
                "receiver_type": "G10",
                "status": "completed",
                "files_downloaded": len(final_files),
                "downloaded_files": final_files,
                "duration": duration,
                "start_time": start,
                "end_time": end,
                "session": session,
            }

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"Download failed: {e}"
            self.logger.error(error_msg)

            return {
                "station_id": self.station_id,
                "receiver_type": "G10",
                "status": "error",
                "files_downloaded": 0,
                "downloaded_files": [],
                "error": error_msg,
                "duration": duration,
            }


    def _validate_archived_file(self, file_path: Path) -> bool:
        """Basic sanity checks for archived files.

        Args:
            file_path: Path to archived file

        Returns:
            True if file passes basic sanity checks, False otherwise
        """
        try:
            # Check 1: File must not be zero or tiny (less than 1KB is suspicious)
            file_size = file_path.stat().st_size
            if file_size < 1024:  # 1KB minimum
                self.logger.debug(f"File too small ({file_size} bytes): {file_path}")
                return False

            # Check 2: If it's a .gz file, verify it has gzip magic header
            if str(file_path).endswith(".gz"):
                with open(file_path, "rb") as f:
                    # Read first 2 bytes for gzip magic number
                    magic = f.read(2)
                    if magic != b"\x1f\x8b":  # gzip magic bytes
                        self.logger.debug(
                            f"File doesn't have gzip magic header: {file_path}"
                        )
                        return False

            # Basic checks passed
            return True
        except (OSError, IOError) as e:
            self.logger.debug(f"Error validating archived file {file_path}: {e}")
            return False

    def _generate_file_list(
        self, start: datetime, end: datetime, session: str, **kwargs
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Generate file lists for Leica G10 downloads.

        Args:
            start: Start time
            end: End time
            session: Session type (only 15s_24hr supported)
            **kwargs: Additional parameters

        Returns:
            Tuple of (files_dict, archive_files_dict)
            files_dict maps filename -> remote_directory (not used for Leica)
            archive_files_dict maps filename -> archive_path
        """
        # Parse session parameters
        afrequency, ffrequency, gt_frequency = parse_session_parameters(session)

        # Get session mapping from configuration
        # Normalize session name to lowercase for config lookup (1Hz_1hr -> 1hz_1hr)
        session_key = session.lower()
        session_mapping = self.leica_config.get(f"session_map_{session_key}")
        if not session_mapping:
            raise ValueError(f"Unsupported session type for Leica G10: {session} (looked for session_map_{session_key})")

        session_letter, remote_directory = session_mapping.split(",", 1)
        remote_directory = remote_directory.strip()
        session_letter = session_letter.strip()

        self.logger.info(f"Using session mapping: {session} -> letter={session_letter}, directory={remote_directory}")

        # Generate datetime list using unified build_path method (same as other receivers)
        file_datetime_list = self.build_path(None, "#datelist", session, gt_frequency, start, end)

        # Create remote template using unified approach for Leica filename convention
        # Different templates based on session type:
        # - Daily files (15s_24hr): Use session letter (a)
        # - Hourly files (1Hz_1hr): Use #hourl for hour letters (a,b,c...x)
        file_extension = self.get_file_extension()  # .m00
        base_path = self.leica_config.get('base_path', '/SD Card/Data/')

        if session == "15s_24hr":
            # Daily files: directly under /SD Card/Data/15s_24hr/ (no year/month/day subdirectories)
            remote_template = f"{base_path}{remote_directory}/{self.station_id}%j{session_letter}{file_extension}.zip"
        else:
            # Hourly files: nested structure /SD Card/Data/1s_1hr/STATION/year/month/day/
            remote_template = f"{base_path}{remote_directory}/{self.station_id}/%Y/%m/%d/{self.station_id}%j#hourl{file_extension}.zip"

        # Generate remote full paths using unified method with gtimes frequency for #Rin2 expansion
        remote_full_paths = self.build_path(file_datetime_list, remote_template, session, gt_frequency)

        # Extract remote directories and filenames for files_dict
        files_dict = {}
        for full_path in remote_full_paths:
            remote_dir = os.path.dirname(full_path) + "/"  # Add trailing slash for FTP
            filename = os.path.basename(full_path)
            files_dict[filename] = remote_dir

        # Generate archive paths using unified approach
        archive_template = self.receivers_config.get_archive_template()

        # Leica files are archived as .m00.gz (compressed)
        raw_extension = self.get_file_extension()  # .m00
        archived_extension = raw_extension + ".gz"  # .m00.gz

        full_archive_template = archive_template.format(
            prepath=self.data_prepath,
            station="{station}",
            session="{session}",
            extension=archived_extension,
            session_letter="{session_letter}",
        )

        # Adjust timestamps to match Leica G10 file creation times (same pattern as NetR9/NetRS)
        # Daily files (15s_24hr): created at midnight (00:00)
        # Hourly files (1Hz_1hr): created at hour boundaries (01:00, 02:00, etc.)
        adjusted_datetime_list = []
        for dt in file_datetime_list:
            if ffrequency == "24hr":
                # Daily files always created at midnight
                adjusted_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                # Hourly files created at hour boundaries
                adjusted_dt = dt.replace(minute=0, second=0, microsecond=0)
            adjusted_datetime_list.append(adjusted_dt)

        archive_file_list = self.build_path(
            adjusted_datetime_list, full_archive_template, session, gt_frequency
        )

        # Map filenames to archive paths
        # For Leica, we need to map both the .zip filename and the final .m00 filename
        archive_files_dict = {}
        filenames = list(files_dict.keys())
        for i, filename in enumerate(filenames):
            if i < len(archive_file_list):
                # Map the zip filename: SKFC268b.m00.zip -> archive path
                archive_files_dict[filename] = archive_file_list[i]
                # Also map the unzipped filename: SKFC268b.m00 -> archive path
                unzipped_filename = filename.replace('.zip', '')
                archive_files_dict[unzipped_filename] = archive_file_list[i]

        return files_dict, archive_files_dict

    def _unzip_single_file(self, zip_file_path: str) -> Optional[str]:
        """Unzip a single .zip file and return the path to the unzipped file.

        Args:
            zip_file_path: Path to the .zip file

        Returns:
            Path to unzipped .m00 file, or None if unzipping failed
        """
        zip_path = Path(zip_file_path)
        if not zip_path.exists():
            self.logger.warning(f"File not found: {zip_file_path}")
            return None

        if not str(zip_path).endswith('.zip'):
            self.logger.warning(f"File is not a zip file: {zip_file_path}")
            return zip_file_path  # Return as-is

        # Unzip the file
        try:
            self.logger.info(f"📦 Unzipping: {zip_path.name}")

            # Run unzip command in the same directory
            result = subprocess.run(
                ['unzip', '-o', str(zip_path)],
                cwd=zip_path.parent,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                # Find the unzipped .m00 file
                base_name = zip_path.stem  # Remove .zip extension
                m00_file = zip_path.parent / base_name

                if m00_file.exists():
                    self.logger.info(f"✅ Unzipped: {base_name}")

                    # Remove the zip file to save space
                    zip_path.unlink()
                    self.logger.debug(f"🧹 Removed zip file: {zip_path.name}")

                    return str(m00_file)
                else:
                    self.logger.error(f"❌ Unzipped file not found: {base_name}")
                    return None
            else:
                self.logger.error(f"❌ Unzip failed for {zip_path.name}: {result.stderr}")
                return None

        except subprocess.TimeoutExpired:
            self.logger.error(f"❌ Unzip timeout for {zip_path.name}")
            return None
        except Exception as e:
            self.logger.error(f"❌ Unzip error for {zip_path.name}: {e}")
            return None

    def _process_zip_files(self, downloaded_files: List[str]) -> List[str]:
        """Process downloaded .zip files by unzipping them.

        Args:
            downloaded_files: List of downloaded .zip file paths

        Returns:
            List of unzipped file paths
        """
        processed_files = []

        for zip_file_path in downloaded_files:
            zip_path = Path(zip_file_path)
            if not zip_path.exists():
                self.logger.warning(f"Downloaded file not found: {zip_file_path}")
                continue

            if not str(zip_path).endswith('.zip'):
                self.logger.warning(f"File is not a zip file: {zip_file_path}")
                processed_files.append(zip_file_path)  # Add as-is
                continue

            # Unzip the file
            try:
                self.logger.info(f"📦 Unzipping: {zip_path.name}")

                # Run unzip command in the same directory
                result = subprocess.run(
                    ['unzip', '-o', str(zip_path)],
                    cwd=zip_path.parent,
                    capture_output=True,
                    text=True,
                    timeout=60
                )

                if result.returncode == 0:
                    # Find the unzipped .m00 file
                    base_name = zip_path.stem  # Remove .zip extension
                    m00_file = zip_path.parent / base_name

                    if m00_file.exists():
                        self.logger.info(f"✅ Unzipped: {base_name}")
                        processed_files.append(str(m00_file))

                        # Remove the zip file to save space
                        zip_path.unlink()
                        self.logger.debug(f"🧹 Removed zip file: {zip_path.name}")
                    else:
                        self.logger.error(f"❌ Unzipped file not found: {base_name}")
                else:
                    self.logger.error(f"❌ Unzip failed for {zip_path.name}: {result.stderr}")

            except subprocess.TimeoutExpired:
                self.logger.error(f"❌ Unzip timeout for {zip_path.name}")
            except Exception as e:
                self.logger.error(f"❌ Unzip error for {zip_path.name}: {e}")

        return processed_files

    def _archive_files(
        self, downloaded_files: List[str], archive_files_dict: Dict[str, str]
    ):
        """Archive downloaded files to final locations with compression.

        Args:
            downloaded_files: List of downloaded file paths (.m00 files)
            archive_files_dict: Dictionary mapping filename to archive path
        """
        self.logger.debug("Using Phase 1 FileArchiver (IMMEDIATE mode)")
        archived_count = 0

        for file_path in downloaded_files:
            try:
                file_path_obj = Path(file_path)
                filename = file_path_obj.name

                if not file_path_obj.exists():
                    self.logger.warning(
                        f"Cannot archive - file not found: {file_path}"
                    )
                    continue

                if filename in archive_files_dict:
                    archive_path = Path(archive_files_dict[filename])

                    # Archive file immediately (one at a time for fault tolerance)
                    with FileArchiver(mode=ArchiveMode.IMMEDIATE, logger=self.logger) as archiver:
                        success = archiver.archive_file(
                            file_path_obj,
                            archive_path,
                            compress=True,
                            remove_tmp=True,
                        )

                    if success:
                        archived_count += 1
                    else:
                        self.logger.error(f"❌ Failed to archive {filename}")

            except Exception as e:
                self.logger.error(
                    f"❌ Failed to archive {filename}: {e}"
                )

        self.logger.info(
            f"Archiving complete: {archived_count}/{len(downloaded_files)} files archived"
        )
        return archived_count

    def get_file_extension(self) -> str:
        """Get file extension for Leica G10 files.

        Returns:
            File extension (.m00)
        """
        return self.leica_config.get("file_extension", ".m00")

    def get_session_letter(self, session: str) -> str:
        """Get session letter for Leica G10 session type.

        Args:
            session: Session type (e.g., '15s_24hr')

        Returns:
            Session letter (e.g., 'a' for 15s_24hr, 'b' for 1Hz_1hr)
        """
        # Normalize session name to lowercase for config lookup (1Hz_1hr -> 1hz_1hr)
        session_key = session.lower()
        session_map = self.leica_config.get(f"session_map_{session_key}", "a,15s_24hr")
        session_letter, _ = session_map.split(",", 1)
        return session_letter

    def get_station_info(self) -> Dict[str, Any]:
        """Get station information for this receiver.

        Returns:
            Station information dictionary
        """
        return self.station_info

    def get_health_status(self) -> Dict[str, Any]:
        """Get health status from Leica G10 receiver.

        Note: Leica G10 has limited health monitoring capabilities via FTP.

        Returns:
            Dictionary with basic health information
        """
        health_data = {}

        try:
            self.logger.debug(f"Collecting health data from {self.station_id}")

            # Check connection first
            connection_status = self.get_connection_status()
            if not connection_status["receiver"]:
                return {
                    "station_id": self.station_id,
                    "receiver_type": "G10",
                    "timestamp": datetime.now(),
                    "overall_status": "offline",
                    "error": connection_status.get("error", "Receiver not accessible"),
                }

            # Basic FTP connection health
            ftp_test = connection_status.get("ftp_test", {})
            health_data["connection"] = {
                "status": "online" if ftp_test.get("success") else "offline",
                "response_time": ftp_test.get("duration", 0),
                "directory_accessible": ftp_test.get("directory_accessible", False),
                "files_found": ftp_test.get("files_found", 0),
            }

            # Create standardized health report
            overall_status = "online" if ftp_test.get("success") else "degraded"

            return {
                "station_id": self.station_id,
                "receiver_type": "G10",
                "timestamp": datetime.now(),
                "overall_status": overall_status,
                "connection": health_data["connection"],
                "note": "Leica G10 health monitoring is limited to FTP connectivity",
            }

        except Exception as e:
            error_msg = f"Health data collection failed: {e}"
            self.logger.error(error_msg)

            return {
                "station_id": self.station_id,
                "receiver_type": "G10",
                "timestamp": datetime.now(),
                "overall_status": "error",
                "error": error_msg,
            }

    def close(self):
        """Close connections to receiver."""
        if hasattr(self, 'ftp_downloader'):
            self.ftp_downloader.close()

    def __del__(self):
        """Clean up resources."""
        self.close()