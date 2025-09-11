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
        # data_prepath already available via BaseReceiver initialization

        # System paths from ConfigManager
        self.base_path = self.config_manager.get_system_path("receiver_base_path")
        self.sbf2rin_path = self.config_manager.get_system_path("sbf2rin_path")
        self.teqc_path = self.config_manager.get_system_path("teqc_path")

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

    def download_data(
        self,
        start: Optional[Union[datetime, str]] = None,
        end: Optional[Union[datetime, str]] = None,
        session: str = "15s_24hr",
        ffrequency: str = "1D",
        afrequency: str = "15s",
        clean_tmp: bool = True,
        sync: bool = False,
        compression: str = ".gz",
        archive: bool = True,
        immediate_archive: bool = True,
        tmp_dir: str = "/home/bgo/tmp/download/",
        predir: str = "/DSK2/SSN/",
        loglevel: int = logging.WARNING,
    ) -> Dict[str, Any]:
        """Download data from PolaRX5 receiver.

        This is the main download function that handles file synchronization
        from the receiver to the local archive.

        Args:
            start: Start time for download period
            end: End time for download period
            session: Data session type
            ffrequency: File frequency (e.g., '1D', '1H')
            afrequency: Acquisition frequency
            clean_tmp: Clean temporary directory before download
            sync: Whether to actually sync files (False for dry run)
            compression: File compression type
            archive: Whether to archive downloaded files
            immediate_archive: If True, archive each file immediately after download (fault-tolerant)
                             If False, archive all files after download completion (efficient)
            tmp_dir: Temporary download directory
            predir: Remote directory prefix
            loglevel: Logging level

        Returns:
            Dictionary with download results and file information
        """
        # Set logger level
        self.logger.setLevel(loglevel)

        # Set up directories
        tmp_dir_path = Path(tmp_dir) / self.station_id
        tmp_dir_path.mkdir(parents=True, exist_ok=True)

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

        # Generate file lists - special handling for hourly sessions
        if ffrequency == "1H":
            # For hourly sessions, manually generate hourly timestamps
            from datetime import timedelta

            file_datetime_list = []
            current = start
            while current <= end:
                file_datetime_list.append(current)
                current += timedelta(hours=1)
            self.logger.info(f"Generated {len(file_datetime_list)} hourly timestamps")
        else:
            file_datetime_list = gt.datepathlist(
                "#datelist",
                ffrequency,
                starttime=start,
                endtime=end,
                datelist=[],
                closed="both",
            )

        # Create archive and remote file paths using configurable prepath
        archive_format = f"{self.data_prepath}%Y/#b/{self.station_id}/{session}/raw/{self.station_id}%Y%m%d%H00a.sbf{compression}"
        archive_file_list = gt.datepathlist(
            archive_format, ffrequency, datelist=file_datetime_list, closed="both"
        )

        igs_format = f"{self.station_id}#Rin2_{compression}"
        igs_file_list = gt.datepathlist(
            igs_format, ffrequency, datelist=file_datetime_list, closed="both"
        )

        file_date_dict = dict(
            zip(file_datetime_list, zip(archive_file_list, igs_file_list))
        )

        # Find missing files and incomplete files (like getSeptentrio3 logic)
        missing_file_dict = {}
        incomplete_file_dict = {}

        for key, value in file_date_dict.items():
            archive_file = value[0]
            if not os.path.isfile(archive_file):
                # File doesn't exist - needs download
                missing_file_dict[key] = value
            else:
                # File exists - check if it's incomplete by attempting to get remote size
                if sync:  # Only check completeness if we plan to sync
                    try:
                        # Try to get remote file size for comparison
                        remote_path = self._get_remote_file_path(key, session)
                        # Note: We'll check completeness during actual download
                        # For now, assume existing files are complete unless proven otherwise
                        pass
                    except Exception:
                        # Can't verify completeness without FTP connection
                        pass

        # Combine missing and incomplete files for processing
        all_missing_files = {**missing_file_dict, **incomplete_file_dict}

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
                self.logger.info(f"✅ Connection test OK: {self.ip_number}:{self.ip_port}")
            except Exception as e:
                # Connection failed - now run diagnostics to understand why
                from ..base.download_diagnostics import DownloadDiagnosticsAnalyzer
                diagnostics = DownloadDiagnosticsAnalyzer(self.station_id, self.logger)
                
                network_check = diagnostics.classify_network_failure(self.ip_number)
                
                if network_check['classification'] == 'invalid_ip':
                    self.logger.critical(f"❌ INVALID IP: {self.ip_number} - likely configuration typo")
                    self.logger.critical(f"💡 SUGGESTED FIX: Check station configuration for correct IP address")
                elif 'connection refused' in str(e).lower():
                    self.logger.error(f"❌ FTP CONNECTION REFUSED: {self.ip_number}:{self.ip_port}")
                    if network_check['classification'] == 'network_ok':
                        self.logger.error(f"💡 Router responds but FTP refused - Wrong port ({self.ip_port}) or port forwarding issue")
                    else:
                        self.logger.error(f"💡 LIKELY ISSUE: Wrong FTP port ({self.ip_port}) or port forwarding not configured")
                    self.logger.error(f"💡 SUGGESTED ACTION: Verify ftpport={self.ip_port} in station configuration")
                else:
                    self.logger.warning(f"⚠️ Connection test failed: {e}")
                    if network_check.get('analysis'):
                        self.logger.info(f"Network analysis: {network_check['analysis']}")

        try:
            if sync:
                downloaded_files_dict = self._sync_missing_files(
                    all_missing_files,
                    tmp_dir_path,
                    session,
                    predir,
                    ffrequency,
                    clean_tmp,
                    archive,
                    immediate_archive,
                    compression,
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
                performance_metrics.update({
                    "success": False,
                    "duration": time.time() - start_time,
                    "error_type": "configuration_error",
                    "error_category": e.category.value if hasattr(e, 'category') else "unknown",
                    "validation_needed": True
                })
                self._record_performance_metrics(performance_metrics)
                
                return {
                    "status": "configuration_error",
                    "error": str(e),
                    "error_type": "invalid_ip_range",
                    "suggested_fix": getattr(e, 'suggested_fix', 'Check station configuration'),
                    "files_checked": len(file_date_dict),
                    "files_missing": len(all_missing_files),
                    "files_downloaded": 0,
                    "duration": time.time() - start_time,
                    "validation_triggered": True
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
                found_files = list(downloaded_files_dict.keys()) if 'downloaded_files_dict' in locals() else []
                
                # Create receiver response context
                receiver_response = {
                    'connection_ok': getattr(self, '_last_connection_time', 0) > 0,
                    'ftp_connected': False,  # Connection failed, so FTP never connected
                    'performance_metrics': performance_metrics
                }
                
                # Use enhanced failure handling
                failure_result = self.handle_download_failure(
                    expected_files=expected_files,
                    found_files=found_files,
                    session_type=session,
                    date_range=f"{start} to {end}",
                    error=e,
                    receiver_response=receiver_response
                )
                
                # Log the failure analysis
                if failure_result and 'failure_analysis' in failure_result:
                    analysis = failure_result['failure_analysis']
                    self.logger.info(f"Failure analysis: {analysis.get('analysis', 'No analysis available')}")
                    if analysis.get('validation_trigger'):
                        self.logger.warning(f"Validation triggered for {self.station_id}")
                
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

    def _process_time_parameters(self, start, end, session, ffrequency):
        """Process and validate time parameters."""
        # Handle hourly vs daily sessions
        hoursession = re.compile(r"1h", re.IGNORECASE)
        is_hourly = hoursession.search(session)

        if ffrequency.lower() == "1h" or is_hourly:
            # Hourly data processing
            if end is None:
                end = datetime.now() - timedelta(hours=1)
            if isinstance(end, str):
                end = datetime.fromisoformat(end)
            end = end.replace(minute=0, second=0, microsecond=0)

            if start is None:
                start = end - timedelta(hours=24)
            if isinstance(start, str):
                start = datetime.fromisoformat(start)
            start = start.replace(minute=0, second=0, microsecond=0)
        else:
            # Daily data processing
            if end is None:
                end = currDatetime(-1)
            if isinstance(end, str):
                end = datetime.fromisoformat(end)
            end = end.date()

            if start is None:
                start = end - timedelta(days=10)
            if isinstance(start, str):
                start = datetime.fromisoformat(start)
            start = start.date()

        return start, end

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
        clean_tmp,
        archive,
        immediate_archive,
        compression=".gz",
    ):
        """Sync missing files from receiver to local archive."""
        # Get session info
        if session not in self.session_map:
            raise ConfigurationError(f"Unknown session type: {session}")

        session_info = self.session_map[session][1]
        remote_format = f"{self.base_path}{session_info}/%y%j/"
        remote_path_list = gt.datepathlist(
            remote_format,
            ffrequency,
            datelist=list(missing_file_dict.keys()),
            closed="both",
        )

        # Generate remote filenames using RINEX format (like getSeptentrio3)
        # Remote files use RINEX naming: STATION#Rin2_compression format
        igs_format = f"{self.station_id}#Rin2_{compression}"
        igs_file_name_list = gt.datepathlist(
            igs_format,
            ffrequency,
            datelist=list(missing_file_dict.keys()),
            closed="both",
        )

        # Create download dictionary with RINEX filenames and remote paths
        download_file_dict = dict(zip(igs_file_name_list, remote_path_list))

        # Connect and download
        ftp = self._ftp_open_connection()
        if not ftp:
            raise ConnectionError(
                f"Could not connect to {self.ip_number}:{self.ip_port}"
            )

        try:
            downloaded_files = self._ftp_download(
                download_file_dict, tmp_dir, clean_tmp=clean_tmp, ftp=ftp,
                archive=archive, immediate_archive=immediate_archive, missing_file_dict=missing_file_dict
            )

            downloaded_files_dict = dict(zip(missing_file_dict, downloaded_files))

            # Archive files (only if not using immediate archiving)
            if downloaded_files_dict and archive and not immediate_archive:
                self._archive_files(downloaded_files_dict, missing_file_dict)

            return downloaded_files_dict

        finally:
            ftp.close()

    def _ftp_open_connection(self, timeout: Optional[int] = None) -> Optional[FTP]:
        """Open FTP connection to receiver with optimistic approach - try first, diagnose on failure."""
        connection_start = time.time()
        
        # Optimistic approach: Try connection directly first
        try:
            self.logger.info(f"Attempting FTP connection to {self.ip_number}:{self.ip_port}...")
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
            self.logger.info(f"Connection failed after {connection_time:.2f}s - running diagnostics...")
            
            from ..base.download_diagnostics import DownloadDiagnosticsAnalyzer
            diagnostics = DownloadDiagnosticsAnalyzer(self.station_id, self.logger)
            
            # Quick network classification to understand the failure
            network_check = diagnostics.classify_network_failure(self.ip_number)
            
            # If it's an invalid IP range, provide critical error immediately
            if network_check['classification'] == 'invalid_ip':
                self.logger.critical(f"❌ INVALID IP: {self.ip_number} - likely configuration typo")
                self.logger.critical(f"💡 SUGGESTED FIX: Check station configuration for correct IP address")
                
                from ..base.exceptions import ConfigurationError, FailureCategory
                raise ConfigurationError(
                    message=f"Invalid IP range {self.ip_number} - likely configuration typo",
                    station_id=self.station_id,
                    category=FailureCategory.DNS_FAILURE,
                    config_field="router_ip",
                    actual_value=self.ip_number,
                    suggested_fix="Verify IP address in station configuration"
                )
            
            # Provide intelligent error analysis based on failure type and network status
            if 'connection refused' in error_str:
                self.logger.error(f"❌ FTP CONNECTION REFUSED: {self.ip_number}:{self.ip_port}")
                if network_check['classification'] == 'network_ok':
                    self.logger.error(f"💡 Router responds but FTP refused - Wrong port ({self.ip_port}) or port forwarding issue")
                else:
                    self.logger.error(f"💡 LIKELY ISSUE: Wrong FTP port ({self.ip_port}) or port forwarding not configured")
                self.logger.error(f"💡 SUGGESTED ACTION: Verify ftpport={self.ip_port} in station configuration")
            elif 'timeout' in error_str or 'timed out' in error_str:
                if network_check['classification'] == 'network_ok':
                    self.logger.error(f"⚠️ RECEIVER TIMEOUT: Router responds but receiver doesn't on FTP port")
                    self.logger.error(f"💡 LIKELY ISSUE: Receiver down, ethernet broken, or firewall blocking FTP")
                else:
                    self.logger.error(f"⚠️ NETWORK TIMEOUT: {network_check['analysis']}")
            else:
                self.logger.error(f"Connection failed: {e}")
                self.logger.info(f"Network analysis: {network_check['analysis']}")
            
            self._last_connection_time = connection_time
            return None

    def _ftp_download(self, files_dict, local_dir, clean_tmp=True, ftp=None, 
                     archive=True, immediate_archive=True, missing_file_dict=None):
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
            if clean_tmp and local_file.exists():
                local_file.unlink()

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

                # Handle existing partial files and size mismatches
                offset = 0
                if local_file.exists():
                    local_file_size = local_file.stat().st_size

                    # Check for size mismatch - force re-download if mismatch detected
                    if (
                        remote_file_size is not None
                        and local_file_size == remote_file_size
                    ):
                        # File is complete and matches remote size
                        self.logger.info(
                            f"✅ File {file_name} already complete ({local_file_size:,} bytes)"
                        )
                        
                        # Immediate archiving if enabled
                        if immediate_archive and archive and missing_file_dict:
                            # Find the datetime key for this file
                            file_datetime = None
                            for dt_key, (arch_path, _) in missing_file_dict.items():
                                if file_name in arch_path or os.path.basename(arch_path).startswith(file_name[:8]):
                                    file_datetime = dt_key
                                    break
                            
                            if file_datetime and self._archive_single_file(str(local_file), file_datetime, missing_file_dict):
                                # File successfully archived - add archive path to downloaded files
                                downloaded_files.append(missing_file_dict[file_datetime][0])
                            else:
                                # Archive failed - add tmp file path
                                downloaded_files.append(str(local_file))
                        else:
                            # No immediate archiving - add tmp file path
                            downloaded_files.append(str(local_file))
                        continue
                    elif (
                        remote_file_size is not None
                        and local_file_size > remote_file_size
                    ):
                        # Local file is larger than remote - corruption detected
                        self.logger.warning(
                            f"🔧 Local file {file_name} is larger than remote ({local_file_size:,} > {remote_file_size:,} bytes)"
                        )
                        if clean_tmp:
                            self.logger.info(
                                f"   Re-downloading due to size mismatch (clean_tmp=True)"
                            )
                            local_file.unlink()
                            offset = 0
                        else:
                            self.logger.info(
                                f"   Keeping corrupted file (clean_tmp=False)"
                            )
                            continue
                    elif not clean_tmp:
                        # Resume partial download (default behavior)
                        offset = local_file_size
                        self.logger.info(
                            f"📄 Resuming download from {offset:,} bytes (clean_tmp=False)"
                        )
                    else:
                        # clean_tmp=True - start fresh
                        self.logger.info(f"🔄 Restarting download (clean_tmp=True)")
                        local_file.unlink()
                        offset = 0

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
                        
                        # Immediate archiving if enabled
                        if immediate_archive and archive and missing_file_dict:
                            # Find the datetime key for this file
                            file_datetime = None
                            for dt_key, (arch_path, _) in missing_file_dict.items():
                                if file_name in arch_path or os.path.basename(arch_path).startswith(file_name[:8]):
                                    file_datetime = dt_key
                                    break
                            
                            if file_datetime and self._archive_single_file(str(local_file), file_datetime, missing_file_dict):
                                # File successfully archived - add archive path to downloaded files
                                downloaded_files.append(missing_file_dict[file_datetime][0])
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
                    with open(local_file, "ab") as f:
                        ftp.retrbinary(f"RETR {remote_file}", f.write, rest=offset)

                    # Without remote size, just check if file grew
                    if local_file.exists() and local_file.stat().st_size > offset:
                        downloaded_files.append(str(local_file))
                        self.logger.info(
                            f"Successfully downloaded {file_name} (size validation not available)"
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
            with open(local_file, "ab") as f:
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
                with open(local_file, "ab") as f:

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

    def _archive_single_file(self, tmp_file_path: str, file_datetime, missing_file_dict) -> bool:
        """Archive a single file immediately after download.
        
        Args:
            tmp_file_path: Path to the temporary downloaded file
            file_datetime: The datetime key for this file
            missing_file_dict: Dict mapping datetime to (archive_path, remote_path) tuples
            
        Returns:
            True if archiving succeeded, False otherwise
        """
        if not os.path.isfile(tmp_file_path):
            self.logger.warning(f"Cannot archive - file not found: {tmp_file_path}")
            return False
            
        tmp_file_size = os.path.getsize(tmp_file_path)
        destination = missing_file_dict[file_datetime][0]
        archive_path, archive_filename = os.path.split(destination)
        
        # Create archive directory
        os.makedirs(archive_path, exist_ok=True)
        
        # Check if archive file already exists
        if os.path.isfile(destination):
            archive_file_size = os.path.getsize(destination)
            if tmp_file_size == archive_file_size:
                self.logger.warning(
                    f"Archive file already exists with same size ({tmp_file_size:,} bytes): {archive_filename}"
                )
                # Remove tmp file and consider this a success
                os.unlink(tmp_file_path)
                return True
        
        # Atomic move to archive location
        self.logger.info(f"📦 Archiving {archive_filename} ({tmp_file_size:,} bytes)")
        try:
            os.rename(tmp_file_path, destination)
            
            # Verify successful archive
            if os.path.isfile(destination):
                archive_file_size = os.path.getsize(destination)
                if tmp_file_size == archive_file_size:
                    self.logger.info(f"✅ Archived to: {destination}")
                    return True
                else:
                    self.logger.error(
                        f"❌ Archive size mismatch: expected {tmp_file_size:,}, got {archive_file_size:,}"
                    )
                    return False
            else:
                self.logger.error(f"❌ Archive failed: destination file not found")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ Archive error: {e}")
            # Clean up tmp file on failure
            if os.path.isfile(tmp_file_path):
                os.unlink(tmp_file_path)
            return False

    def _archive_files(self, downloaded_files_dict, missing_file_dict):
        """Move downloaded files to archive locations with getSeptentrio3 naming convention."""
        archived_count = 0
        for ddate, tmp_file in downloaded_files_dict.items():
            if not os.path.isfile(tmp_file):
                continue

            tmp_file_size = os.path.getsize(tmp_file)
            self.logger.info(
                f"File to archive {os.path.basename(tmp_file)} ({tmp_file_size:,} bytes)"
            )

            destination = missing_file_dict[ddate][0]
            archive_path, archive_filename = os.path.split(destination)

            # Archive files use full SBF format like getSeptentrio3:
            # ORFC2490.25_.gz -> ORFC202509060000a.sbf.gz
            # The destination path is already correctly generated by gtimes.datepathlist

            os.makedirs(archive_path, exist_ok=True)

            if os.path.isfile(destination):
                # Archive file already exists - check sizes (like getSeptentrio3)
                archive_file_size = os.path.getsize(destination)
                if tmp_file_size == archive_file_size:
                    self.logger.warning(
                        f"Files dated {ddate}:\n   {tmp_file} and {destination}\n"
                        f"   have the same size {tmp_file_size:,} bytes. Aborting archive."
                    )
                    continue

            # Atomic move to archive location (like getSeptentrio3)
            self.logger.info(
                f"Move file dated {ddate} from {tmp_file} to {destination}"
            )
            try:
                os.rename(tmp_file, destination)

                # Verify successful archive (like getSeptentrio3)
                if os.path.isfile(destination):
                    archive_file_size = os.path.getsize(destination)
                    if tmp_file_size == archive_file_size:
                        self.logger.info(
                            f"✅ File successfully archived ({archive_file_size:,} bytes)"
                        )
                        self.logger.info(f"   Final location: {destination}")
                        archived_count += 1
                        # tmp file removed by os.rename (atomic move)
                    else:
                        self.logger.error(
                            f"❌ Archive size mismatch: expected {tmp_file_size:,}, got {archive_file_size:,}"
                        )
                        # Try to clean up if archive failed
                        if os.path.isfile(tmp_file):
                            os.unlink(tmp_file)
                            self.logger.info(
                                f"🧹 Cleaned up failed tmp file: {tmp_file}"
                            )
                else:
                    self.logger.error(f"❌ Archive failed: destination file not found")
                    # Clean up tmp file on failure
                    if os.path.isfile(tmp_file):
                        os.unlink(tmp_file)
                        self.logger.info(f"🧹 Cleaned up failed tmp file: {tmp_file}")

            except Exception as e:
                self.logger.error(f"❌ Failed to move {tmp_file} to {destination}: {e}")
                # Clean up tmp file on failure
                if os.path.isfile(tmp_file):
                    try:
                        os.unlink(tmp_file)
                        self.logger.info(f"🧹 Cleaned up failed tmp file: {tmp_file}")
                    except Exception as cleanup_e:
                        self.logger.error(
                            f"❌ Failed to cleanup tmp file {tmp_file}: {cleanup_e}"
                        )

        # Clean up empty tmp directories
        self._cleanup_empty_tmp_directories()

        return archived_count

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
        **kwargs,
    ) -> Dict[str, Any]:
        """Download health data (status_1hr session) from PolaRX5 receiver.

        Uses LOG5_status_1hr session containing binary health/status files
        that need conversion to ASCII format for analysis.

        Returns:
            Dictionary with download results and health data files
        """
        return self.download_data(
            start=start, end=end, session="status_1hr", ffrequency="1H", **kwargs
        )

    def get_health_archive_path(self, timestamp: datetime) -> str:
        """Generate health data archive path using gtimes datepath pattern.

        Format: {prepath}/YYYY/#b/STATION/status/station_status_timestamp.sbf

        Args:
            timestamp: datetime for the health data file

        Returns:
            Full path to health archive file
        """
        # Use gtimes-compatible format for health data
        health_format = f"{self.data_prepath}%Y/#b/{self.station_id}/status/{self.station_id.lower()}_status_%Y%m%d%H%M.sbf"
        return gt.datepathlist(
            health_format, "1H", datelist=[timestamp], closed="both"
        )[0]

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
