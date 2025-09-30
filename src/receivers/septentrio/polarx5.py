"""Septentrio PolaRX5 receiver implementation."""

import binascii
import logging
import os
import re
import time
from datetime import datetime, timedelta
from ftplib import FTP
from pathlib import Path
from typing import Any, Dict, Optional, Union

import gtimes.timefunc as gt
from gtimes.timefunc import currDatetime

try:
    from tqdm import tqdm

    progressbar_available = True
except ImportError:
    progressbar_available = False
    tqdm = None

from ..base.exceptions import (
    ConfigurationError,
    ConnectionError,
)
from ..base.receiver import BaseReceiver
from ..utils.file_validator import FileValidator
from ..utils.session_parser import parse_session_parameters
from ..utils.performance_recorder import record_performance_metrics, create_performance_metrics

# Phase 1 utilities (feature-flagged)
from ..utils.archive_validator import ArchiveValidator
from ..utils.time_processor import TimeParameterProcessor
from ..utils.file_archiver import FileArchiver, ArchiveMode


class PolaRX5(BaseReceiver):
    """Septentrio PolaRX5 receiver implementation.

    This class handles data download and health monitoring for Septentrio
    PolaRX5 GNSS receivers used in the Icelandic Met Office GPS network.
    """

    def __init__(self, station_id: str, station_info: Dict[str, Any]):
        """Initialize PolaRX5 receiver.

        Args:
            station_id: Station identifier (e.g., 'REYK', 'HOFN')
            station_info: Station configuration dictionary with router/receiver info
        """
        super().__init__(station_id, station_info)

        # Set up logging
        self.logger = self._get_logger()

        # Extract connection info from station_info
        self._setup_connection_info()

        # Configuration from shared ConfigManager (BaseReceiver provides self.config_manager)
        self.session_map = self.config_manager.get_session_map()
        # Configuration available via BaseReceiver initialization
        # Get Septentrio-specific configuration
        self.septentrio_config = self.receivers_config.get_receiver_config("polarx5")

        # System paths from ConfigManager
        self.base_path = self.config_manager.get_system_path("receiver_base_path")
        self.sbf2rin_path = self.config_manager.get_system_path("sbf2rin_path")
        self.teqc_path = self.config_manager.get_system_path("teqc_path")

        # Initialize file validator for download integrity checking
        self.file_validator = FileValidator(self.logger)

        # Phase 1 utilities (always enabled - Phase 3B)
        self.archive_validator = ArchiveValidator(logger=self.logger)
        self.time_processor = TimeParameterProcessor(logger=self.logger)
        # FileArchiver will be created per-download with appropriate mode

        # Timeout configuration based on station network type
        self._setup_timeouts()

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

    def _setup_timeouts(self):
        """Setup timeout configuration using gps_parser centralized configuration."""
        try:
            # Try to load timeout configuration from gps_parser
            import sys

            sys.path.append("../gps_parser/src")
            import gps_parser

            parser = gps_parser.ConfigParser()
            timeout_config = parser.getStationTimeout(self.station_id)

            self.connection_timeout = timeout_config["connection_timeout"]
            self.inactivity_timeout = timeout_config["inactivity_timeout"]
            self.progress_timeout = timeout_config["progress_timeout"]
            self.min_speed_threshold = timeout_config["min_speed_threshold"]

            self.logger.info(
                f"Timeout config from gps_parser - Connection: {self.connection_timeout}s, "
                f"Inactivity: {self.inactivity_timeout}s, Progress: {self.progress_timeout}s, "
                f"Min speed: {self.min_speed_threshold} B/s"
            )

        except Exception as e:
            # Fallback to default timeout configuration if gps_parser fails
            self.logger.warning(f"Could not load timeout config from gps_parser: {e}")
            self.logger.info("Using fallback timeout configuration")

            # Default fallback timeouts (mobile category as reasonable default)
            self.connection_timeout = 20
            self.inactivity_timeout = 60
            self.progress_timeout = 300
            self.min_speed_threshold = 2048

            self.logger.info(
                f"Fallback timeout config - Connection: {self.connection_timeout}s, "
                f"Inactivity: {self.inactivity_timeout}s, Progress: {self.progress_timeout}s"
            )

    def _setup_connection_info(self):
        """Extract and validate connection information from station_info."""
        try:
            self.ip_number = self.station_info["router"]["ip"]
            self.ip_port = int(self.station_info["receiver"]["ftpport"])

            # Use gps_parser for FTP mode determination
            ftp_mode = self.station_info.get("receiver", {}).get("ftp_mode", "auto")
            if ftp_mode == "active":
                self.pasv = False
            elif ftp_mode == "passive":
                self.pasv = True
            else:
                # If ftp_mode is "auto" or not set, it was already determined by CLI
                # using gps_parser.getStationFtpMode() and passed in station_info
                self.pasv = ftp_mode == "passive"

            self.logger.info(
                f"Station {self.station_id} - Address: {self.ip_number}:{self.ip_port}, FTP Passive: {self.pasv}"
            )

        except KeyError as e:
            raise ConfigurationError(f"Missing configuration key: {e}")
        except ValueError as e:
            raise ConfigurationError(f"Invalid port number: {e}")

    def get_connection_status(self) -> Dict[str, Any]:
        """Check connection status to receiver.

        Returns:
            Dictionary with router and receiver connection status
        """
        try:
            # Simple connection test
            ftp = FTP()
            ftp.connect(self.ip_number, self.ip_port, timeout=self.connection_timeout)
            ftp.login("anonymous")
            ftp.set_pasv(self.pasv)
            ftp.quit()

            status = {
                "router": True,
                "receiver": True,
                "ip": self.ip_number,
                "port": self.ip_port,
                "timestamp": datetime.utcnow().isoformat(),
                "error": None,
            }

        except Exception as e:
            status = {
                "router": False,
                "receiver": False,
                "ip": self.ip_number,
                "port": self.ip_port,
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(e),
            }

        self.connection_status = status
        return status

    def get_file_extension(self) -> str:
        """Get Septentrio file extension from configuration.

        Returns:
            File extension with compression (e.g., '.sbf.gz')
        """
        return self.septentrio_config.get("file_extension", ".sbf.gz")

    def get_session_letter(self, session: str) -> str:
        """Get session letter for Septentrio receiver.

        Args:
            session: Session type (e.g., '15s_24hr', '1Hz_1hr', 'status_1hr')

        Returns:
            Session letter code from session mapping ('a', 'b', 'c')
        """
        if session in self.session_map:
            return self.session_map[session][0]  # First element is the letter
        return "a"  # Default fallback

    def download_data(
        self,
        start: Optional[Union[datetime, str]] = None,
        end: Optional[Union[datetime, str]] = None,
        session: str = "15s_24hr",
        sync: bool = False,
        clean_tmp: bool = True,
        archive: bool = True,
        **kwargs,
    ) -> Dict[str, Any]:
        """Download data from PolaRX5 receiver with comprehensive validation.

        This is the main download function that handles file synchronization
        from the receiver to the local archive with integrity checking and
        fault-tolerant archiving.

        Args:
            start: Start time for download period
            end: End time for download period
            session: Data session type (e.g., '15s_24hr', '1Hz_1hr')
            sync: Whether to actually sync files (False for dry run)
            clean_tmp: Whether to clean temporary directory before download
            archive: Whether to archive downloaded files
            **kwargs: Additional parameters (ffrequency, afrequency, etc.)

        Returns:
            Dictionary with download results and file information
        """
        # Extract legacy parameters from kwargs for backward compatibility
        loglevel = kwargs.get("loglevel", logging.WARNING)
        ffrequency = kwargs.get("ffrequency", "1D")
        afrequency = kwargs.get("afrequency", "15s")
        compression = kwargs.get("compression", ".gz")
        immediate_archive = kwargs.get("immediate_archive", True)
        predir = kwargs.get("predir", "/DSK2/SSN/")

        # Set logger level
        self.logger.setLevel(loglevel)

        # Get centralized configuration paths
        tmp_dir = self.receivers_config.get_tmp_dir()
        tmp_dir_path = Path(tmp_dir) / self.station_id
        tmp_dir_path.mkdir(parents=True, exist_ok=True)

        # Clean tmp directory if requested
        if clean_tmp:
            self.logger.info(f"Cleaning temporary directory: {tmp_dir_path}")
            files_removed = self.file_validator.clean_directory(str(tmp_dir_path))
            if files_removed > 0:
                self.logger.info(f"Removed {files_removed} files from tmp directory")

        # Handle time parameters and performance tracking
        start_time = time.time()
        connection_start_time = 0
        start, end = self._process_time_parameters(start, end, session, ffrequency)

        # Initialize performance metrics
        performance_metrics = {
            "success": False,
            "duration": 0.0,
            "bytes_downloaded": 0,
            "avg_speed": 0.0,
            "had_timeout": False,
            "timeout_type": None,
            "connection_time": 0.0,
        }

        self.logger.info(f"Checking {session} sessions from {start} to {end}")

        # Generate all datetime lists and paths using unified approach
        session_info = self.session_map[session][1]

        # Generate datetime list using unified build_path method
        file_datetime_list = self.build_path(None, "#datelist", session, ffrequency, start, end)
        self.logger.info(f"Generated {len(file_datetime_list)} timestamps")

        # Create archive paths using unified method
        archive_template = self.receivers_config.get_archive_template()
        prepath = self.receivers_config.get_prepath()
        extension = self.get_file_extension()

        # Build archive template
        full_archive_template = archive_template.format(
            prepath=prepath,
            station='{station}',
            session='{session}',
            extension=extension,
            session_letter='{session_letter}'
        )
        archive_file_list = self.build_path(file_datetime_list, full_archive_template, session, ffrequency)

        # Create remote paths with filenames using unified method
        remote_template = f"{self.base_path}{session_info}/%y%j/{self.station_id}#Rin2_{compression}"
        remote_full_paths = self.build_path(file_datetime_list, remote_template, session, ffrequency)

        # Extract remote directories and IGS filenames
        remote_path_list = []
        igs_file_list = []
        for full_path in remote_full_paths:
            remote_dir = os.path.dirname(full_path) + "/"  # Add trailing slash for FTP
            igs_filename = os.path.basename(full_path)
            remote_path_list.append(remote_dir)
            igs_file_list.append(igs_filename)

        file_date_dict = dict(
            zip(file_datetime_list, zip(archive_file_list, igs_file_list))
        )

        # Create remote path mapping for later use in _sync_missing_files
        remote_path_dict = dict(zip(file_datetime_list, remote_path_list))

        # Find missing and invalid files using comprehensive validation
        self.logger.debug("Using Phase 1 ArchiveValidator")
        all_missing_files, validated_files, corrupted_files_removed, files_archived_from_tmp = self._validate_files_phase1(
            file_date_dict, tmp_dir_path
        )

        # Common logging after validation
        if corrupted_files_removed > 0:
            self.logger.info(
                f"Removed {corrupted_files_removed} corrupted/incomplete files"
            )

        if files_archived_from_tmp > 0:
            self.logger.info(
                f"Archived {files_archived_from_tmp} files from tmp directory"
            )

        self.logger.info(f"Validated {validated_files} existing files")

        if not all_missing_files:
            self.logger.info("Archive is up to date")
            # Record performance metrics for successful up-to-date case
            performance_metrics.update(
                {
                    "success": True,
                    "duration": time.time() - start_time,
                    "bytes_downloaded": 0,
                    "avg_speed": 0.0,
                }
            )
            self._record_performance_metrics(performance_metrics)

            return {
                "status": "up_to_date",
                "files_checked": len(file_date_dict),
                "files_missing": 0,
                "files_downloaded": 0,
                "duration": time.time() - start_time,
            }

        self.logger.info(f"Missing files: {len(all_missing_files)}")

        downloaded_files_dict = {}
        total_bytes = 0
        sync_success = True

        # For dry run, try connection first - run diagnostics only if it fails
        if not sync and all_missing_files:
            self.logger.info("Testing FTP connection for dry run...")

            # Optimistic approach: try connection first
            try:
                from ftplib import FTP

                ftp = FTP()
                ftp.connect(self.ip_number, self.ip_port, timeout=3)
                ftp.close()
                self.logger.info(
                    f"✅ Connection test OK: {self.ip_number}:{self.ip_port}"
                )
            except Exception as e:
                # Connection failed - now run diagnostics to understand why
                from ..base.download_diagnostics import DownloadDiagnosticsAnalyzer

                diagnostics = DownloadDiagnosticsAnalyzer(self.station_id, self.logger)

                network_check = diagnostics.classify_network_failure(self.ip_number)

                if network_check["classification"] == "invalid_ip":
                    self.logger.critical(
                        f"❌ INVALID IP: {self.ip_number} - likely configuration typo"
                    )
                    self.logger.critical(
                        f"💡 SUGGESTED FIX: Check station configuration for correct IP address"
                    )
                elif "connection refused" in str(e).lower():
                    self.logger.error(
                        f"❌ FTP CONNECTION REFUSED: {self.ip_number}:{self.ip_port}"
                    )
                    if network_check["classification"] == "network_ok":
                        self.logger.error(
                            f"💡 Router responds but FTP refused - Wrong port ({self.ip_port}) or port forwarding issue"
                        )
                    else:
                        self.logger.error(
                            f"💡 LIKELY ISSUE: Wrong FTP port ({self.ip_port}) or port forwarding not configured"
                        )
                    self.logger.error(
                        f"💡 SUGGESTED ACTION: Verify ftpport={self.ip_port} in station configuration"
                    )
                else:
                    self.logger.warning(f"⚠️ Connection test failed: {e}")
                    if network_check.get("analysis"):
                        self.logger.info(
                            f"Network analysis: {network_check['analysis']}"
                        )

        try:
            if sync:
                downloaded_files_dict = self._sync_missing_files(
                    all_missing_files,
                    tmp_dir_path,
                    session,
                    predir,
                    ffrequency,
                    archive,
                    immediate_archive,
                    compression,
                    remote_path_dict,
                )

                # Calculate total bytes downloaded
                for file_path in downloaded_files_dict.values():
                    if os.path.isfile(file_path):
                        total_bytes += os.path.getsize(file_path)

        except Exception as e:
            sync_success = False

            # Handle ConfigurationError (like invalid IP) with concise output
            from ..base.exceptions import ConfigurationError

            if isinstance(e, ConfigurationError):
                # For configuration errors, provide minimal output focused on the fix
                self.logger.critical(f"❌ Configuration Error: {e}")
                performance_metrics.update(
                    {
                        "success": False,
                        "duration": time.time() - start_time,
                        "error_type": "configuration_error",
                        "error_category": e.category.value
                        if hasattr(e, "category")
                        else "unknown",
                        "validation_needed": True,
                    }
                )
                self._record_performance_metrics(performance_metrics)

                return {
                    "status": "configuration_error",
                    "error": str(e),
                    "error_type": "invalid_ip_range",
                    "suggested_fix": getattr(
                        e, "suggested_fix", "Check station configuration"
                    ),
                    "files_checked": len(file_date_dict),
                    "files_missing": len(all_missing_files),
                    "files_downloaded": 0,
                    "duration": time.time() - start_time,
                    "validation_triggered": True,
                }

            # For other errors, log normally
            self.logger.error(f"Sync failed: {e}")

            # Check if this was a timeout-related error
            if any(
                timeout_word in str(e).lower()
                for timeout_word in ["timeout", "timed out"]
            ):
                performance_metrics["had_timeout"] = True
                if "connection" in str(e).lower():
                    performance_metrics["timeout_type"] = "connection"
                elif "inactivity" in str(e).lower():
                    performance_metrics["timeout_type"] = "inactivity"
                else:
                    performance_metrics["timeout_type"] = "progress"

            # Enhanced failure handling - analyze what went wrong
            try:
                # Get expected vs found files for analysis
                expected_files = list(file_date_dict.keys())
                found_files = (
                    list(downloaded_files_dict.keys())
                    if "downloaded_files_dict" in locals()
                    else []
                )

                # Create receiver response context
                receiver_response = {
                    "connection_ok": getattr(self, "_last_connection_time", 0) > 0,
                    "ftp_connected": False,  # Connection failed, so FTP never connected
                    "performance_metrics": performance_metrics,
                }

                # Use enhanced failure handling
                failure_result = self.handle_download_failure(
                    expected_files=expected_files,
                    found_files=found_files,
                    session_type=session,
                    date_range=f"{start} to {end}",
                    error=e,
                    receiver_response=receiver_response,
                )

                # Log the failure analysis
                if failure_result and "failure_analysis" in failure_result:
                    analysis = failure_result["failure_analysis"]
                    self.logger.info(
                        f"Failure analysis: {analysis.get('analysis', 'No analysis available')}"
                    )
                    if analysis.get("validation_trigger"):
                        self.logger.warning(
                            f"Validation triggered for {self.station_id}"
                        )

            except Exception as handler_error:
                self.logger.debug(f"Failure handler error: {handler_error}")

        # Record final performance metrics
        final_duration = time.time() - start_time
        performance_metrics.update(
            {
                "success": sync_success and len(downloaded_files_dict) > 0,
                "duration": final_duration,
                "bytes_downloaded": total_bytes,
                "avg_speed": total_bytes / final_duration
                if final_duration > 0
                else 0.0,
                "connection_time": getattr(self, "_last_connection_time", 0.0),
            }
        )
        self._record_performance_metrics(performance_metrics)

        return {
            "status": "completed" if sync else "dry_run",
            "files_checked": len(file_date_dict),
            "files_missing": len(all_missing_files),
            "files_downloaded": len(downloaded_files_dict),
            "downloaded_files": list(downloaded_files_dict.values()),
            "duration": final_duration,
        }

    def _validate_files_phase1(self, file_date_dict, tmp_dir_path):
        """Validate files using Phase 1 ArchiveValidator.

        Args:
            file_date_dict: Dict mapping datetime -> (archive_path, igs_filename)
            tmp_dir_path: Path to temporary download directory

        Returns:
            Tuple of (all_missing_files, validated_files, corrupted_files_removed, files_archived_from_tmp)
        """
        # Convert format for ArchiveValidator
        # ArchiveValidator expects: filename -> remote_path (for missing files)
        # and filename -> archive_path (for archive files)
        files_dict = {}  # filename -> remote_path mapping
        archive_paths_dict = {}  # filename -> archive_path mapping

        for dt, (archive_path, igs_filename) in file_date_dict.items():
            files_dict[igs_filename] = archive_path  # Use archive path as "remote" for validation
            archive_paths_dict[igs_filename] = archive_path

        # Use batch validation
        missing_files_dict, found_count, validated_count = self.archive_validator.batch_validate_archives(
            files_dict,
            archive_paths_dict,
            tmp_dir_path
        )

        # Convert back to expected format: datetime -> (archive_path, igs_filename)
        all_missing_files = {}
        for igs_filename in missing_files_dict.keys():
            # Find corresponding datetime key
            for dt, (arch_path, igs_file) in file_date_dict.items():
                if igs_file == igs_filename:
                    all_missing_files[dt] = (arch_path, igs_file)
                    break

        self.logger.info(f"Phase 1 validation: {validated_count} files checked, {found_count} found, {len(missing_files_dict)} missing")

        return all_missing_files, validated_count, 0, 0  # missing, validated, corrupted_removed, archived_from_tmp

    def _process_time_parameters(self, start, end, session, ffrequency):
        """Process and validate time parameters using Phase 1 TimeParameterProcessor."""
        self.logger.debug("Using Phase 1 TimeParameterProcessor")
        return self.time_processor.process_time_parameters(start, end, session)

    def make_file_name(self, day, session="15s_24hr", compression=".gz"):
        """Generate Septentrio file name using getSeptentrio3 logic.

        Args:
            day: datetime object for the file date
            session: session type (15s_24hr, 1Hz_1hr)
            compression: compression suffix (.gz)

        Returns:
            str: formatted filename (e.g., ELDC202509040000a.sbf.gz)
        """
        import re

        # Session type detection
        daysession = re.compile(r"24h", re.IGNORECASE)
        hoursession = re.compile(r"1h", re.IGNORECASE)

        if daysession.search(session):
            filedate = day.strftime("%Y%m%d0000a")  # Daily files end with 'a'
        elif hoursession.search(session):
            filedate = day.strftime("%Y%m%d%H00b")  # Hourly files end with 'b'
        else:
            # Default to daily format
            filedate = day.strftime("%Y%m%d0000a")

        # Septentrio PolaRX5 uses .sbf format
        file_name = f"{self.station_id}{filedate}.sbf{compression}"

        return file_name

    def _get_remote_file_path(self, date_key, session):
        """Get remote file path for a given date and session."""
        if session not in self.session_map:
            raise ConfigurationError(f"Unknown session type: {session}")

        session_letter, session_path = self.session_map[session]

        # Build remote path like getSeptentrio3
        gps_week = gt.date2gpsWeek(date_key)[0]
        remote_path = f"{self.base_path}{session_path}/{gps_week:05d}/"

        return remote_path

    def _sync_missing_files(
        self,
        missing_file_dict,
        tmp_dir,
        session,
        predir,
        ffrequency,
        archive,
        immediate_archive,
        compression=".gz",
        remote_path_dict=None,
    ):
        """Sync missing files from receiver to local archive."""
        # Simple approach: use pre-built paths and extract IGS filenames

        # Sort missing_file_dict by datetime to ensure consistent ordering
        sorted_missing_items = sorted(missing_file_dict.items())

        # Extract IGS filenames and get corresponding remote paths
        download_file_dict = {}
        for dt, (archive_path, igs_filename) in sorted_missing_items:
            if remote_path_dict and dt in remote_path_dict:
                remote_path = remote_path_dict[dt]
            else:
                # Fallback to old method if remote_path_dict not available
                raise ValueError("Remote path dictionary is required but not provided")
            download_file_dict[igs_filename] = remote_path

        # Create properly ordered missing_file_dict for immediate archiving
        updated_missing_file_dict = dict(sorted_missing_items)

        # Connect and download
        ftp = self._ftp_open_connection()
        if not ftp:
            raise ConnectionError(
                f"Could not connect to {self.ip_number}:{self.ip_port}"
            )

        try:
            downloaded_files = self._ftp_download(
                download_file_dict,
                tmp_dir,
                ftp=ftp,
                archive=archive,
                immediate_archive=immediate_archive,
                missing_file_dict=updated_missing_file_dict,
            )

            downloaded_files_dict = dict(
                zip(updated_missing_file_dict, downloaded_files)
            )

            # Archive files (only if not using immediate archiving)
            if downloaded_files_dict and archive and not immediate_archive:
                self._archive_files(downloaded_files_dict, updated_missing_file_dict)

            return downloaded_files_dict

        finally:
            ftp.close()

    def _ftp_open_connection(self, timeout: Optional[int] = None) -> Optional[FTP]:
        """Open FTP connection to receiver with optimistic approach - try first, diagnose on failure."""
        connection_start = time.time()

        # Optimistic approach: Try connection directly first
        try:
            self.logger.info(
                f"Attempting FTP connection to {self.ip_number}:{self.ip_port}..."
            )
            ftp = FTP()
            if timeout is None:
                timeout = self.connection_timeout
            ftp.connect(self.ip_number, self.ip_port, timeout=timeout)
            ftp.login("anonymous")
            ftp.set_pasv(self.pasv)
            connection_time = time.time() - connection_start
            self.logger.info(f"Connection successful in {connection_time:.2f}s!")

            # Store connection time in instance variable for performance tracking
            self._last_connection_time = connection_time

            return ftp
        except Exception as e:
            connection_time = time.time() - connection_start
            error_str = str(e).lower()

            # Connection failed - now run intelligent diagnostics to determine why
            self.logger.info(
                f"Connection failed after {connection_time:.2f}s - running diagnostics..."
            )

            from ..base.download_diagnostics import DownloadDiagnosticsAnalyzer

            diagnostics = DownloadDiagnosticsAnalyzer(self.station_id, self.logger)

            # Quick network classification to understand the failure
            network_check = diagnostics.classify_network_failure(self.ip_number)

            # If it's an invalid IP range, provide critical error immediately
            if network_check["classification"] == "invalid_ip":
                self.logger.critical(
                    f"❌ INVALID IP: {self.ip_number} - likely configuration typo"
                )
                self.logger.critical(
                    f"💡 SUGGESTED FIX: Check station configuration for correct IP address"
                )

                from ..base.exceptions import ConfigurationError, FailureCategory

                raise ConfigurationError(
                    message=f"Invalid IP range {self.ip_number} - likely configuration typo",
                    station_id=self.station_id,
                    category=FailureCategory.DNS_FAILURE,
                    config_field="router_ip",
                    actual_value=self.ip_number,
                    suggested_fix="Verify IP address in station configuration",
                )

            # Provide intelligent error analysis based on failure type and network status
            if "connection refused" in error_str:
                self.logger.error(
                    f"❌ FTP CONNECTION REFUSED: {self.ip_number}:{self.ip_port}"
                )
                if network_check["classification"] == "network_ok":
                    self.logger.error(
                        f"💡 Router responds but FTP refused - Wrong port ({self.ip_port}) or port forwarding issue"
                    )
                else:
                    self.logger.error(
                        f"💡 LIKELY ISSUE: Wrong FTP port ({self.ip_port}) or port forwarding not configured"
                    )
                self.logger.error(
                    f"💡 SUGGESTED ACTION: Verify ftpport={self.ip_port} in station configuration"
                )
            elif "timeout" in error_str or "timed out" in error_str:
                if network_check["classification"] == "network_ok":
                    self.logger.error(
                        f"⚠️ RECEIVER TIMEOUT: Router responds but receiver doesn't on FTP port"
                    )
                    self.logger.error(
                        f"💡 LIKELY ISSUE: Receiver down, ethernet broken, or firewall blocking FTP"
                    )
                else:
                    self.logger.error(f"⚠️ NETWORK TIMEOUT: {network_check['analysis']}")
            else:
                self.logger.error(f"Connection failed: {e}")
                self.logger.info(f"Network analysis: {network_check['analysis']}")

            self._last_connection_time = connection_time
            return None

    def _ftp_download(
        self,
        files_dict,
        local_dir,
        ftp=None,
        archive=True,
        immediate_archive=True,
        missing_file_dict=None,
    ):
        """Download files via FTP with progress tracking."""
        downloaded_files = []

        # Log station connection details once at the beginning
        self.logger.info(f"Station connection: {self.ip_number}:{self.ip_port}")

        # Track unique paths to log each only once
        logged_paths = set()

        # For immediate archiving, we need to track which datetime each file corresponds to
        # This will be populated from the calling code

        for file_name, remote_dir in sorted(files_dict.items(), reverse=True):
            # Log remote directory path only once per unique path
            if remote_dir not in logged_paths:
                self.logger.info(f"Remote path: {remote_dir}")
                logged_paths.add(remote_dir)

            self.logger.info(f"Downloading {file_name}")

            local_file = local_dir / file_name
            # Initialize offset for download resumption
            offset = 0

            remote_file = f"{remote_dir}{file_name}"

            try:
                # Check if remote file exists and get size (like getSeptentrio3)
                try:
                    remote_file_size = ftp.size(remote_file)
                    remote_file_exists = True
                except Exception as e:
                    # Check if it's a "file not found" vs "connection error"
                    error_msg = str(e).lower()
                    if (
                        "550" in error_msg
                        or "not found" in error_msg
                        or "no such file" in error_msg
                    ):
                        # Remote file is missing - check local file for archiving
                        remote_file_exists = False
                        remote_file_size = None
                        if local_file.exists():
                            local_size = local_file.stat().st_size
                            if local_size > 0:
                                self.logger.info(
                                    f"📁 Remote file {file_name} missing, but local copy exists ({local_size:,} bytes)"
                                )
                                self.logger.info(
                                    f"   Adding existing local file to archive queue: {local_file}"
                                )
                                # Add to downloaded_files for archiving (it's a complete file from previous download)
                                downloaded_files.append(str(local_file))
                            else:
                                # Zero-size file should be removed
                                self.logger.warning(
                                    f"🗑️ Removing zero-size local file: {local_file}"
                                )
                                local_file.unlink()
                            continue
                        else:
                            self.logger.error(
                                f"❌ Remote file {file_name} not found on server"
                            )
                            continue  # No local, no remote - nothing to do
                    else:
                        # Connection/server error - can't determine file status
                        self.logger.error(
                            f"⚠️  Cannot check remote file {file_name}: {e}"
                        )
                        remote_file_exists = False
                        remote_file_size = None

                # Now that we have remote file size, check if we should resume existing local file
                if local_file.exists():
                    should_resume, resume_offset = (
                        self.file_validator.should_resume_download(
                            str(local_file), remote_file_size
                        )
                    )
                    if should_resume:
                        offset = resume_offset
                        self.logger.info(f"📄 Resuming download from {offset:,} bytes")
                    else:
                        self.logger.info(
                            f"🔄 Starting fresh download (invalid tmp file removed)"
                        )

                if remote_file_size is not None:
                    # Use progress bar download
                    diff = self._download_with_progressbar_and_retry(
                        ftp, remote_file, str(local_file), remote_file_size, offset
                    )

                    # Validate download completeness (like getSeptentrio3)
                    local_file_size = local_file.stat().st_size
                    self.logger.info(
                        f"Remote file size: {remote_file_size} bytes, Local file size: {local_file_size} bytes"
                    )
                    self.logger.info(
                        f"Difference between remote and downloaded file: {diff} bytes"
                    )

                    if diff == 0:
                        self.logger.info(
                            f"✅ Successfully downloaded {file_name} ({local_file_size:,} bytes)"
                        )

                        # Validate downloaded file integrity
                        validation_result = self.file_validator.validate_file(
                            str(local_file)
                        )
                        if not validation_result["valid"]:
                            self.logger.warning(
                                f"Downloaded file failed validation: {validation_result['error']}"
                            )
                            self.logger.info(
                                f"Removing invalid downloaded file: {local_file}"
                            )
                            try:
                                os.unlink(local_file)
                                continue  # Skip this file
                            except OSError as e:
                                self.logger.error(
                                    f"Could not remove invalid file {local_file}: {e}"
                                )
                                continue
                        else:
                            self.logger.debug(
                                f"Downloaded file validated: {validation_result['compression']} compression, {validation_result['size']} bytes"
                            )

                        # Immediate archiving if enabled
                        if immediate_archive and archive and missing_file_dict:
                            # Find the datetime key for this file by matching the downloaded filename
                            file_datetime = None
                            for dt_key, (
                                arch_path,
                                igs_filename,
                            ) in missing_file_dict.items():
                                if file_name == igs_filename:
                                    file_datetime = dt_key
                                    self.logger.info(
                                        f"✅ Found match: {file_name} -> {dt_key}"
                                    )
                                    break

                            if file_datetime and self._archive_single_file(
                                str(local_file),
                                file_datetime,
                                {file_datetime: missing_file_dict[file_datetime]},
                            ):
                                # File successfully archived - add archive path to downloaded files
                                downloaded_files.append(
                                    missing_file_dict[file_datetime][0]
                                )
                            else:
                                # Archive failed - add tmp file path
                                downloaded_files.append(str(local_file))
                        else:
                            # No immediate archiving - add tmp file path
                            downloaded_files.append(str(local_file))
                    else:
                        self.logger.error(
                            f"❌ Download incomplete for {file_name}: size mismatch of {diff} bytes"
                        )
                        self.logger.error(
                            f"   Expected: {remote_file_size:,} bytes, Got: {local_file_size:,} bytes"
                        )
                        self.logger.info(
                            f"   Partial file kept for resume: {local_file}"
                        )
                        # Keep partial file for resume in next attempt

                else:
                    # Fallback to simple download without progress
                    file_mode = "ab" if offset > 0 else "wb"
                    with open(local_file, file_mode) as f:
                        ftp.retrbinary(f"RETR {remote_file}", f.write, rest=offset)

                    # Without remote size, just check if file grew
                    if local_file.exists() and local_file.stat().st_size > offset:
                        # Validate downloaded file integrity
                        validation_result = self.file_validator.validate_file(
                            str(local_file)
                        )
                        if not validation_result["valid"]:
                            self.logger.warning(
                                f"Downloaded file failed validation: {validation_result['error']}"
                            )
                            self.logger.info(
                                f"Removing invalid downloaded file: {local_file}"
                            )
                            try:
                                os.unlink(local_file)
                                continue  # Skip this file
                            except OSError as e:
                                self.logger.error(
                                    f"Could not remove invalid file {local_file}: {e}"
                                )
                                continue

                        # Immediate archiving if enabled (same logic as progress bar path)
                        if immediate_archive and archive and missing_file_dict:
                            # Find the datetime key for this file by matching the downloaded filename
                            file_datetime = None
                            for dt_key, (
                                arch_path,
                                igs_filename,
                            ) in missing_file_dict.items():
                                if file_name == igs_filename:
                                    file_datetime = dt_key
                                    self.logger.info(
                                        f"✅ Found match: {file_name} -> {dt_key}"
                                    )
                                    break

                            if file_datetime and self._archive_single_file(
                                str(local_file),
                                file_datetime,
                                {file_datetime: missing_file_dict[file_datetime]},
                            ):
                                # File successfully archived - add archive path to downloaded files
                                downloaded_files.append(
                                    missing_file_dict[file_datetime][0]
                                )
                                self.logger.info(
                                    f"Successfully downloaded and archived {file_name} (fallback path)"
                                )
                            else:
                                # Archive failed - add tmp file path
                                downloaded_files.append(str(local_file))
                                self.logger.info(
                                    f"Successfully downloaded {file_name} (fallback path, archiving failed)"
                                )
                        else:
                            # No immediate archiving - add tmp file path
                            downloaded_files.append(str(local_file))
                            self.logger.info(
                                f"Successfully downloaded {file_name} (size validation not available, integrity validated)"
                            )
                    else:
                        self.logger.warning(f"Download may have failed for {file_name}")

            except Exception as e:
                self.logger.error(f"Failed to download {file_name}: {e}")
                continue

        return downloaded_files

    def _download_with_progressbar(
        self, ftp, remote_file, local_file, remote_file_size, offset=0
    ):
        """Download file with progress bar display and intelligent timeout handling.

        Implements station-specific timeout handling:
        - Progress-based timeouts (don't timeout if making progress)
        - Inactivity timeouts (timeout if no progress at all)
        - Speed-based timeouts (timeout if too slow overall)
        - Station-specific thresholds for mobile/remote stations
        """
        if not progressbar_available:
            # Fallback without progress bar
            file_mode = "ab" if offset > 0 else "wb"
            with open(local_file, file_mode) as f:
                ftp.retrbinary(f"RETR {remote_file}", f.write, rest=offset)
        else:
            # Use tqdm progress bar with intelligent timeout monitoring
            filename = Path(remote_file).name
            desc = f"Downloading {filename}"

            # Progress monitoring variables
            last_progress_time = time.time()
            last_bytes = offset
            start_time = time.time()

            with tqdm(
                total=remote_file_size,
                initial=offset,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=desc,
            ) as pbar:
                file_mode = "ab" if offset > 0 else "wb"
                with open(local_file, file_mode) as f:

                    def callback(chunk):
                        nonlocal last_progress_time, last_bytes

                        # Write chunk and update progress
                        f.write(chunk)
                        pbar.update(len(chunk))

                        # Check timeout conditions
                        current_bytes = pbar.n
                        current_time = time.time()
                        time_since_last_progress = current_time - last_progress_time
                        bytes_since_last_check = current_bytes - last_bytes

                        # If we made progress, reset progress timer
                        if bytes_since_last_check > 0:
                            last_progress_time = current_time
                            last_bytes = current_bytes

                        # Check for inactivity timeout (no progress at all)
                        elif time_since_last_progress > self.inactivity_timeout:
                            raise ConnectionError(
                                f"Download timed out: no progress for {time_since_last_progress:.1f}s"
                            )

                        # Check for overall progress timeout (making progress but too slow)
                        total_time = current_time - start_time
                        if total_time > self.progress_timeout:
                            avg_speed = (
                                (current_bytes - offset) / total_time
                                if total_time > 0
                                else 0
                            )
                            if avg_speed < self.min_speed_threshold:
                                raise ConnectionError(
                                    f"Download timed out: speed {avg_speed:.0f} B/s below minimum {self.min_speed_threshold} B/s"
                                )

                    ftp.retrbinary(f"RETR {remote_file}", callback, rest=offset)

        local_file_size = os.path.getsize(local_file)
        return local_file_size - remote_file_size

    def _download_with_progressbar_and_retry(
        self, ftp, remote_file, local_file, remote_file_size, offset=0
    ):
        """Download with progress bar and intelligent FTP mode retry on connection issues."""
        try:
            # Try with current FTP mode first
            return self._download_with_progressbar(
                ftp, remote_file, local_file, remote_file_size, offset
            )
        except Exception as e:
            error_msg = str(e).lower()

            # Check if it's a data connection issue that might benefit from mode switching
            connection_errors = [
                "connection refused",
                "errno 111",
                "data connection",
                "port",
            ]
            if any(err in error_msg for err in connection_errors):
                self.logger.warning(
                    f"⚠️  Data connection failed with {self._get_ftp_mode_description()}: {e}"
                )

                # Try switching FTP mode
                original_pasv = ftp.passiveserver
                try:
                    new_pasv = not original_pasv
                    ftp.set_pasv(new_pasv)
                    self.logger.info(
                        f"🔄 Retrying with {self._get_ftp_mode_description(new_pasv)} mode..."
                    )

                    # Retry download with switched mode
                    result = self._download_with_progressbar(
                        ftp, remote_file, local_file, remote_file_size, offset
                    )
                    self.logger.info(
                        f"✅ Success with {self._get_ftp_mode_description(new_pasv)} mode - updating station config"
                    )

                    # Update our internal mode preference for this station
                    self.pasv = new_pasv

                    return result

                except Exception as retry_e:
                    # Restore original mode and re-raise original error
                    ftp.set_pasv(original_pasv)
                    self.logger.error(
                        f"❌ Both FTP modes failed. Original: {e}, Retry: {retry_e}"
                    )
                    raise e
            else:
                # Not a connection error we can fix with mode switching
                raise e

    def _get_ftp_mode_description(self, pasv=None):
        """Get human-readable FTP mode description."""
        if pasv is None:
            pasv = getattr(self, "pasv", True)
        return "passive" if pasv else "active"

    def _archive_single_file(
        self, tmp_file_path: str, file_datetime, missing_file_dict
    ) -> bool:
        """Archive a single file immediately after download using Phase 1 FileArchiver.

        Args:
            tmp_file_path: Path to the temporary downloaded file
            file_datetime: The datetime key for this file
            missing_file_dict: Dict mapping datetime to (archive_path, remote_path) tuples

        Returns:
            True if archiving succeeded, False otherwise
        """
        self.logger.debug("Using Phase 1 FileArchiver (IMMEDIATE mode)")
        destination = missing_file_dict[file_datetime][0]

        with FileArchiver(mode=ArchiveMode.IMMEDIATE, logger=self.logger) as archiver:
            result = archiver.archive_file(
                Path(tmp_file_path),
                Path(destination),
                compress=True,
                remove_tmp=True
            )

        return result.success

    def _archive_files(self, downloaded_files_dict, missing_file_dict):
        """Move downloaded files to archive locations using Phase 1 FileArchiver (BULK mode)."""
        self.logger.debug("Using Phase 1 FileArchiver (BULK mode)")
        with FileArchiver(mode=ArchiveMode.BULK, logger=self.logger) as archiver:
            for ddate, tmp_file in downloaded_files_dict.items():
                if not os.path.isfile(tmp_file):
                    continue
                destination = missing_file_dict[ddate][0]
                archiver.archive_file(
                    Path(tmp_file),
                    Path(destination),
                    compress=True,
                    remove_tmp=True
                )
            # Auto-flushes on context exit

        stats = archiver.get_statistics()
        self.logger.info(f"Archiving complete: {stats['successful']}/{stats['total_files']} files archived")
        return stats['successful']

    def _cleanup_empty_tmp_directories(self):
        """Remove empty station directories from tmp download area."""
        try:
            tmp_base = Path("/home/bgo/tmp/download/")
            if tmp_base.exists():
                for station_dir in tmp_base.iterdir():
                    if station_dir.is_dir() and not any(station_dir.iterdir()):
                        station_dir.rmdir()
                        self.logger.info(
                            f"🧹 Removed empty tmp directory: {station_dir}"
                        )
        except Exception as e:
            self.logger.warning(f"⚠️  Failed to clean up tmp directories: {e}")

    def get_health_status(self) -> Dict[str, Any]:
        """Get health status of PolaRX5 receiver.

        Returns:
            Dictionary with health status information
        """
        health = {
            "station_id": self.station_id,
            "receiver_type": "PolaRX5",
            "timestamp": datetime.utcnow().isoformat(),
            "connection": self.get_connection_status(),
            "data_flow": "N/A",  # TODO: Implement data flow check
            "storage": "N/A",  # TODO: Implement storage check
            "overall_status": "unknown",
        }

        # Determine overall status
        if health["connection"]["receiver"]:
            health["overall_status"] = "healthy"
        else:
            health["overall_status"] = "unhealthy"

        return health

    def analyze_health_data(self, ascii_dir: Optional[str] = None) -> Dict[str, Any]:
        """Analyze health data from converted ASCII status files.

        Uses the HealthDataAnalyzer to process ReceiverStatus blocks from
        ASCII files converted from SBF status sessions.

        Args:
            ascii_dir: Directory containing ASCII status files.
                      If None, uses default status_1hr/ascii directory.

        Returns:
            Dictionary with comprehensive health analysis results
        """
        from .health_analyzer import HealthDataAnalyzer

        if ascii_dir is None:
            ascii_dir = f"data/2025/sep/{self.station_id}/status_1hr/ascii"

        if not os.path.exists(ascii_dir):
            return {
                "error": f"ASCII directory not found: {ascii_dir}",
                "suggestion": "Run SBF to ASCII conversion first",
            }

        # Initialize and run health analyzer
        analyzer = HealthDataAnalyzer(ascii_dir)
        analyzer.load_all_files()

        if not analyzer.health_data:
            return {
                "error": "No health data found in ASCII files",
                "ascii_dir": ascii_dir,
            }

        # Get comprehensive analysis
        cpu_analysis = analyzer.analyze_cpu_load()
        uptime_analysis = analyzer.analyze_uptime()
        status_analysis = analyzer.analyze_rx_status()

        # Generate DataFrame for time series analysis
        df = analyzer.get_dataframe()
        time_span = (
            df["datetime"].max() - df["datetime"].min() if not df.empty else None
        )

        return {
            "station_id": self.station_id,
            "analysis_timestamp": datetime.utcnow().isoformat(),
            "ascii_directory": ascii_dir,
            "data_summary": {
                "total_records": len(analyzer.health_data),
                "time_span": str(time_span) if time_span else None,
                "first_record": df["datetime"].min().isoformat()
                if not df.empty
                else None,
                "last_record": df["datetime"].max().isoformat()
                if not df.empty
                else None,
            },
            "cpu_analysis": cpu_analysis,
            "uptime_analysis": uptime_analysis,
            "receiver_status": status_analysis,
            "health_report": analyzer.generate_health_report(),
            "dataframe_available": not df.empty,
        }

    def download_health_data(
        self,
        start: Optional[Union[datetime, str]] = None,
        end: Optional[Union[datetime, str]] = None,
        convert_to_ascii: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Download and optionally convert health data for analysis.

        Downloads LOG5_status_1hr session containing binary health/status files.
        Future versions will support automatic SBF to ASCII conversion and
        integration with health analysis workflows.

        Args:
            start: Start time for download period
            end: End time for download period
            convert_to_ascii: Future feature - convert SBF to ASCII format
            **kwargs: Additional parameters passed to download_data

        Returns:
            Dictionary with download results and health data files

        TODO: Add SBF to ASCII conversion when convert_to_ascii=True
        TODO: Integrate with health analysis pipeline
        TODO: Add automatic health report generation option
        """
        # Download raw SBF health data
        result = self.download_data(
            start=start, end=end, session="status_1hr", ffrequency="1H", **kwargs
        )

        # TODO: Future enhancement - SBF to ASCII conversion
        if convert_to_ascii:
            self.logger.warning(
                "SBF to ASCII conversion not yet implemented. "
                "Downloaded files are in binary SBF format."
            )
            # Future: Add conversion logic here
            # Future: Update result with ASCII file paths

        # TODO: Future enhancement - trigger health analysis if requested
        # if kwargs.get('analyze_after_download'):
        #     result['health_analysis'] = self.analyze_health_data()

        return result

    def get_health_archive_path(self, timestamp: datetime) -> str:
        """Generate health data archive path using unified build_path method.

        Format: {prepath}/YYYY/#b/STATION/status/station_status_timestamp.sbf

        Args:
            timestamp: datetime for the health data file

        Returns:
            Full path to health archive file
        """
        # Use unified build_path method with health-specific template
        health_template = f"{self.data_prepath}%Y/#b/{{station}}/status/{{station_lower}}_status_%Y%m%d%H%M.sbf"

        # Substitute station-specific placeholders
        health_template = health_template.format(
            station=self.station_id,
            station_lower=self.station_id.lower()
        )

        return self.build_path(timestamp, health_template, "status_1hr", "1H")[0]

    def convert_sbf_to_ascii(self, sbf_file_path: Union[str, Path]) -> Dict[str, Any]:
        """Convert SBF binary health file to ASCII using sbf2rin + teqc.

        This implements the operational conversion workflow:
        SBF → (sbf2rin) → RINEX → (teqc +err) → ASCII health data

        Args:
            sbf_file_path: Path to SBF binary file

        Returns:
            Dictionary with conversion results and output paths
        """
        import subprocess
        import tempfile
        from pathlib import Path

        sbf_file = Path(sbf_file_path)
        if not sbf_file.exists():
            raise FileNotFoundError(f"SBF file not found: {sbf_file_path}")

        # Create temporary and output files
        temp_dir = Path(tempfile.mkdtemp())
        rinex_temp = temp_dir / f"{sbf_file.stem}.rinex"
        ascii_output = sbf_file.parent / f"{sbf_file.stem}.ascii"

        try:
            # Step 1: SBF to RINEX conversion
            sbf2rin_cmd = [
                self.sbf2rin_path,
                "-f",
                str(sbf_file),
                "-d",
                str(rinex_temp),
            ]
            result1 = subprocess.run(sbf2rin_cmd, capture_output=True, text=True)

            if result1.returncode != 0:
                return {
                    "success": False,
                    "error": f"sbf2rin failed: {result1.stderr}",
                    "step": "sbf2rin",
                }

            # Step 2: TEQC health data extraction (simplified - would need proper config)
            teqc_cmd = [self.teqc_path, "+err", "err.lst", str(rinex_temp)]
            result2 = subprocess.run(
                teqc_cmd, capture_output=True, text=True, cwd=temp_dir
            )

            # Save ASCII output
            with open(ascii_output, "w") as f:
                f.write(result2.stdout)

            return {
                "success": True,
                "sbf_file": str(sbf_file),
                "ascii_file": str(ascii_output),
                "sbf2rin_success": result1.returncode == 0,
                "teqc_success": result2.returncode == 0,
                "processing_time": datetime.utcnow().isoformat(),
            }

        except Exception as e:
            return {"success": False, "error": str(e), "step": "conversion_process"}
        finally:
            # Cleanup temporary files
            import shutil

            shutil.rmtree(temp_dir, ignore_errors=True)

    def extract_health_metrics(
        self, ascii_file_path: Union[str, Path]
    ) -> Dict[str, Any]:
        """Extract health metrics from ASCII converted file.

        Parses TEQC ASCII output to extract structured health data
        matching the current PostgreSQL schema.

        Args:
            ascii_file_path: Path to ASCII health data file

        Returns:
            Dictionary with structured health metrics
        """
        # TODO: Implement ASCII parsing based on actual TEQC output format
        # This is a placeholder structure matching current DB schema
        return {
            "router_status": True,  # Parse from ASCII
            "receiver_status": True,  # Parse from ASCII
            "temperature": 45.2,  # Extract from ASCII
            "voltage": 12.1,  # Extract from ASCII
            "satellite_count": 12,  # Additional metric
            "signal_quality": {  # Enhanced metrics
                "GPS": 8.5,
                "GLONASS": 7.2,
                "Galileo": 8.1,
            },
        }

    def store_health_data(
        self, health_data: Dict[str, Any], storage_path: Optional[str] = None
    ) -> str:
        """Store health data to structured JSON files for gradual DB migration.

        Creates file structure: {prepath}/YYYY/#b/station/status/health/

        Args:
            health_data: Structured health metrics dictionary
            storage_path: Optional custom storage path

        Returns:
            Path to stored JSON file
        """
        import json
        from pathlib import Path

        if storage_path is None:
            # Use gtimes pattern for health storage
            timestamp = datetime.fromisoformat(
                health_data.get("timestamp", datetime.utcnow().isoformat())
            )
            health_dir_format = (
                f"{self.data_prepath}%Y/#b/{self.station_id}/status/health/"
            )
            health_dir = gt.datepathlist(
                health_dir_format, "1D", datelist=[timestamp], closed="both"
            )[0]
        else:
            health_dir = storage_path

        Path(health_dir).mkdir(parents=True, exist_ok=True)

        # Create daily health file with timestamp
        timestamp_str = health_data.get("timestamp", datetime.utcnow().isoformat())
        date_str = datetime.fromisoformat(timestamp_str).strftime("%Y%m%d")
        json_file = (
            Path(health_dir) / f"{self.station_id.lower()}_health_{date_str}.json"
        )

        # Append to daily file (for multiple hourly measurements)
        existing_data = []
        if json_file.exists():
            with open(json_file, "r") as f:
                existing_data = json.load(f)

        existing_data.append(health_data)

        with open(json_file, "w") as f:
            json.dump(existing_data, f, indent=2)

        return str(json_file)

    def _record_performance_metrics(self, performance_metrics: Dict[str, Any]) -> None:
        """
        Record performance metrics using gps_parser adaptive learning system.

        Args:
            performance_metrics: Dictionary with performance data
        """
        try:
            # Try to load gps_parser and record performance data
            import sys

            sys.path.append("../gps_parser/src")
            import gps_parser

            parser = gps_parser.ConfigParser()
            parser.record_performance_data(self.station_id, performance_metrics)

        except Exception as e:
            self.logger.debug(f"Could not record performance metrics: {e}")
            # Fail silently - don't break downloads if performance tracking fails

    def get_station_info(self) -> Dict[str, Any]:
        """Get station information and configuration.

        Returns:
            Dictionary with station configuration
        """
        return {
            "station_id": self.station_id,
            "receiver_type": "PolaRX5",
            "ip": self.ip_number,
            "port": self.ip_port,
            "pasv_mode": self.pasv,
            "configuration": self.station_info,
        }

    @staticmethod
    def is_gz_file(filepath: Union[str, Path]) -> bool:
        """Check if a file is gzipped.

        Args:
            filepath: Path to file to check

        Returns:
            True if file is gzipped, False otherwise
        """
        try:
            with open(filepath, "rb") as f:
                return binascii.hexlify(f.read(2)) == b"1f8b"
        except OSError:
            return False
