"""Configuration management utilities for GPS receivers.

Provides shared configuration access methods that all receiver implementations
can use to get paths, session mappings, and system tool locations from gps_parser.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple


class ConfigManager:
    """Shared configuration manager for all GPS receivers.
    
    Handles configuration retrieval from gps_parser with fallback strategies
    for data paths, system tool paths, and session configurations.
    """
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize configuration manager.
        
        Args:
            logger: Optional logger instance, creates default if None
        """
        self.logger = logger or logging.getLogger("receivers.config")
        self._gps_parser = None
    
    def _get_parser(self):
        """Lazy load gps_parser to avoid import issues."""
        if self._gps_parser is None:
            try:
                # Try different import strategies for gps_parser
                import gps_parser
                self._gps_parser = gps_parser.ConfigParser()
            except ImportError:
                try:
                    # Try with path adjustment
                    sys.path.append("../gps_parser/src")
                    import gps_parser
                    self._gps_parser = gps_parser.ConfigParser()
                except ImportError:
                    self.logger.warning("Could not import gps_parser - using fallbacks")
                    self._gps_parser = None
        
        return self._gps_parser
    
    def get_data_prepath(self) -> str:
        """Get data prepath from gps_parser configuration or environment.

        Priority:
        1. gps_parser configuration file setting
        2. Environment variable DATA_PREPATH
        3. Default ./data/

        Returns:
            Data prepath string
        """
        try:
            parser = self._get_parser()
            if parser:
                return parser.getSystemPath("data_prepath")
        except Exception as e:
            self.logger.debug(f"Could not get data_prepath from gps_parser: {e}")

        # Fallback to environment variable or default
        fallback = os.getenv("DATA_PREPATH", "./data/")
        self.logger.debug(f"Using fallback data prepath: {fallback}")
        return fallback

    def get_system_path(self, path_name: str) -> str:
        """Get system tool path from gps_parser configuration.

        Args:
            path_name: Name of the system path to retrieve

        Returns:
            Full path to the system tool
        """
        try:
            parser = self._get_parser()
            if parser:
                return parser.getSystemPath(path_name)
        except Exception as e:
            self.logger.debug(f"Could not load {path_name} from gps_parser: {e}")

        # Fallback paths for critical tools
        fallback_paths = {
            "sbf2rin_path": "/home/gpsops/bin/sbf2rin",
            "teqc_path": "/home/gpsops/bin/teqc", 
            "bin2asc_path": "/opt/rxtools/bin/bin2asc",
            "sbf2asc_path": "/opt/rxtools/bin/sbf2asc",
            "sbfanalyzer_path": "/opt/rxtools/bin/sbfanalyzer",
            "sbfconverter_path": "/opt/rxtools/bin/sbfconverter",
        }

        fallback_path = fallback_paths.get(path_name, f"/usr/local/bin/{path_name}")
        self.logger.info(f"Using fallback path for {path_name}: {fallback_path}")
        return fallback_path

    def get_session_map(self) -> Dict[str, Tuple[str, str]]:
        """Get session mapping from gps_parser configuration.

        Returns:
            Dictionary mapping session types to (session_letter, session_path) tuples
        """
        try:
            parser = self._get_parser()
            if parser:
                # Build session map from gps_parser configuration
                session_map = {}
                for session_type in ["15s_24hr", "1Hz_1hr", "status_1hr"]:
                    try:
                        session_config = parser.getSessionConfig(session_type)
                        session_map[session_type] = (
                            session_config["session_letter"],
                            session_config["session_path"],
                        )
                    except Exception as e:
                        self.logger.debug(
                            f"Could not load session config for {session_type}: {e}"
                        )

                # If we got any session configurations, return them
                if session_map:
                    self.logger.debug(
                        f"Loaded {len(session_map)} session configurations from gps_parser"
                    )
                    return session_map
        except Exception as e:
            self.logger.debug(f"Could not load session mapping from gps_parser: {e}")

        # Fallback to hardcoded session mapping
        self.logger.debug("Using fallback session mapping")
        return {
            "15s_24hr": ("a", "LOG1_15s_24hr"),  # Daily 15-second data
            "1Hz_1hr": ("b", "LOG2_1Hz_1hr"),  # Hourly 1Hz data  
            "status_1hr": ("b", "LOG5_status_1hr"),  # Hourly status files
        }

    def get_timeout_config(self, station_id: str, ip: str) -> str:
        """Get timeout category for a station.
        
        Args:
            station_id: Station identifier
            ip: Station IP address
            
        Returns:
            Timeout category: 'fixed_wired', 'mobile', or 'very_remote'
        """
        try:
            parser = self._get_parser()
            if parser:
                return parser.getStationTimeout(station_id)
        except Exception as e:
            self.logger.debug(f"Could not get timeout config for {station_id}: {e}")
        
        # Fallback timeout categorization based on IP ranges
        if ip.startswith("10.4.") or ip.startswith("10.6."):
            return "mobile"
        elif "gps.vedur.is" in ip:
            return "fixed_wired"
        else:
            return "mobile"  # Conservative default

    def get_ftp_mode(self, station_id: str, ip: str) -> str:
        """Get FTP mode for a station.
        
        Args:
            station_id: Station identifier  
            ip: Station IP address
            
        Returns:
            FTP mode: 'passive' or 'active'
        """
        try:
            parser = self._get_parser()
            if parser:
                return parser.getStationFtpMode(station_id, ip)
        except Exception as e:
            self.logger.debug(f"Could not get FTP mode for {station_id}: {e}")
        
        # Default to passive mode (safer for firewalls)
        return "passive"


# Singleton instance for shared use
_config_manager = None

def get_config_manager(logger: Optional[logging.Logger] = None) -> ConfigManager:
    """Get shared configuration manager instance.
    
    Args:
        logger: Optional logger instance
        
    Returns:
        ConfigManager instance
    """
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager(logger)
    return _config_manager