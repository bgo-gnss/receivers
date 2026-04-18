"""Icinga configuration management.

This module handles loading configuration for Icinga monitoring integration,
including connection settings and threshold values.

Connection settings resolution order:
1. icinga.cfg [connection] section (highest priority)
2. Environment variables (ICINGA_HOST, ICINGA_USERNAME, ICINGA_PASSWORD, etc.)
3. Hardcoded defaults (lowest priority)
"""

import configparser
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class IcingaThresholds:
    """Icinga monitoring thresholds and TTL values.

    All values can be configured in icinga.cfg.
    TTL (time-to-live) can be set globally in [ttl] section, and optionally
    overridden per-category by adding 'ttl = value' to any threshold section.
    """

    # Global default TTL (seconds) - used when category doesn't specify its own
    ttl_default: int = 14400  # 4 hours

    # Per-category TTL overrides (None = use default)
    ttl_temperature: Optional[int] = None
    ttl_voltage: Optional[int] = None
    ttl_cpu: Optional[int] = None
    ttl_satellites: Optional[int] = None
    ttl_disk: Optional[int] = None
    ttl_file_daily: Optional[int] = None
    ttl_file_hourly: Optional[int] = None
    ttl_rtk: Optional[int] = None
    ttl_processing: Optional[int] = None

    def get_ttl(self, category: str) -> int:
        """Get TTL for a category, falling back to default if not set.

        Args:
            category: Category name (e.g., 'temperature', 'voltage', 'file_daily')

        Returns:
            TTL value in seconds
        """
        ttl_attr = f"ttl_{category}"
        if hasattr(self, ttl_attr):
            value = getattr(self, ttl_attr)
            if value is not None:
                return value
        return self.ttl_default

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

    # Disk usage thresholds (percent)
    disk_warning: float = 90.0
    disk_critical: float = 97.0

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


@dataclass
class IcingaConnection:
    """Icinga API connection settings.

    Values are resolved from environment variables with fallback defaults.
    Config file values (icinga.cfg) take priority when loaded via IcingaConfig.
    """

    host: str = field(
        default_factory=lambda: os.getenv("ICINGA_HOST", "ut-icinga-m-vip.vedur.is")
    )
    port: int = field(default_factory=lambda: int(os.getenv("ICINGA_PORT", "5665")))
    username: str = field(default_factory=lambda: os.getenv("ICINGA_USERNAME", ""))
    password: str = field(default_factory=lambda: os.getenv("ICINGA_PASSWORD", ""))
    verify_ssl: bool = False
    timeout: int = 30
    check_source: str = field(
        default_factory=lambda: os.getenv("ICINGA_CHECK_SOURCE", "eldey")
    )


