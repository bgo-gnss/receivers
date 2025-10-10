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
        self.tmp_dir = "/tmp/gps_receivers/download/"

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
            router_config = self.station_info["router"]
            receiver_config = self.station_info["receiver"]

            if not router_config.get("ip"):
                raise ConfigurationError(
                    f"Missing router IP for station {self.station_id}"
                )

            # Check for HTTP port
            http_port = receiver_config.get("httpport") or receiver_config.get(
                "receiver_httpport"
            )
            if not http_port:
                self.logger.warning(
                    f"Missing HTTP port for {self.station_id}, using default 8060"
                )
                receiver_config["httpport"] = 8060
            else:
                receiver_config["httpport"] = http_port

            if not receiver_config.get("ftpport"):
                self.logger.warning(
                    f"Missing FTP port for {self.station_id}, using default 21"
                )
                receiver_config["ftpport"] = 21

        except KeyError as e:
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
                success, response, error = self.http_client.get_url(self.endpoints['voltage'])
                if success and response:
                    health_data['voltage'] = self.health_parser.parse_voltage_response(response)
                else:
                    health_data['voltage'] = {"status": "error", "error": error or "No response"}
            except Exception as e:
                health_data['voltage'] = {"status": "error", "error": str(e)}
            
            # Collect temperature information  
            try:
                success, response, error = self.http_client.get_url(self.endpoints['temperature'])
                if success and response:
                    health_data['temperature'] = self.health_parser.parse_temperature_response(response)
                else:
                    health_data['temperature'] = {"status": "error", "error": error or "No response"}
            except Exception as e:
                health_data['temperature'] = {"status": "error", "error": str(e)}
            
            # Collect session information
            try:
                success, response, error = self.http_client.get_url(self.endpoints['sessions'])
                if success and response:
                    health_data['sessions'] = self.health_parser.parse_sessions_response(response)
                else:
                    health_data['sessions'] = {"status": "error", "error": error or "No response"}
            except Exception as e:
                health_data['sessions'] = {"status": "error", "error": str(e)}
                
            # Collect tracking information
            try:
                success, response, error = self.http_client.get_url(self.endpoints['tracking'])
                if success and response:
                    health_data['tracking'] = self.health_parser.parse_tracking_response(response)
                else:
                    health_data['tracking'] = {"status": "error", "error": error or "No response"}
            except Exception as e:
                health_data['tracking'] = {"status": "error", "error": str(e)}
        
        # Determine overall status
        health_data["overall_status"] = self._determine_overall_status(health_data)
        
        return health_data
    
    def test_connection(self) -> Dict[str, Any]:
        """Test connection to NetRS receiver.
        
        Returns:
            Dictionary with connection test results
        """
        # Test HTTP connection
        http_result = self.http_client.test_connection()
        
        # Test FTP connection 
        ftp_result = self.ftp_client.test_connection()
        
        # Combine results
        return {
            "success": http_result.get("success", False) or ftp_result.get("success", False),
            "http": http_result,
            "ftp": ftp_result
        }
    
    def download_data(self, start: Optional[datetime] = None, end: Optional[datetime] = None,
                     session: str = "15s_24hr", ffrequency: str = "24hr", 
                     clean_tmp: bool = True, archive: bool = False, **kwargs) -> Dict[str, Any]:
        """Download data from NetRS receiver.
        
        Args:
            start: Start datetime for download
            end: End datetime for download  
            session: Session type to download
            ffrequency: File frequency (24hr, 1hr, etc.)
            clean_tmp: Whether to clean temporary files
            archive: Whether to archive downloaded files
            **kwargs: Additional arguments
            
        Returns:
            Dictionary with download results
        """
        self.logger.info(f"Starting download for NetRS {self.station_id}")
        
        try:
            # Process time parameters
            if end is None:
                end = currDatetime()
            if start is None:
                start = end.replace(day=end.day-1)  # Default to yesterday
                
            # Generate file lists
            files_dict, archive_files_dict = self._generate_file_lists(
                start, end, session, ffrequency
            )
            
            self.logger.info(f"Missing files: {len(files_dict)}")
            
            # Create local download directory
            local_dir = Path(kwargs.get('local_dir', '/tmp'))
            local_dir.mkdir(parents=True, exist_ok=True)
            
            # Download files via FTP
            downloaded_files = self.ftp_client.download_files(
                files_dict, local_dir, clean_tmp=clean_tmp
            )
            
            # Archive files if requested
            if archive and downloaded_files:
                self._archive_files(downloaded_files, archive_files_dict)
            
            return {
                "station": self.station_id,
                "downloaded_files": downloaded_files,
                "files_requested": len(files_dict),
                "files_downloaded": len(downloaded_files),
                "session": session,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None
            }
            
        except Exception as e:
            self.logger.error(f"Download failed: {e}")
            raise ConnectionError(f"NetRS download failed for {self.station_id}: {e}")
    
    def _generate_file_lists(self, start: datetime, end: datetime, 
                           session: str, ffrequency: str) -> tuple[Dict[str, str], Dict[str, str]]:
        """Generate file lists for NetRS download.
        
        NetRS uses similar filename format to NetR9:
        SSSSDDDF.YYT where SSSS=station, DDD=day of year, F=file seq, YY=year, T=file type
        
        Args:
            start: Start datetime
            end: End datetime
            session: Session type
            ffrequency: File frequency
            
        Returns:
            Tuple of (remote_files_dict, archive_files_dict)
        """
        # Map session frequency to gtimes frequency
        frequency_mapping = {
            "24hr": "1D",   # Daily files
            "1hr": "1H",    # Hourly files
        }
        gt_frequency = frequency_mapping.get(ffrequency, "1D")
        
        # Generate datetime list using PolaRX5 pattern
        datelist = gt.datepathlist(
            "#datelist",
            gt_frequency, 
            starttime=start,
            endtime=end,
            datelist=[],
            closed="both"
        )
        
        # Generate remote file paths (NetRS specific format, similar to NetR9)
        files_dict = {}
        archive_files_dict = {}
        
        for dt in datelist:
            # NetRS filename format: SSSSDDDF.YYT (where SSSS=station, DDD=day of year, F=file seq, YY=year, T=file type)
            doy = dt.timetuple().tm_yday
            year = dt.strftime('%y')
            
            if ffrequency == "24hr":
                # Daily files: STATIONDDF.YYT
                filename = f"{self.station_id}{doy:03d}0.{year}T"
                remote_dir = f"/Internal/{dt.strftime('%Y')}/{dt.strftime('%m')}/T/"
            elif ffrequency == "1hr":
                # Hourly files: STATIONDDF.YYT (F = hour)
                filename = f"{self.station_id}{doy:03d}{dt.hour}.{year}T"
                remote_dir = f"/Internal/{dt.strftime('%Y')}/{dt.strftime('%m')}/T/"
            else:
                # Default to daily
                filename = f"{self.station_id}{doy:03d}0.{year}T"
                remote_dir = f"/Internal/{dt.strftime('%Y')}/{dt.strftime('%m')}/T/"
            
            files_dict[filename] = remote_dir
            
            # Generate archive path using standard strftime
            archive_path = f"{self.data_prepath}{self.station_id}/{session}/raw/{filename}"
            archive_files_dict[filename] = archive_path
        
        return files_dict, archive_files_dict
    
    def _archive_files(self, downloaded_files: List[str], archive_files_dict: Dict[str, str]):
        """Archive downloaded files to their final locations.
        
        Args:
            downloaded_files: List of downloaded file paths
            archive_files_dict: Dictionary mapping filenames to archive paths
        """
        for file_path in downloaded_files:
            filename = Path(file_path).name
            if filename in archive_files_dict:
                archive_path = Path(archive_files_dict[filename])
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                
                try:
                    import shutil
                    shutil.move(file_path, archive_path)
                    self.logger.info(f"Archived {filename} to {archive_path}")
                except Exception as e:
                    self.logger.error(f"Failed to archive {filename}: {e}")
    
    def _determine_overall_status(self, health_data: Dict[str, Any]) -> str:
        """Determine overall status based on health data.
        
        Args:
            health_data: Health data dictionary
            
        Returns:
            Overall status: 'healthy', 'warning', or 'critical'
        """
        # If no connection, it's critical
        if not health_data.get("connection", {}).get("success", False):
            return "critical"
        
        # Check individual component statuses
        critical_count = 0
        warning_count = 0
        
        components = ['voltage', 'temperature', 'sessions', 'tracking']
        for component in components:
            status = health_data.get(component, {}).get("status", "unknown")
            if status in ["error", "critical"]:
                critical_count += 1
            elif status == "warning":
                warning_count += 1
        
        # Determine overall status
        if critical_count > 0:
            return "critical"
        elif warning_count > 0:
            return "warning"
        else:
            return "healthy"
    
    def get_status_data(self, start: Optional[datetime] = None, end: Optional[datetime] = None,
                       **kwargs) -> Dict[str, Any]:
        """Download status/health data files for NetRS.
        
        NetRS receivers may have specific status file formats and endpoints.
        This method handles status data collection similar to NetR9.
        
        Args:
            start: Start datetime
            end: End datetime
            **kwargs: Additional arguments
            
        Returns:
            Dictionary with download results and health data files
        """
        return self.download_data(
            start=start, end=end, session="status_1hr", ffrequency="1H", **kwargs
        )
    
    def get_connection_status(self) -> Dict[str, Any]:
        """Check connection status to receiver.
        
        Returns:
            Dictionary with connection status information
        """
        return self.test_connection()
    
    def get_health_status(self) -> Dict[str, Any]:
        """Get comprehensive health status of receiver.
        
        Returns:
            Dictionary with health status information  
        """
        return self.get_health()
    
    def get_station_info(self) -> Dict[str, Any]:
        """Get station information and configuration.
        
        Returns:
            Dictionary with station information
        """
        return {
            "station_id": self.station_id,
            "receiver_type": "NetRS", 
            "config": self.station_config,
            "data_prepath": self.data_prepath
        }
    
    def close(self):
        """Clean up connections and resources."""
        if hasattr(self, 'http_client'):
            self.http_client.close()
        # FTP client doesn't need explicit closing