"""Leica GNSS receiver implementation.

Leica GNSS receivers are professional GPS receivers that typically use HTTP-based
management interfaces. This implementation provides basic connectivity and health
monitoring capabilities.
"""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import gtimes.timefunc as gt
from gtimes.timefunc import currDatetime

from ..base.receiver import BaseReceiver
from ..base.exceptions import ConfigurationError, ConnectionError


class Leica(BaseReceiver):
    """Leica GNSS receiver implementation.
    
    Minimal implementation for Leica GNSS receivers providing basic connectivity
    and health monitoring. Since there's only one Leica station, this is a
    simple implementation that can be expanded if needed.
    """
    
    def __init__(self, station_id: str, station_config: Dict[str, Any]):
        """Initialize Leica GNSS receiver.
        
        Args:
            station_id: Station identifier
            station_config: Station configuration dictionary
        """
        super().__init__(station_id, station_config)
        self.logger = logging.getLogger(f"receivers.leica.gnss.{self.station_id}")
        
        # Extract connection details from configuration
        self.ip = station_config["router"]["ip"]
        receiver_config = station_config.get("receiver", {})
        self.http_port = receiver_config.get("httpport", 8060)
        self.ftp_port = receiver_config.get("ftpport", 21)
        
        self.logger.info(f"Initialized Leica GNSS receiver for {self.station_id} at {self.ip}:{self.http_port}")
    
    def get_health(self, **kwargs) -> Dict[str, Any]:
        """Collect basic health data from Leica receiver.
        
        Since Leica receivers may have different interfaces than Trimble,
        this provides a basic health check implementation.
        
        Returns:
            Dictionary with health data and overall status
        """
        self.logger.debug(f"Collecting health data from Leica {self.station_id}")
        
        # Test basic connectivity
        conn_test = self.test_connection()
        
        # Basic health data structure
        health_data = {
            "station": self.station_id,
            "receiver_type": "Leica",
            "timestamp": currDatetime().isoformat(),
            "connection": conn_test,
            "status": "unknown"
        }
        
        # Determine overall status based on connectivity
        if conn_test.get("success", False):
            health_data["overall_status"] = "healthy"
        else:
            health_data["overall_status"] = "critical"
        
        return health_data
    
    def test_connection(self) -> Dict[str, Any]:
        """Test connection to Leica receiver.
        
        Performs basic connectivity tests to determine if receiver is accessible.
        
        Returns:
            Dictionary with connection test results
        """
        start_time = time.time()
        
        # Try to ping or connect to receiver
        success = False
        error_message = None
        
        try:
            import subprocess
            # Ping test (3 packets to tolerate lossy links)
            result = subprocess.run(
                ['ping', '-c', '3', '-W', '2', self.ip],
                capture_output=True,
                text=True,
                timeout=8
            )
            success = (result.returncode == 0)
            if not success:
                error_message = f"Ping failed: {result.stderr}"
        except Exception as e:
            error_message = f"Connection test failed: {e}"
        
        duration = time.time() - start_time
        
        return {
            "success": success,
            "duration": duration,
            "server": f"{self.ip}:{self.http_port}",
            "error": error_message
        }
    
    def download_data(self, start: Optional[datetime] = None, end: Optional[datetime] = None,
                     session: str = "15s_24hr", ffrequency: str = "24hr",
                     clean_tmp: bool = True, archive: bool = False, **kwargs) -> Dict[str, Any]:
        """Download data from Leica receiver.
        
        Basic implementation for Leica data download. Since Leica receivers
        may have different file formats and protocols, this is a placeholder
        implementation.
        
        Args:
            start: Start datetime for download
            end: End datetime for download
            session: Session type to download
            ffrequency: File frequency
            clean_tmp: Whether to clean temporary files
            archive: Whether to archive downloaded files
            **kwargs: Additional arguments
            
        Returns:
            Dictionary with download results
        """
        self.logger.info(f"Download requested for Leica {self.station_id}")
        
        # For now, return empty result since Leica protocol needs investigation
        self.logger.warning(f"Leica download not yet implemented - need to investigate {self.station_id} protocols")
        
        return {
            "station": self.station_id,
            "downloaded_files": [],
            "files_requested": 0,
            "files_downloaded": 0,
            "session": session,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "status": "not_implemented",
            "message": "Leica download protocol needs investigation"
        }
    
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
            "receiver_type": "Leica",
            "config": self.station_config,
            "data_prepath": self.data_prepath,
            "ip": self.ip,
            "http_port": self.http_port,
            "ftp_port": self.ftp_port
        }
    
    def get_status_data(self, start: Optional[datetime] = None, end: Optional[datetime] = None,
                       **kwargs) -> Dict[str, Any]:
        """Download status/health data files for Leica.
        
        Args:
            start: Start datetime
            end: End datetime
            **kwargs: Additional arguments
            
        Returns:
            Dictionary with download results and health data files
        """
        return self.download_data(
            start=start, end=end, session="status", ffrequency="1H", **kwargs
        )
    
    def close(self):
        """Clean up connections and resources."""
        # No persistent connections to clean up for basic implementation
        pass