class IcingaConfig:
    """Configuration manager for Icinga integration.

    Loads configuration from icinga.cfg and provides structured access
    to connection settings and monitoring thresholds.
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize configuration manager.

        Args:
            config_path: Optional path to icinga.cfg file
        """
        self.config = configparser.ConfigParser()
        self.config_path = self._find_config_path(config_path)
        self._load_config()

    def _find_config_path(self, config_path: Optional[str] = None) -> Optional[str]:
        """Find icinga.cfg configuration file.

        Args:
            config_path: Optional explicit path

        Returns:
            Path to configuration file, or None if not found
        """
        if config_path and os.path.isfile(config_path):
            return config_path

        # Check GPS_CONFIG_PATH environment variable
        config_dir = os.environ.get("GPS_CONFIG_PATH")
        if config_dir:
            icinga_cfg = os.path.join(config_dir, "icinga.cfg")
            if os.path.isfile(icinga_cfg):
                return icinga_cfg

        # Try gps_parser config directory
        try:
            import gps_parser

            parser_config = gps_parser.ConfigParser()
            gps_config_dir = parser_config.config_path
            if gps_config_dir:
                icinga_cfg = os.path.join(gps_config_dir, "icinga.cfg")
                if os.path.isfile(icinga_cfg):
                    return icinga_cfg
        except Exception:
            pass

        # Try standard locations
        search_paths = [
            os.path.expanduser("~/.config/gpsconfig/icinga.cfg"),
            os.path.expanduser("~/.gpsconfig/icinga.cfg"),
            "./icinga.cfg",
        ]

        for path in search_paths:
            if os.path.isfile(path):
                return path

        logger.debug("icinga.cfg not found, using defaults")
        return None

    def _load_config(self) -> None:
        """Load configuration from icinga.cfg file."""
        if self.config_path:
            try:
                self.config.read(self.config_path)
                logger.debug(f"Loaded icinga config from: {self.config_path}")
            except Exception as e:
                logger.warning(f"Failed to load icinga config: {e}")

    def get_connection(self) -> IcingaConnection:
        """Get Icinga API connection settings.

        Returns:
            IcingaConnection with configured or default values
        """
        conn = IcingaConnection()

        try:
            section = "connection"
            if self.config.has_section(section):
                conn.host = self.config.get(section, "host", fallback=conn.host)
                conn.port = self.config.getint(section, "port", fallback=conn.port)
                conn.username = self.config.get(
                    section, "username", fallback=conn.username
                )
                conn.password = self.config.get(
                    section, "password", fallback=conn.password
                )
                conn.verify_ssl = self.config.getboolean(
                    section, "verify_ssl", fallback=conn.verify_ssl
                )
                conn.timeout = self.config.getint(
                    section, "timeout", fallback=conn.timeout
                )
                conn.check_source = self.config.get(
                    section, "check_source", fallback=conn.check_source
                )
        except Exception as e:
            logger.debug(f"Error reading connection settings: {e}")

        return conn

    def get_thresholds(self) -> IcingaThresholds:
        """Get Icinga monitoring thresholds from configuration.

        Returns:
            IcingaThresholds with configured or default values
        """
        thresholds = IcingaThresholds()

        try:
            # Global default TTL
            if self.config.has_section("ttl"):
                thresholds.ttl_default = self.config.getint(
                    "ttl", "default", fallback=thresholds.ttl_default
                )

            # Helper to read optional TTL from a section
            def get_section_ttl(section: str, category: str) -> None:
                if self.config.has_option(section, "ttl"):
                    ttl_value = self.config.getint(section, "ttl")
                    setattr(thresholds, f"ttl_{category}", ttl_value)

            # Temperature
            if self.config.has_section("thresholds.temperature"):
                thresholds.temp_warning = self.config.getfloat(
                    "thresholds.temperature",
                    "warning",
                    fallback=thresholds.temp_warning,
                )
                thresholds.temp_critical = self.config.getfloat(
                    "thresholds.temperature",
                    "critical",
                    fallback=thresholds.temp_critical,
                )
                get_section_ttl("thresholds.temperature", "temperature")

            # Voltage
            if self.config.has_section("thresholds.voltage"):
                thresholds.voltage_warning_low = self.config.getfloat(
                    "thresholds.voltage",
                    "warning_low",
                    fallback=thresholds.voltage_warning_low,
                )
                thresholds.voltage_critical_low = self.config.getfloat(
                    "thresholds.voltage",
                    "critical_low",
                    fallback=thresholds.voltage_critical_low,
                )
                thresholds.voltage_warning_high = self.config.getfloat(
                    "thresholds.voltage",
                    "warning_high",
                    fallback=thresholds.voltage_warning_high,
                )
                thresholds.voltage_critical_high = self.config.getfloat(
                    "thresholds.voltage",
                    "critical_high",
                    fallback=thresholds.voltage_critical_high,
                )
                get_section_ttl("thresholds.voltage", "voltage")

            # CPU
            if self.config.has_section("thresholds.cpu"):
                thresholds.cpu_warning = self.config.getint(
                    "thresholds.cpu", "warning", fallback=thresholds.cpu_warning
                )
                thresholds.cpu_critical = self.config.getint(
                    "thresholds.cpu", "critical", fallback=thresholds.cpu_critical
                )
                get_section_ttl("thresholds.cpu", "cpu")

            # Satellites
            if self.config.has_section("thresholds.satellites"):
                thresholds.satellites_warning = self.config.getint(
                    "thresholds.satellites",
                    "warning",
                    fallback=thresholds.satellites_warning,
                )
                thresholds.satellites_critical = self.config.getint(
                    "thresholds.satellites",
                    "critical",
                    fallback=thresholds.satellites_critical,
                )
                get_section_ttl("thresholds.satellites", "satellites")

            # Disk
            if self.config.has_section("thresholds.disk"):
                thresholds.disk_warning = self.config.getfloat(
                    "thresholds.disk", "warning", fallback=thresholds.disk_warning
                )
                thresholds.disk_critical = self.config.getfloat(
                    "thresholds.disk", "critical", fallback=thresholds.disk_critical
                )
                get_section_ttl("thresholds.disk", "disk")

            # File status - daily
            if self.config.has_section("thresholds.file_daily"):
                thresholds.file_daily_warning_hours = self.config.getfloat(
                    "thresholds.file_daily",
                    "warning_hours",
                    fallback=thresholds.file_daily_warning_hours,
                )
                thresholds.file_daily_critical_hours = self.config.getfloat(
                    "thresholds.file_daily",
                    "critical_hours",
                    fallback=thresholds.file_daily_critical_hours,
                )
                get_section_ttl("thresholds.file_daily", "file_daily")

            # File status - hourly
            if self.config.has_section("thresholds.file_hourly"):
                thresholds.file_hourly_warning_hours = self.config.getfloat(
                    "thresholds.file_hourly",
                    "warning_hours",
                    fallback=thresholds.file_hourly_warning_hours,
                )
                thresholds.file_hourly_critical_hours = self.config.getfloat(
                    "thresholds.file_hourly",
                    "critical_hours",
                    fallback=thresholds.file_hourly_critical_hours,
                )
                get_section_ttl("thresholds.file_hourly", "file_hourly")

            # RTK latency
            if self.config.has_section("thresholds.rtk"):
                thresholds.rtk_latency_warning = self.config.getfloat(
                    "thresholds.rtk",
                    "latency_warning",
                    fallback=thresholds.rtk_latency_warning,
                )
                thresholds.rtk_latency_critical = self.config.getfloat(
                    "thresholds.rtk",
                    "latency_critical",
                    fallback=thresholds.rtk_latency_critical,
                )
                get_section_ttl("thresholds.rtk", "rtk")

            # Processing
            if self.config.has_section("thresholds.processing"):
                thresholds.processing_warning_days = self.config.getint(
                    "thresholds.processing",
                    "warning_days",
                    fallback=thresholds.processing_warning_days,
                )
                thresholds.processing_critical_days = self.config.getint(
                    "thresholds.processing",
                    "critical_days",
                    fallback=thresholds.processing_critical_days,
                )
                get_section_ttl("thresholds.processing", "processing")

        except Exception as e:
            logger.debug(f"Error reading thresholds, using defaults: {e}")

        return thresholds


# Global configuration instance
_global_config: Optional[IcingaConfig] = None


def get_icinga_config() -> IcingaConfig:
    """Get global Icinga configuration instance.

    Returns:
        Shared IcingaConfig instance
    """
    global _global_config
    if _global_config is None:
        _global_config = IcingaConfig()
    return _global_config
