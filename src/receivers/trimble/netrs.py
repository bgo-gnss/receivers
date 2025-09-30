"""Trimble NetRS receiver implementation.

Modern implementation of Trimble NetRS receiver support with HTTP-based downloads.
NetRS receivers use HTTP API similar to NetR9 but with different URL patterns and file formats.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

import gtimes.timefunc as gt

from ..base.receiver import BaseReceiver
from ..base.exceptions import ConnectionError, ConfigurationError
from ..utils.session_parser import parse_session_parameters
from ..utils.performance_recorder import record_performance_metrics, create_performance_metrics
from .http_client import TrimbleHTTPClient
from .health_parser import TrimbleHealthParser
from .netrs_http_download_client import NetRSHTTPDownloader

# Phase 1 utilities (feature-flagged)
from ..utils.archive_validator import ArchiveValidator
from ..utils.time_processor import TimeParameterProcessor
from ..utils.file_archiver import FileArchiver, ArchiveMode


class NetRS(BaseReceiver):
    """Trimble NetRS receiver implementation.

    Provides HTTP-based health monitoring and HTTP-based data download
    for Trimble NetRS GNSS receivers.
    """

    def __init__(self, station_id: str, station_info: Dict[str, Any]):
        """Initialize NetRS receiver.

        Args:
            station_id: Station identifier
            station_info: Station configuration dictionary
        """
        super().__init__(station_id, station_info)

        # Set up logging
        self.logger = self._get_logger()

        # Validate required configuration
        self._validate_config()

        # Get NetRS-specific configuration
        self.netrs_config = self.receivers_config.get_receiver_config("netrs")

        # Initialize HTTP client for health monitoring
        self.http_client = TrimbleHTTPClient(station_id, station_info)

        # Initialize HTTP downloader for data downloads
        self.http_downloader = NetRSHTTPDownloader(station_id, station_info)

        # Initialize health parser
        self.health_parser = TrimbleHealthParser(station_id, "NetRS")

        # data_prepath is now handled by BaseReceiver via ConfigManager
        self.tmp_dir = "/home/bgo/tmp/download/"

        # Phase 1 utilities (always enabled - Phase 3B)
        self.archive_validator = ArchiveValidator(logger=self.logger)
        self.time_processor = TimeParameterProcessor(logger=self.logger)
        # FileArchiver will be created per-download with appropriate mode

        # NetRS HTTP API endpoints (similar to NetR9)
        self.endpoints = {
            "voltage": "/prog/show?Voltages",
            "temperature": "/prog/show?Temperature",
            "sessions": "/prog/show?sessions",
            "position": "/prog/show?position",
            "tracking": "/prog/show?trackingstatus",
            "firmware": "/prog/show?firmwareversion",
            "directory": "/prog/show?directory&path=/{path}",
        }

        self.logger.info(f"Initialized NetRS receiver for {self.station_id}")

    def _get_logger(self, level: int = logging.INFO) -> logging.Logger:
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

    def _validate_config(self):
        """Validate required configuration parameters."""
        try:
            # Handle both new format (router/receiver keys) and legacy gps_parser format
            if "router" in self.station_info and "receiver" in self.station_info:
                # New format with separate router and receiver sections
                router_config = self.station_info["router"]
                receiver_config = self.station_info["receiver"]

                if not router_config.get("ip"):
                    raise ConfigurationError(
                        f"Missing router IP for station {self.station_id}"
                    )

                # Check for HTTP port
                http_port = receiver_config.get("httpport")
                if not http_port:
                    self.logger.warning(
                        f"Missing HTTP port for {self.station_id}, using default 8060"
                    )
                    receiver_config["httpport"] = 8060
                else:
                    receiver_config["httpport"] = int(http_port)

                if not receiver_config.get("ftpport"):
                    self.logger.warning(
                        f"Missing FTP port for {self.station_id}, using default 21"
                    )
                    receiver_config["ftpport"] = 21

            elif "station" in self.station_info:
                # Legacy gps_parser format - convert to expected format
                station_config = self.station_info["station"]

                # Extract router IP
                router_ip = station_config.get("router_ip")
                if not router_ip:
                    raise ConfigurationError(
                        f"Missing router_ip for station {self.station_id}"
                    )

                # Create expected structure
                self.station_info["router"] = {"ip": router_ip}

                # Extract receiver config
                http_port = station_config.get("receiver_httpport", "8060")
                ftp_port = station_config.get("receiver_ftpport", "21")

                self.station_info["receiver"] = {
                    "httpport": int(http_port),
                    "ftpport": int(ftp_port)
                }

                self.logger.debug(f"Converted legacy config for {self.station_id}")

            else:
                raise ConfigurationError(
                    f"Invalid station configuration structure for {self.station_id}"
                )

        except (KeyError, ValueError) as e:
            raise ConfigurationError(
                f"Invalid station configuration for {self.station_id}: {e}"
            )

    def get_connection_status(self) -> Dict[str, Any]:
        """Check connection status to NetRS receiver.

        Returns:
            Dictionary with connection status information
        """
        try:
            self.logger.debug(f"Testing connection to {self.station_id}")

            # Test HTTP connection
            http_test = self.http_client.test_connection()

            # Test HTTP download connection
            download_test = self.http_downloader.test_connection()

            # Update internal connection status
            self.connection_status = {
                "router": http_test["success"],
                "receiver": http_test["success"],
            }

            return {
                "station_id": self.station_id,
                "ip": self.station_info["router"]["ip"],
                "port": self.station_info["receiver"]["httpport"],
                "router": http_test["success"],
                "receiver": http_test["success"],
                "http_test": http_test,
                "download_test": download_test,
                "error": http_test.get("error"),
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

    def get_health_status(self) -> Dict[str, Any]:
        """Get comprehensive health status from NetRS receiver.

        Returns:
            Dictionary with health metrics and status information
        """
        health_data = {}

        try:
            self.logger.debug(f"Collecting health data from {self.station_id}")

            # Check connection first
            connection_status = self.get_connection_status()
            if not connection_status["receiver"]:
                return {
                    "station_id": self.station_id,
                    "receiver_type": "NetRS",
                    "timestamp": datetime.now(),
                    "overall_status": "offline",
                    "error": connection_status.get("error", "Receiver not accessible"),
                }

            # Collect voltage information
            try:
                success, response, error = self.http_client.get_url(
                    self.endpoints["voltage"]
                )
                if success and response:
                    health_data["voltage"] = self.health_parser.parse_voltage_response(
                        response
                    )
                else:
                    health_data["voltage"] = {
                        "status": "error",
                        "error": error or "No response",
                    }
            except Exception as e:
                health_data["voltage"] = {"status": "error", "error": str(e)}

            # Collect temperature information
            try:
                success, response, error = self.http_client.get_url(
                    self.endpoints["temperature"]
                )
                if success and response:
                    health_data["temperature"] = (
                        self.health_parser.parse_temperature_response(response)
                    )
                else:
                    health_data["temperature"] = {
                        "status": "error",
                        "error": error or "No response",
                    }
            except Exception as e:
                health_data["temperature"] = {"status": "error", "error": str(e)}

            # Collect session information
            try:
                success, response, error = self.http_client.get_url(
                    self.endpoints["sessions"]
                )
                if success and response:
                    health_data["sessions"] = (
                        self.health_parser.parse_sessions_response(response)
                    )
                else:
                    health_data["sessions"] = {
                        "status": "error",
                        "error": error or "No response",
                    }
            except Exception as e:
                health_data["sessions"] = {"status": "error", "error": str(e)}

            # Collect tracking information
            try:
                success, response, error = self.http_client.get_url(
                    self.endpoints["tracking"]
                )
                if success and response:
                    health_data["tracking"] = (
                        self.health_parser.parse_tracking_response(response)
                    )
                else:
                    health_data["tracking"] = {
                        "status": "error",
                        "error": error or "No response",
                    }
            except Exception as e:
                health_data["tracking"] = {"status": "error", "error": str(e)}

            # Create standardized health report
            return self.health_parser.create_standard_health_report(health_data)

        except Exception as e:
            error_msg = f"Health data collection failed: {e}"
            self.logger.error(error_msg)

            return {
                "station_id": self.station_id,
                "receiver_type": "NetRS",
                "timestamp": datetime.now(),
                "overall_status": "error",
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
        """Download data from NetRS receiver for specified time period.

        Args:
            start: Start time for data download
            end: End time for data download
            session: Data session type (e.g., '15s_24hr', '1Hz_1hr')
            sync: Whether to sync missing files
            clean_tmp: Whether to clean temporary download directory
            archive: Whether to archive downloaded files
            **kwargs: Additional receiver-specific parameters

        Returns:
            Dictionary with download results and file information
        """
        # Extract legacy parameters from kwargs for backward compatibility
        loglevel = kwargs.get(
            "loglevel", logging.INFO
        )  # Default to INFO for detailed logging

        # Set logger level
        self.logger.setLevel(loglevel)

        start_time = time.time()

        try:
            self.logger.info(f"Starting download for NetRS {self.station_id}")

            # Process time parameters
            start, end = self._process_time_parameters(start, end, session)

            # Log session info (matching NetR9 pattern)
            self.logger.info(f"Checking {session} sessions from {start} to {end}")

            # Set up directories
            tmp_dir_path = Path(self.tmp_dir) / self.station_id
            tmp_dir_path.mkdir(parents=True, exist_ok=True)

            # Generate file list based on session type and time range
            files_dict, archive_files_dict = self._generate_file_list(
                start, end, session, **kwargs
            )

            # Log file generation info (matching NetR9 pattern)
            self.logger.info(f"Generated {len(files_dict)} timestamps")

            if not files_dict:
                self.logger.warning(f"No files to download for {self.station_id}")
                return {
                    "station_id": self.station_id,
                    "receiver_type": "NetRS",
                    "status": "no_files",
                    "files_downloaded": 0,
                    "downloaded_files": [],
                    "duration": time.time() - start_time,
                }

            # Filter out files that already exist in archive (like NetR9 does)
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
                        # Basic sanity check: ensure file is not zero or tiny
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

                    # Check if compressed version exists (.T00.gz)
                    if not str(archive_path).endswith(".gz"):
                        archive_path_gz = archive_path + ".gz"
                        archive_path_gz_obj = Path(archive_path_gz)
                        if archive_path_gz_obj.exists():
                            # Basic sanity check: ensure compressed file is valid
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

                # File is missing from archive, add to download list
                missing_files_dict[filename] = remote_dir

            # Log validation results (matching NetR9 pattern)
            self.logger.info(f"Validated {validated_files} existing files")
            if files_found_in_archive > 0:
                self.logger.info(
                    f"Found {files_found_in_archive} files already archived, skipping re-download"
                )

            if not missing_files_dict:
                self.logger.info("Archive is up to date")
                return {
                    "station_id": self.station_id,
                    "receiver_type": "NetRS",
                    "status": "up_to_date",
                    "files_checked": len(files_dict),
                    "files_missing": 0,
                    "files_downloaded": 0,
                    "duration": time.time() - start_time,
                }

            self.logger.info(f"Missing files: {len(missing_files_dict)}")

            # Log connection info and remote paths (matching NetR9 pattern)
            if missing_files_dict:
                # Log station connection details
                router_ip = self.station_info["router"]["ip"]
                http_port = self.station_info["receiver"]["httpport"]
                self.logger.info(f"Station connection: {router_ip}:{http_port}")

                # Log remote paths (unique paths only)
                logged_paths = set()
                for filename, remote_dir in sorted(missing_files_dict.items(), reverse=True):
                    if remote_dir not in logged_paths:
                        self.logger.info(f"Remote path: {remote_dir}")
                        logged_paths.add(remote_dir)

            # Download files if sync is enabled
            downloaded_files = []
            if sync:
                if missing_files_dict:
                    # Pass archive info for immediate archiving after each download
                    downloaded_files = self.http_downloader.download_files(
                        missing_files_dict,
                        tmp_dir_path,
                        clean_tmp,
                        archive_files_dict=archive_files_dict if archive else None,
                        use_phase1_utilities=archive  # Always use Phase 1 when archiving
                    )
                else:
                    self.logger.info("Archive is up to date - no files to download")
            else:
                self.logger.info("Sync disabled - skipping actual download")

            # Files are archived inline by download client when archive=True
            # downloaded_files contains archive paths when archiving is enabled
            archived_files = []
            if archive and downloaded_files:
                # After archiving, create list of archived file paths
                for file_path in downloaded_files:
                    file_path_obj = Path(file_path)
                    filename = file_path_obj.name
                    if filename in archive_files_dict:
                        archived_files.append(archive_files_dict[filename])

                # Use archived files for reporting if archiving was successful
                final_files = archived_files if archived_files else downloaded_files
            else:
                final_files = downloaded_files

            # Calculate bytes from final file locations
            final_bytes = 0
            for f in final_files:
                try:
                    if Path(f).exists():
                        final_bytes += Path(f).stat().st_size
                except (OSError, IOError):
                    pass

            # Record performance metrics using abstracted utility
            duration = time.time() - start_time
            performance_metrics = create_performance_metrics(
                success=len(final_files) > 0 if sync else True,
                duration=duration,
                bytes_downloaded=final_bytes,
                connection_time=getattr(
                    self.http_downloader, "_last_connection_time", 0.0
                ),
            )
            record_performance_metrics(
                self.station_id, performance_metrics, self.logger
            )

            return {
                "station_id": self.station_id,
                "receiver_type": "NetRS",
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
                "receiver_type": "NetRS",
                "status": "error",
                "files_downloaded": 0,
                "downloaded_files": [],
                "error": error_msg,
                "duration": duration,
            }

    def _process_time_parameters(
        self, start: Union[datetime, str], end: Union[datetime, str], session: str
    ) -> Tuple[datetime, datetime]:
        """Process and validate time parameters.

        Args:
            start: Start time
            end: End time
            session: Session type

        Returns:
            Tuple of processed start and end datetime objects
        """
        self.logger.debug("Using Phase 1 TimeParameterProcessor")
        return self.time_processor.process_time_parameters(start, end, session)

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
        """Generate list of files to download based on time range and session.

        Args:
            start: Start time
            end: End time
            session: Session type
            **kwargs: Additional parameters

        Returns:
            Tuple of (files_dict, archive_files_dict)
            files_dict maps filename -> remote_directory
            archive_files_dict maps filename -> archive_path
        """
        # Parse session parameters using abstracted utility
        afrequency, ffrequency, gt_frequency = parse_session_parameters(session)

        # Generate datetime list using unified build_path approach
        file_datetime_list = self.build_path(
            None, "#datelist", session, gt_frequency, start, end
        )

        # Adjust timestamps to match NetRS file creation times
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

        # Generate remote file paths (NetRS specific format)
        files_dict = {}
        archive_files_dict = {}

        # Get base path and session mapping from config
        base_path = self.netrs_config.get("base_path", "/download/")

        for file_dt in adjusted_datetime_list:
            # Get session mapping from configuration
            # ConfigParser converts keys to lowercase, so normalize session key
            session_key = session.lower()
            session_map_key = f"session_map_{session_key}"
            session_mapping = self.netrs_config.get(session_map_key, "a,a")
            letter_code, remote_subdir = session_mapping.split(",")

            # NetRS filename format: STATIONYYYYMMDDHHMM{session_letter}.T00
            # Timestamps are already adjusted to match file creation times
            filename_format = self.netrs_config.get(
                "remote_filename_format",
                "{station}%Y%m%d%H%M{session_letter}.T00"
            )

            filename = file_dt.strftime(filename_format).format(
                station=self.station_id,
                session_letter=letter_code
            )

            # Remote directory format: /download/YYYYMM/session_directory/
            date_format = self.netrs_config.get("remote_date_format", "%Y%m")
            remote_dir = f"{base_path.rstrip('/')}/{file_dt.strftime(date_format)}/{remote_subdir}"

            files_dict[filename] = remote_dir

        # Generate archive paths using unified approach
        archive_template = self.receivers_config.get_archive_template()
        # NetRS files are archived as compressed .T00.gz
        raw_extension = self.get_file_extension()  # .T00
        archived_extension = raw_extension + ".gz"  # .T00.gz
        full_archive_template = archive_template.format(
            prepath=self.data_prepath,
            station="{station}",
            session="{session}",
            extension=archived_extension,
            session_letter="{session_letter}",
        )

        # Use adjusted datetime list to ensure archive timestamps match filename timestamps
        archive_file_list = self.build_path(
            adjusted_datetime_list, full_archive_template, session, gt_frequency
        )

        # Map filenames to archive paths
        for i, filename in enumerate(files_dict.keys()):
            if i < len(archive_file_list):
                archive_files_dict[filename] = archive_file_list[i]

        return files_dict, archive_files_dict

    def _archive_files(
        self, downloaded_files: List[str], archive_files_dict: Dict[str, str]
    ):
        """Archive downloaded files to final locations with compression.

        Args:
            downloaded_files: List of downloaded file paths
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
        """Get file extension for NetRS files.

        Returns:
            File extension for NetRS receiver files
        """
        return self.netrs_config.get("file_extension", ".T00")

    def get_session_letter(self, session: str) -> str:
        """Get session letter for NetRS receiver type and session.

        Args:
            session: Session type (e.g., '15s_24hr', '1Hz_1hr')

        Returns:
            Session letter code for NetRS
        """
        # Get session mapping from configuration
        # ConfigParser converts keys to lowercase, so normalize session key
        session_key = session.lower()
        session_map_key = f"session_map_{session_key}"
        session_mapping = self.netrs_config.get(session_map_key, "a,a")
        # Format: "letter_code,remote_directory"
        letter_code = session_mapping.split(",")[0]
        return letter_code

    def get_station_info(self) -> Dict[str, Any]:
        """Get station information and configuration.

        Returns:
            Dictionary with station information
        """
        return {
            "station_id": self.station_id,
            "receiver_type": "NetRS",
            "router_ip": self.station_info["router"]["ip"],
            "http_port": self.station_info["receiver"]["httpport"],
            "ftp_port": self.station_info["receiver"].get("ftpport", 21),
            "connection_type": self.station_info["station"].get(
                "connection_type", "unknown"
            ),
            "timeout_category": self.station_info["receiver"].get(
                "timeout_category", "mobile"
            ),
            "configuration": self.station_info,
        }

    def get_firmware_version(self) -> Dict[str, Any]:
        """Get firmware version from NetRS receiver.

        Returns:
            Dictionary with firmware information
        """
        try:
            success, response, error = self.http_client.get_url(
                self.endpoints["firmware"]
            )
            if success and response:
                return {
                    "success": True,
                    "firmware_version": response.strip(),
                    "raw_response": response,
                }
            else:
                return {"success": False, "error": error or "No response"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_position(self) -> Dict[str, Any]:
        """Get current position from NetRS receiver.

        Returns:
            Dictionary with position information
        """
        try:
            success, response, error = self.http_client.get_url(
                self.endpoints["position"]
            )
            if success and response:
                # TODO: Parse position response (would need to see actual response format)
                return {
                    "success": True,
                    "raw_response": response,
                    "note": "Position parsing not yet implemented",
                }
            else:
                return {"success": False, "error": error or "No response"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def __del__(self):
        """Clean up resources."""
        if hasattr(self, "http_client"):
            self.http_client.close()
        if hasattr(self, "http_downloader"):
            self.http_downloader.close()