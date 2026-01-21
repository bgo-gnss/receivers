"""Receivers configuration management.

This module handles loading and managing configuration for the receivers package,
including archive paths, session types, and receiver-specific settings.
"""

import ast
import configparser
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class IcingaThresholds:
    """Icinga monitoring thresholds and TTL values.

    All values can be configured in receivers.cfg [icinga_thresholds] section.
    """

    # TTL values (seconds)
    ttl_health_checks: int = 14400  # 4 hours
    ttl_file_status: int = 360000  # 100 hours
    ttl_rtk_status: int = 14400  # 4 hours
    ttl_processing_status: int = 14400  # 4 hours

    # Temperature thresholds (Celsius)
    temp_warning: float = 50.0
    temp_critical: float = 60.0

    # Voltage thresholds (Volts)
    voltage_warning_low: float = 11.8
    voltage_critical_low: float = 11.0
    voltage_warning_high: float = 15.0
    voltage_critical_high: float = 16.0

    # CPU load thresholds (percent)
    cpu_warning: int = 75
    cpu_critical: int = 90

    # Satellite count thresholds
    satellites_warning: int = 8
    satellites_critical: int = 4

    # File status thresholds (hours)
    file_daily_warning_hours: float = 26.0
    file_daily_critical_hours: float = 50.0
    file_hourly_warning_hours: float = 2.0
    file_hourly_critical_hours: float = 4.0

    # RTK latency thresholds (seconds)
    rtk_latency_warning: float = 10.0
    rtk_latency_critical: float = 30.0

    # Processing thresholds (days behind)
    processing_warning_days: int = 1
    processing_critical_days: int = 3

try:
    import gps_parser
    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False


