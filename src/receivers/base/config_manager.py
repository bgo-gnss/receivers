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
        """Lazy load gps_parser with proper error handling."""
        if self._gps_parser is None:
            try:
                # Try different import strategies for gps_parser
                import gps_parser
                self._gps_parser = gps_parser.ConfigParser()
                self.logger.debug("Successfully loaded gps_parser")
            except ImportError:
                try:
                    # Try with path adjustment for development
                    sys.path.append("../gps_parser/src")
                    import gps_parser
                    self._gps_parser = gps_parser.ConfigParser()
                    self.logger.debug("Successfully loaded gps_parser with path adjustment")
                except ImportError:
                    self.logger.error("Could not import gps_parser - configuration unavailable")
                    raise ImportError(
                        "gps_parser package not found. Please install gps_parser:\\n"
                        "cd ../gps_parser && pip install -e ."
                    )

        return self._gps_parser
    
    def get_data_prepath(self) -> str:
        """Get data prepath from gps_parser configuration.

        Returns:
            Data prepath string from configuration

        Raises:
            Exception: If data_prepath not found in configuration
        """
        parser = self._get_parser()
        return parser.getSystemPath("data_prepath")

    def get_system_path(self, path_name: str) -> str:
        """Get system tool path from gps_parser configuration.

        Args:
            path_name: Name of the system path to retrieve

        Returns:
            Full path to the system tool

        Raises:
            Exception: If path not found in configuration
        """
        parser = self._get_parser()
        return parser.getSystemPath(path_name)

    def get_session_map(self, receiver_type: str = "polarx5") -> Dict[str, Tuple[str, str]]:
        """Get session mapping from receivers configuration.

        Args:
            receiver_type: Receiver type (e.g., 'polarx5', 'netr9', 'netrs', 'g10')

        Returns:
            Dictionary mapping session types to (session_letter, session_path) tuples

        Raises:
            Exception: If session configuration not found
        """
        from ..config.receivers_config import get_receivers_config

        receivers_config = get_receivers_config()

        # Build session map from receivers.cfg configuration
        session_map = {}

        # Get the receiver config which contains session mappings
        receiver_config = receivers_config.get_receiver_config(receiver_type)

        for session_type in ["15s_24hr", "1Hz_1hr", "status_1hr"]:
            mapping_key = f"session_map_{session_type}"
            mapping_key_lower = f"session_map_{session_type.lower()}"

            if mapping_key in receiver_config:
                # Parse format: "letter,path"
                letter, path = receiver_config[mapping_key].split(",", 1)
                session_map[session_type] = (letter.strip(), path.strip())
            elif mapping_key_lower in receiver_config:
                # Try lowercase version (configparser converts keys to lowercase)
                letter, path = receiver_config[mapping_key_lower].split(",", 1)
                session_map[session_type] = (letter.strip(), path.strip())
            else:
                # Fallback to gps_parser if not found in receivers config
                parser = self._get_parser()
                session_config = parser.getSessionConfig(session_type)
                session_map[session_type] = (
                    session_config["session_letter"],
                    session_config["session_path"],
                )
                self.logger.warning(f"Session mapping for {session_type} not found in receivers.cfg, using gps_parser fallback")

        self.logger.debug(f"Loaded {len(session_map)} session configurations from receivers config")
        return session_map

    def get_timeout_config(self, station_id: str) -> Dict[str, int]:
        """Get timeout configuration for a station.

        Args:
            station_id: Station identifier

        Returns:
            Dictionary with timeout values from gps_parser

        Raises:
            Exception: If timeout configuration not found
        """
        parser = self._get_parser()
        return parser.getStationTimeout(station_id)

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