class ReceiversConfig:
    """Configuration manager for receivers package.

    Loads configuration from receivers.cfg and provides structured access
    to archive paths, session types, and receiver-specific settings.
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize configuration manager.

        Args:
            config_path: Optional path to receivers.cfg file
        """
        self.logger = logging.getLogger(__name__)
        self.config = configparser.ConfigParser()
        self.config_path = self._find_config_path(config_path)
        self._load_config()

    def _find_config_path(self, config_path: Optional[str] = None) -> str:
        """Find receivers.cfg configuration file.

        Args:
            config_path: Optional explicit path

        Returns:
            Path to configuration file

        Raises:
            FileNotFoundError: If configuration file not found
        """
        if config_path and os.path.isfile(config_path):
            return config_path

        # Try gps_parser config directory first
        if HAS_GPS_PARSER:
            try:
                parser_config = gps_parser.ConfigParser()
                gps_config_dir = parser_config.config_path
                receivers_cfg = os.path.join(gps_config_dir, "receivers.cfg")
                if os.path.isfile(receivers_cfg):
                    return receivers_cfg
            except Exception as e:
                self.logger.debug(f"Could not get config dir from gps_parser: {e}")

        # Try standard locations
        search_paths = [
            os.path.expanduser("~/.config/gpsconfig/receivers.cfg"),
            os.path.expanduser("~/.gpsconfig/receivers.cfg"),
            "./receivers.cfg",
            "../receivers.cfg",
        ]

        for path in search_paths:
            if os.path.isfile(path):
                return path

        # If not found, use default location
        default_path = os.path.expanduser("~/.config/gpsconfig/receivers.cfg")
        raise FileNotFoundError(
            f"receivers.cfg not found. Searched: {search_paths}. "
            f"Please create configuration at: {default_path}"
        )

    def _load_config(self) -> None:
        """Load configuration from receivers.cfg file."""
        try:
            self.config.read(self.config_path)
            self.logger.debug(f"Loaded receivers config from: {self.config_path}")
        except Exception as e:
            self.logger.error(f"Failed to load receivers config: {e}")
            raise

    def get_data_prepath(self) -> str:
        """Get base data directory path.

        Returns:
            Base directory path for data storage
        """
        try:
            data_prepath = self.config.get("archive_paths", "data_prepath")
            # Convert relative paths to absolute from project root
            if data_prepath.startswith("./"):
                # Get project root (where this config is being called from)
                project_root = os.getcwd()
                data_prepath = os.path.join(project_root, data_prepath[2:])
                data_prepath = os.path.abspath(data_prepath)
            return data_prepath
        except (configparser.NoSectionError, configparser.NoOptionError):
            # Fallback to project-local tmp directory
            fallback = os.path.join(os.getcwd(), "tmp", "data")
            self.logger.warning(f"Using fallback data_prepath: {fallback}")
            return fallback

    def get_prepath(self) -> str:
        """DEPRECATED: Use get_data_prepath() instead.

        Kept for backward compatibility.
        """
        return self.get_data_prepath()

    def get_tmp_dir(self) -> str:
        """Get temporary download directory path.

        Returns:
            Temporary directory path for downloads
        """
        try:
            tmp_dir = self.config.get("archive_paths", "tmp_dir")
            # Convert relative paths to absolute from project root
            if tmp_dir.startswith("./"):
                project_root = os.getcwd()
                tmp_dir = os.path.join(project_root, tmp_dir[2:])
                tmp_dir = os.path.abspath(tmp_dir)
            return tmp_dir
        except (configparser.NoSectionError, configparser.NoOptionError):
            # Fallback to project-local tmp directory
            fallback = os.path.join(os.getcwd(), "tmp", "download")
            self.logger.warning(f"Using fallback tmp_dir: {fallback}")
            return fallback

    def get_archive_template(self) -> str:
        """Get archive path template.

        Returns:
            Archive path template with placeholders
        """
        try:
            return self.config.get("archive_paths", "archive_template")
        except (configparser.NoSectionError, configparser.NoOptionError):
            # Fallback template
            return "{data_prepath}/%Y/#b/{station}/{session}/raw/{station}%Y%m%d%H00a{extension}"

    def get_session_types(self) -> Dict[str, Dict[str, Any]]:
        """Get session type definitions.

        Returns:
            Dictionary mapping session names to their properties
        """
        session_types = {}
        try:
            for session_name, session_config in self.config.items("session_types"):
                try:
                    # Parse CSV format: frequency,acquisition,description,file_frequency
                    parts = session_config.split(',')
                    if len(parts) >= 3:
                        session_data = {
                            "frequency": parts[0].strip(),
                            "acquisition": parts[1].strip(),
                            "description": parts[2].strip(),
                            "file_frequency": parts[3].strip() if len(parts) > 3 else "24hr"
                        }
                        session_types[session_name] = session_data
                    else:
                        self.logger.warning(f"Invalid session config format for {session_name}: {session_config}")
                except Exception as e:
                    self.logger.warning(f"Could not parse session config for {session_name}: {e}")
                    continue
        except configparser.NoSectionError:
            # Fallback session types
            session_types = {
                "15s_24hr": {"frequency": "1D", "acquisition": "15s", "description": "Daily 15-second data"},
                "1Hz_1hr": {"frequency": "1H", "acquisition": "1Hz", "description": "Hourly 1Hz data"},
                "status_1hr": {"frequency": "1H", "acquisition": "status", "description": "Hourly status data"}
            }
            self.logger.warning("Using fallback session types")

        return session_types

    def get_receiver_config(self, receiver_type: str) -> Dict[str, Any]:
        """Get configuration for specific receiver type.

        Args:
            receiver_type: Receiver type (e.g., 'septentrio', 'leica')

        Returns:
            Dictionary with receiver-specific configuration
        """
        receiver_config = {}

        # Get receiver defaults first
        try:
            for key, value in self.config.items("receiver_defaults"):
                try:
                    # Try to parse as Python literal (bool, int, etc.)
                    receiver_config[key] = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    # Keep as string if not parseable
                    receiver_config[key] = value
        except configparser.NoSectionError:
            pass

        # Override with receiver-specific settings
        section_name = receiver_type.lower()
        try:
            for key, value in self.config.items(section_name):
                try:
                    # Try to parse as Python literal
                    receiver_config[key] = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    # Keep as string if not parseable
                    receiver_config[key] = value
        except configparser.NoSectionError:
            self.logger.debug(f"No specific configuration found for receiver type: {receiver_type}")

        return receiver_config

    def build_archive_path(self, station_id: str, session: str, dt, extension: str, session_letter: str = "a") -> str:
        """Build archive path for a specific file.

        DEPRECATED: Use BaseReceiver.build_path() instead for unified path building.
        This method is kept for backward compatibility but may be removed in future versions.

        Args:
            station_id: Station identifier
            session: Session type
            dt: datetime object
            extension: File extension (e.g., '.sbf.gz')
            session_letter: Session letter code (e.g., 'a', 'b', 'c')

        Returns:
            Complete archive path
        """
        template = self.get_archive_template()
        data_prepath = self.get_data_prepath()

        # Use gtimes to format the template with datetime
        try:
            import gtimes.timefunc as gt

            # Create template with our variables filled in
            filled_template = template.format(
                data_prepath=data_prepath,
                station=station_id,
                session=session,
                extension=extension,
                session_letter=session_letter
            )

            # Use gtimes to handle the datetime formatting
            archive_paths = gt.datepathlist(
                filled_template,
                "1D",  # We're building for single datetime
                datelist=[dt],
                closed="both"
            )

            return archive_paths[0]

        except ImportError:
            # Fallback without gtimes
            self.logger.warning("gtimes not available - using simple datetime formatting")
            filled_template = template.format(
                data_prepath=data_prepath,
                station=station_id,
                session=session,
                extension=extension,
                session_letter=session_letter
            )
            # Simple datetime substitution
            return dt.strftime(filled_template)

    def get_icinga_thresholds(self) -> IcingaThresholds:
        """Get Icinga monitoring thresholds from configuration.

        Reads [icinga_thresholds] section and returns IcingaThresholds dataclass
        with all threshold values. Uses defaults if section or values are missing.

        Returns:
            IcingaThresholds with configured or default values
        """
        thresholds = IcingaThresholds()  # Start with defaults

        try:
            section = "icinga_thresholds"
            if self.config.has_section(section):
                # TTL values
                thresholds.ttl_health_checks = self.config.getint(
                    section, "ttl_health_checks", fallback=thresholds.ttl_health_checks
                )
                thresholds.ttl_file_status = self.config.getint(
                    section, "ttl_file_status", fallback=thresholds.ttl_file_status
                )
                thresholds.ttl_rtk_status = self.config.getint(
                    section, "ttl_rtk_status", fallback=thresholds.ttl_rtk_status
                )
                thresholds.ttl_processing_status = self.config.getint(
                    section, "ttl_processing_status", fallback=thresholds.ttl_processing_status
                )

                # Temperature
                thresholds.temp_warning = self.config.getfloat(
                    section, "temp_warning", fallback=thresholds.temp_warning
                )
                thresholds.temp_critical = self.config.getfloat(
                    section, "temp_critical", fallback=thresholds.temp_critical
                )

                # Voltage
                thresholds.voltage_warning_low = self.config.getfloat(
                    section, "voltage_warning_low", fallback=thresholds.voltage_warning_low
                )
                thresholds.voltage_critical_low = self.config.getfloat(
                    section, "voltage_critical_low", fallback=thresholds.voltage_critical_low
                )
                thresholds.voltage_warning_high = self.config.getfloat(
                    section, "voltage_warning_high", fallback=thresholds.voltage_warning_high
                )
                thresholds.voltage_critical_high = self.config.getfloat(
                    section, "voltage_critical_high", fallback=thresholds.voltage_critical_high
                )

                # CPU
                thresholds.cpu_warning = self.config.getint(
                    section, "cpu_warning", fallback=thresholds.cpu_warning
                )
                thresholds.cpu_critical = self.config.getint(
                    section, "cpu_critical", fallback=thresholds.cpu_critical
                )

                # Satellites
                thresholds.satellites_warning = self.config.getint(
                    section, "satellites_warning", fallback=thresholds.satellites_warning
                )
                thresholds.satellites_critical = self.config.getint(
                    section, "satellites_critical", fallback=thresholds.satellites_critical
                )

                # File status
                thresholds.file_daily_warning_hours = self.config.getfloat(
                    section, "file_daily_warning_hours", fallback=thresholds.file_daily_warning_hours
                )
                thresholds.file_daily_critical_hours = self.config.getfloat(
                    section, "file_daily_critical_hours", fallback=thresholds.file_daily_critical_hours
                )
                thresholds.file_hourly_warning_hours = self.config.getfloat(
                    section, "file_hourly_warning_hours", fallback=thresholds.file_hourly_warning_hours
                )
                thresholds.file_hourly_critical_hours = self.config.getfloat(
                    section, "file_hourly_critical_hours", fallback=thresholds.file_hourly_critical_hours
                )

                # RTK latency
                thresholds.rtk_latency_warning = self.config.getfloat(
                    section, "rtk_latency_warning", fallback=thresholds.rtk_latency_warning
                )
                thresholds.rtk_latency_critical = self.config.getfloat(
                    section, "rtk_latency_critical", fallback=thresholds.rtk_latency_critical
                )

                # Processing
                thresholds.processing_warning_days = self.config.getint(
                    section, "processing_warning_days", fallback=thresholds.processing_warning_days
                )
                thresholds.processing_critical_days = self.config.getint(
                    section, "processing_critical_days", fallback=thresholds.processing_critical_days
                )

        except Exception as e:
            self.logger.debug(f"Error reading icinga_thresholds, using defaults: {e}")

        return thresholds

    def is_valid_session(self, session: str) -> bool:
        """Check if session type is valid.

        Args:
            session: Session type to check

        Returns:
            True if session is defined in configuration
        """
        session_types = self.get_session_types()
        return session in session_types

    def get_session_frequency(self, session: str) -> str:
        """Get frequency for session type.

        Args:
            session: Session type

        Returns:
            Frequency string (e.g., '1D', '1H')
        """
        session_types = self.get_session_types()

        # Handle case-insensitive lookup (configparser converts keys to lowercase)
        session_lower = session.lower()
        if session_lower in session_types:
            return session_types[session_lower].get("frequency", "1D")
        elif session in session_types:
            return session_types[session].get("frequency", "1D")
        return "1D"  # Default

    def reload(self) -> None:
        """Reload configuration from file."""
        self._load_config()


# Global configuration instance
_global_config: Optional[ReceiversConfig] = None


def get_receivers_config() -> ReceiversConfig:
    """Get global receivers configuration instance.

    Returns:
        Shared ReceiversConfig instance
    """
    global _global_config
    if _global_config is None:
        _global_config = ReceiversConfig()
    return _global_config


def reload_config() -> None:
    """Reload global configuration from file."""
    global _global_config
    if _global_config is not None:
        _global_config.reload()
    else:
        _global_config = ReceiversConfig()