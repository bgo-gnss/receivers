"""Centralized metrics evaluation and threshold configuration.

This module provides unified threshold definitions and metric evaluation
for all GPS receiver health monitoring. It serves as the single source
of truth for status determination across:
- CLI status display
- Icinga monitoring integration
- Health data extractors (PolaRX5, Trimble, etc.)

Thresholds can be configured via YAML file (hybrid approach):
- Default thresholds are defined in ThresholdConfig dataclass
- Optional config file (~/.config/gpsconfig/thresholds.yaml) can override defaults
- Per-receiver-type overrides are supported

Usage:
    from receivers.health.metrics import MetricChecker, ThresholdConfig, load_thresholds

    # Use defaults
    checker = MetricChecker()

    # Load from config file (with defaults fallback)
    config = load_thresholds()
    checker = MetricChecker(config)

    # Get config for specific receiver type
    config = load_thresholds(receiver_type="NetRS")
    checker = MetricChecker(config)

    result = checker.check_voltage(13.5)
    print(f"{result.status.value}: {result.message}")
"""

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class HealthStatus(Enum):
    """Standardized health status values.

    These map to Nagios/Icinga exit codes:
    - OK = 0
    - WARNING = 1
    - CRITICAL = 2
    - UNKNOWN = 3
    """

    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

    @property
    def exit_code(self) -> int:
        """Get Nagios/Icinga exit code for this status."""
        return {
            HealthStatus.OK: 0,
            HealthStatus.WARNING: 1,
            HealthStatus.CRITICAL: 2,
            HealthStatus.UNKNOWN: 3,
        }[self]

    @property
    def emoji(self) -> str:
        """Get display emoji for this status."""
        return {
            HealthStatus.OK: "✅",
            HealthStatus.WARNING: "⚠️",
            HealthStatus.CRITICAL: "❌",
            HealthStatus.UNKNOWN: "❓",
        }[self]

    @classmethod
    def from_string(cls, value: str) -> "HealthStatus":
        """Convert string to HealthStatus, handling aliases.

        Args:
            value: Status string (ok, warning, critical, unknown, healthy, error, etc.)

        Returns:
            HealthStatus enum value
        """
        normalized = value.lower().strip()
        aliases = {
            "ok": cls.OK,
            "healthy": cls.OK,
            "good": cls.OK,
            "warning": cls.WARNING,
            "warn": cls.WARNING,
            "fair": cls.WARNING,
            "critical": cls.CRITICAL,
            "error": cls.CRITICAL,
            "crit": cls.CRITICAL,
            "unknown": cls.UNKNOWN,
        }
        return aliases.get(normalized, cls.UNKNOWN)


@dataclass
class ThresholdConfig:
    """Unified threshold configuration for all health metrics.

    All thresholds are defined here as the single source of truth.
    These values are used across CLI, Icinga, and health extractors.

    Voltage Thresholds:
        - Normal operation: 11.8V - 15.0V
        - Warning: below 11.8V or above 15.0V
        - Critical: below 11.0V or above 16.0V

    Temperature Thresholds:
        - Normal operation: -10°C to 50°C
        - Warning: -10°C to -20°C (cold) or 50°C to 60°C (hot)
        - Critical: below -20°C or above 60°C

    CPU Load Thresholds:
        - Normal: 0-75%
        - Warning: 75-90%
        - Critical: above 90%

    Satellite Count Thresholds:
        - Normal: 8+ satellites
        - Warning: 4-8 satellites
        - Critical: below 4 satellites

    Disk Usage Thresholds:
        - Normal: 0-80%
        - Warning: 80-90%
        - Critical: above 90%
    """

    # Voltage (V) - for receiver power supply monitoring
    voltage_critical_low: float = 11.0
    voltage_warning_low: float = 11.8
    voltage_warning_high: float = 15.0
    voltage_critical_high: float = 16.0
    voltage_min: float = 10.0  # Performance data range
    voltage_max: float = 18.0  # Performance data range

    # Temperature (°C) - for internal receiver temperature
    temp_critical_low: float = -20.0
    temp_warning_low: float = -10.0
    temp_warning_high: float = 50.0
    temp_critical_high: float = 60.0
    temp_min: float = -30.0  # Performance data range
    temp_max: float = 80.0  # Performance data range

    # CPU Load (%)
    cpu_warning: int = 75
    cpu_critical: int = 90
    cpu_min: int = 0
    cpu_max: int = 100

    # Satellite tracking counts
    sat_warning: int = 8  # Below this is warning
    sat_critical: int = 4  # Below this is critical
    sat_min: int = 0
    sat_max: int = 40

    # Disk usage (%)
    disk_warning: float = 90.0
    disk_critical: float = 97.0
    disk_min: float = 0.0
    disk_max: float = 100.0

    # Ping latency (ms)
    ping_warning: float = 1000.0
    ping_critical: float = 5000.0

    # Packet loss (%)
    packet_loss_warning: float = 20.0
    packet_loss_critical: float = 50.0


def get_thresholds_config_path() -> Path:
    """Get the path to the thresholds config file.

    Respects GPS_CONFIG_PATH environment variable if set.

    Returns:
        Path to thresholds.yaml config file
    """
    config_base = os.environ.get("GPS_CONFIG_PATH", os.path.expanduser("~/.config/gpsconfig"))
    return Path(config_base) / "thresholds.yaml"


def load_thresholds(
    receiver_type: Optional[str] = None,
    config_path: Optional[Path] = None,
    power_type: Optional[str] = None,
) -> ThresholdConfig:
    """Load threshold configuration from YAML file with defaults fallback.

    This implements the hybrid approach:
    1. Start with default ThresholdConfig values
    2. Override with values from config file 'defaults' section (if file exists)
    3. Override with receiver-type-specific values (if specified and present)

    Args:
        receiver_type: Optional receiver type for type-specific overrides
                      (e.g., 'PolaRX5', 'NetRS', 'NetR9')
        config_path: Optional path to config file. If None, uses default location.

    Returns:
        ThresholdConfig with merged values

    Example config file (~/.config/gpsconfig/thresholds.yaml):
        defaults:
          voltage:
            warning_low: 11.8
            critical_low: 11.0
            warning_high: 15.0
            critical_high: 16.0
          temperature:
            warning_high: 50.0
            critical_high: 60.0
          satellites:
            warning: 8
            critical: 4

        receiver_types:
          NetRS:
            voltage:
              warning_low: 11.5
          PolaRX5:
            temperature:
              critical_high: 70.0
    """
    # Start with defaults
    config = ThresholdConfig()

    # Determine config file path
    if config_path is None:
        config_path = get_thresholds_config_path()

    # Built-in DC/DC voltage thresholds (13.5-16.5V normal range)
    _dcdc_voltage_overrides = {
        "voltage": {
            "warning_low": 12.0,
            "critical_low": 11.0,
            "warning_high": 16.5,
            "critical_high": 18.0,
        }
    }

    # Try to load from file
    if not config_path.exists():
        logger.debug(f"No thresholds config file at {config_path}, using defaults")
        if power_type == "dcdc":
            config = _apply_config_section(config, _dcdc_voltage_overrides)
        return config

    try:
        import yaml

        with open(config_path) as f:
            yaml_config = yaml.safe_load(f)

        if not yaml_config:
            return config

        # Apply defaults section
        defaults = yaml_config.get("defaults", {})
        config = _apply_config_section(config, defaults)

        # Apply receiver-type-specific overrides
        if receiver_type:
            receiver_types = yaml_config.get("receiver_types", {})
            # Try exact match first, then case-insensitive
            type_config = receiver_types.get(receiver_type)
            if type_config is None:
                # Case-insensitive lookup
                for key, value in receiver_types.items():
                    if key.lower() == receiver_type.lower():
                        type_config = value
                        break

            if type_config:
                config = _apply_config_section(config, type_config)
                logger.debug(f"Applied receiver-type overrides for {receiver_type}")

        # Apply power-type-specific overrides
        if power_type:
            power_types = yaml_config.get("power_types", {})
            pt_config = power_types.get(power_type)
            if pt_config:
                config = _apply_config_section(config, pt_config)
                logger.debug(f"Applied power-type overrides for {power_type}")
            elif power_type == "dcdc":
                config = _apply_config_section(config, _dcdc_voltage_overrides)
                logger.debug("Applied built-in DC/DC voltage thresholds")

        logger.debug(f"Loaded thresholds from {config_path}")
        return config

    except ImportError:
        logger.warning("PyYAML not installed, using default thresholds")
        return config
    except Exception as e:
        logger.warning(f"Error loading thresholds config: {e}, using defaults")
        return config


def _apply_config_section(config: ThresholdConfig, section: Dict[str, Any]) -> ThresholdConfig:
    """Apply a config section to ThresholdConfig.

    Args:
        config: Current ThresholdConfig
        section: Dict with config values to apply

    Returns:
        Updated ThresholdConfig
    """
    # Voltage thresholds
    voltage = section.get("voltage", {})
    if "critical_low" in voltage:
        config.voltage_critical_low = float(voltage["critical_low"])
    if "warning_low" in voltage:
        config.voltage_warning_low = float(voltage["warning_low"])
    if "warning_high" in voltage:
        config.voltage_warning_high = float(voltage["warning_high"])
    if "critical_high" in voltage:
        config.voltage_critical_high = float(voltage["critical_high"])
    if "min" in voltage:
        config.voltage_min = float(voltage["min"])
    if "max" in voltage:
        config.voltage_max = float(voltage["max"])

    # Temperature thresholds
    temp = section.get("temperature", {})
    if "critical_low" in temp:
        config.temp_critical_low = float(temp["critical_low"])
    if "warning_low" in temp:
        config.temp_warning_low = float(temp["warning_low"])
    if "warning_high" in temp:
        config.temp_warning_high = float(temp["warning_high"])
    if "critical_high" in temp:
        config.temp_critical_high = float(temp["critical_high"])

    # CPU thresholds
    cpu = section.get("cpu", {})
    if "warning" in cpu:
        config.cpu_warning = int(cpu["warning"])
    if "critical" in cpu:
        config.cpu_critical = int(cpu["critical"])

    # Satellite thresholds
    sats = section.get("satellites", {})
    if "warning" in sats:
        config.sat_warning = int(sats["warning"])
    if "critical" in sats:
        config.sat_critical = int(sats["critical"])

    # Disk thresholds
    disk = section.get("disk", {})
    if "warning" in disk:
        config.disk_warning = float(disk["warning"])
    if "critical" in disk:
        config.disk_critical = float(disk["critical"])

    # Ping thresholds
    ping = section.get("ping", {})
    if "warning" in ping:
        config.ping_warning = float(ping["warning"])
    if "critical" in ping:
        config.ping_critical = float(ping["critical"])

    # Packet loss thresholds
    packet_loss = section.get("packet_loss", {})
    if "warning" in packet_loss:
        config.packet_loss_warning = float(packet_loss["warning"])
    if "critical" in packet_loss:
        config.packet_loss_critical = float(packet_loss["critical"])

    return config


@dataclass
class MetricResult:
    """Result of evaluating a metric against thresholds.

    Attributes:
        status: HealthStatus enum value
        value: The measured value
        unit: Unit of measurement (V, C, %, etc.)
        message: Human-readable status message
        performance_data: Nagios performance data string
    """

    status: HealthStatus
    value: Any
    unit: str
    message: str
    performance_data: str

    @property
    def exit_code(self) -> int:
        """Get Nagios/Icinga exit code."""
        return self.status.exit_code

    @property
    def emoji(self) -> str:
        """Get display emoji."""
        return self.status.emoji

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "status": self.status.value,
            "value": self.value,
            "unit": self.unit,
            "message": self.message,
            "performance_data": self.performance_data,
        }


class MetricChecker:
    """Unified metric evaluation with threshold checking.

    This class provides centralized methods for checking all health metrics
    against thresholds and generating consistent status results.

    Example:
        checker = MetricChecker()

        # Check voltage
        result = checker.check_voltage(13.2)
        print(f"Status: {result.status.value}")
        print(f"Message: {result.message}")

        # Check temperature with custom thresholds
        custom_config = ThresholdConfig(temp_critical_high=70.0)
        checker = MetricChecker(custom_config)
        result = checker.check_temperature(55.0)
    """

    def __init__(self, config: Optional[ThresholdConfig] = None):
        """Initialize MetricChecker with optional custom thresholds.

        Args:
            config: Custom threshold configuration. If None, uses defaults.
        """
        self.config = config or ThresholdConfig()

    def check_voltage(
        self,
        voltage: Optional[float],
        station: str = "",
    ) -> MetricResult:
        """Check voltage against thresholds.

        Args:
            voltage: Voltage reading in volts (None if unavailable)
            station: Station ID for message formatting

        Returns:
            MetricResult with status, message, and performance data
        """
        cfg = self.config

        if voltage is None:
            return MetricResult(
                status=HealthStatus.UNKNOWN,
                value=None,
                unit="V",
                message=f"❓ Station volt UNKNOWN - {station} voltage unavailable"
                if station
                else "❓ Voltage unavailable",
                performance_data="",
            )

        # Performance data format: voltage=VALUE;WARN_RANGE;CRIT_RANGE;MIN;MAX
        perf_data = (
            f"voltage={voltage}V;"
            f"{cfg.voltage_warning_low}:{cfg.voltage_warning_high};"
            f"{cfg.voltage_critical_low}:{cfg.voltage_critical_high};"
            f"{cfg.voltage_min};{cfg.voltage_max}"
        )

        station_prefix = f"{station}: " if station else ""

        if voltage <= cfg.voltage_critical_low:
            return MetricResult(
                status=HealthStatus.CRITICAL,
                value=voltage,
                unit="V",
                message=f"❌ Station volt CRITICAL - {station_prefix}{voltage:.1f}V (LOW <={cfg.voltage_critical_low}V)",
                performance_data=perf_data,
            )
        elif voltage >= cfg.voltage_critical_high:
            return MetricResult(
                status=HealthStatus.CRITICAL,
                value=voltage,
                unit="V",
                message=f"❌ Station volt CRITICAL - {station_prefix}{voltage:.1f}V (HIGH >={cfg.voltage_critical_high}V)",
                performance_data=perf_data,
            )
        elif voltage <= cfg.voltage_warning_low:
            return MetricResult(
                status=HealthStatus.WARNING,
                value=voltage,
                unit="V",
                message=f"⚠️  Station volt WARNING - {station_prefix}{voltage:.1f}V (LOW <={cfg.voltage_warning_low}V)",
                performance_data=perf_data,
            )
        elif voltage >= cfg.voltage_warning_high:
            return MetricResult(
                status=HealthStatus.WARNING,
                value=voltage,
                unit="V",
                message=f"⚠️  Station volt WARNING - {station_prefix}{voltage:.1f}V (HIGH >={cfg.voltage_warning_high}V)",
                performance_data=perf_data,
            )
        else:
            return MetricResult(
                status=HealthStatus.OK,
                value=voltage,
                unit="V",
                message=f"✅ Station volt OK - {station_prefix}{voltage:.1f}V",
                performance_data=perf_data,
            )

    def check_temperature(
        self,
        temperature: Optional[float],
        station: str = "",
        unit: str = "C",
    ) -> MetricResult:
        """Check temperature against thresholds.

        Args:
            temperature: Temperature reading in Celsius (None if unavailable)
            station: Station ID for message formatting
            unit: Temperature unit (default: 'C')

        Returns:
            MetricResult with status, message, and performance data
        """
        cfg = self.config

        if temperature is None:
            return MetricResult(
                status=HealthStatus.UNKNOWN,
                value=None,
                unit=unit,
                message=f"❓ Station temp UNKNOWN - {station} temperature unavailable"
                if station
                else "❓ Temperature unavailable",
                performance_data="",
            )

        # Performance data format: temp=VALUE;WARN;CRIT;MIN;MAX
        perf_data = (
            f"temp={temperature}{unit};"
            f"{cfg.temp_warning_high};"
            f"{cfg.temp_critical_high};"
            f"{cfg.temp_min};{cfg.temp_max}"
        )

        station_prefix = f"{station}: " if station else ""

        if temperature >= cfg.temp_critical_high:
            return MetricResult(
                status=HealthStatus.CRITICAL,
                value=temperature,
                unit=unit,
                message=f"❌ Station temp CRITICAL - {station_prefix}{temperature}°{unit} (>={cfg.temp_critical_high}°{unit})",
                performance_data=perf_data,
            )
        elif temperature <= cfg.temp_critical_low:
            return MetricResult(
                status=HealthStatus.CRITICAL,
                value=temperature,
                unit=unit,
                message=f"❌ Station temp CRITICAL - {station_prefix}{temperature}°{unit} (<={cfg.temp_critical_low}°{unit})",
                performance_data=perf_data,
            )
        elif temperature >= cfg.temp_warning_high:
            return MetricResult(
                status=HealthStatus.WARNING,
                value=temperature,
                unit=unit,
                message=f"⚠️  Station temp WARNING - {station_prefix}{temperature}°{unit} (>={cfg.temp_warning_high}°{unit})",
                performance_data=perf_data,
            )
        elif temperature <= cfg.temp_warning_low:
            return MetricResult(
                status=HealthStatus.WARNING,
                value=temperature,
                unit=unit,
                message=f"⚠️  Station temp WARNING - {station_prefix}{temperature}°{unit} (<={cfg.temp_warning_low}°{unit})",
                performance_data=perf_data,
            )
        else:
            return MetricResult(
                status=HealthStatus.OK,
                value=temperature,
                unit=unit,
                message=f"✅ Station temp OK - {station_prefix}{temperature}°{unit}",
                performance_data=perf_data,
            )

    def check_cpu_load(
        self,
        cpu_load: Optional[int],
        station: str = "",
    ) -> MetricResult:
        """Check CPU load against thresholds.

        Args:
            cpu_load: CPU load percentage (None if unavailable)
            station: Station ID for message formatting

        Returns:
            MetricResult with status, message, and performance data
        """
        cfg = self.config

        if cpu_load is None:
            return MetricResult(
                status=HealthStatus.UNKNOWN,
                value=None,
                unit="%",
                message=f"❓ CPU load UNKNOWN - {station} CPU data unavailable"
                if station
                else "❓ CPU load unavailable",
                performance_data="",
            )

        # Performance data format: cpu=VALUE%;WARN;CRIT;MIN;MAX
        perf_data = f"cpu={cpu_load}%;{cfg.cpu_warning};{cfg.cpu_critical};{cfg.cpu_min};{cfg.cpu_max}"

        station_prefix = f"{station}: " if station else ""

        if cpu_load >= cfg.cpu_critical:
            return MetricResult(
                status=HealthStatus.CRITICAL,
                value=cpu_load,
                unit="%",
                message=f"❌ CPU load CRITICAL - {station_prefix}{cpu_load}% (>={cfg.cpu_critical}%)",
                performance_data=perf_data,
            )
        elif cpu_load >= cfg.cpu_warning:
            return MetricResult(
                status=HealthStatus.WARNING,
                value=cpu_load,
                unit="%",
                message=f"⚠️  CPU load WARNING - {station_prefix}{cpu_load}% (>={cfg.cpu_warning}%)",
                performance_data=perf_data,
            )
        else:
            return MetricResult(
                status=HealthStatus.OK,
                value=cpu_load,
                unit="%",
                message=f"✅ CPU load OK - {station_prefix}{cpu_load}%",
                performance_data=perf_data,
            )

    def check_satellites(
        self,
        total_satellites: Optional[int],
        by_constellation: Optional[Dict[str, int]] = None,
        station: str = "",
    ) -> MetricResult:
        """Check satellite count against thresholds.

        Args:
            total_satellites: Total number of satellites tracked
            by_constellation: Dict with counts per constellation (GPS, GLONASS, etc.)
            station: Station ID for message formatting

        Returns:
            MetricResult with status, message, and performance data
        """
        cfg = self.config

        if total_satellites is None:
            return MetricResult(
                status=HealthStatus.UNKNOWN,
                value=None,
                unit="",
                message=f"❓ Satellite status UNKNOWN - {station} satellite data unavailable"
                if station
                else "❓ Satellite data unavailable",
                performance_data="",
            )

        # Performance data format: satellites=VALUE;WARN:;CRIT:;MIN;MAX
        # Note: WARN: and CRIT: format means "warning if below WARN value"
        perf_data = f"satellites={total_satellites};{cfg.sat_warning}:;{cfg.sat_critical}:;{cfg.sat_min};{cfg.sat_max}"

        # Add constellation breakdown to performance data
        if by_constellation:
            for const, count in by_constellation.items():
                perf_data += f" {const.lower()}={count};;;0;20"

        station_prefix = f"{station}: " if station else ""

        # Build constellation info string
        const_str = ""
        if by_constellation:
            const_str = " (" + ", ".join(f"{k}:{v}" for k, v in by_constellation.items()) + ")"

        if total_satellites < cfg.sat_critical:
            return MetricResult(
                status=HealthStatus.CRITICAL,
                value=total_satellites,
                unit="",
                message=f"❌ Satellite status CRITICAL - {station_prefix}{total_satellites} satellites (<{cfg.sat_critical}){const_str}",
                performance_data=perf_data,
            )
        elif total_satellites < cfg.sat_warning:
            return MetricResult(
                status=HealthStatus.WARNING,
                value=total_satellites,
                unit="",
                message=f"⚠️  Satellite status WARNING - {station_prefix}{total_satellites} satellites (<{cfg.sat_warning}){const_str}",
                performance_data=perf_data,
            )
        else:
            return MetricResult(
                status=HealthStatus.OK,
                value=total_satellites,
                unit="",
                message=f"✅ Satellite status OK - {station_prefix}{total_satellites} satellites{const_str}",
                performance_data=perf_data,
            )

    def check_disk_usage(
        self,
        disk_usage: Optional[float],
        logging_active: bool = True,
        disk_status: Optional[str] = None,
        station: str = "",
    ) -> MetricResult:
        """Check disk usage and logging status.

        Args:
            disk_usage: Disk usage percentage (None if unavailable)
            logging_active: Whether logging is currently active
            disk_status: Disk status string ('ok', 'warning', 'critical', etc.)
            station: Station ID for message formatting

        Returns:
            MetricResult with status, message, and performance data
        """
        cfg = self.config

        station_prefix = f"{station}: " if station else ""

        # Check if logging is active
        if not logging_active:
            return MetricResult(
                status=HealthStatus.CRITICAL,
                value=disk_usage,
                unit="%",
                message=f"❌ Logging status CRITICAL - {station_prefix}logging INACTIVE",
                performance_data="logging=0;;;0;1",
            )

        # Handle explicit disk status strings
        if disk_status is not None:
            status_lower = disk_status.lower()
            if status_lower in {"critical", "error", "full"}:
                return MetricResult(
                    status=HealthStatus.CRITICAL,
                    value=disk_usage,
                    unit="%",
                    message=f"❌ Logging status CRITICAL - {station_prefix}disk {disk_status}",
                    performance_data="logging=1;;;0;1",
                )
            elif status_lower in {"warning", "warn"}:
                perf = "logging=1;;;0;1"
                if disk_usage is not None:
                    perf += f" disk_used={disk_usage}%;{cfg.disk_warning};{cfg.disk_critical};0;100"
                return MetricResult(
                    status=HealthStatus.WARNING,
                    value=disk_usage,
                    unit="%",
                    message=f"⚠️  Logging status WARNING - {station_prefix}disk {disk_status}",
                    performance_data=perf,
                )
            elif status_lower in {"ok", "good", "healthy"}:
                # Explicit OK status from receiver
                perf = "logging=1;;;0;1"
                if disk_usage is not None:
                    perf += f" disk_used={disk_usage}%;{cfg.disk_warning};{cfg.disk_critical};0;100"
                return MetricResult(
                    status=HealthStatus.OK,
                    value=disk_usage,
                    unit="%",
                    message=f"✅ Logging status OK - {station_prefix}logging active",
                    performance_data=perf,
                )

        # Check disk usage percentage if available
        if disk_usage is None:
            return MetricResult(
                status=HealthStatus.UNKNOWN,
                value=None,
                unit="%",
                message=f"❓ Logging status UNKNOWN - {station_prefix}logging data unavailable"
                if station
                else "❓ Logging data unavailable",
                performance_data="",
            )

        # Performance data format
        perf_data = f"logging=1;;;0;1 disk_used={disk_usage}%;{cfg.disk_warning};{cfg.disk_critical};0;100"

        if disk_usage >= cfg.disk_critical:
            return MetricResult(
                status=HealthStatus.CRITICAL,
                value=disk_usage,
                unit="%",
                message=f"❌ Logging status CRITICAL - {station_prefix}disk {disk_usage:.1f}% (>={cfg.disk_critical}%)",
                performance_data=perf_data,
            )
        elif disk_usage >= cfg.disk_warning:
            return MetricResult(
                status=HealthStatus.WARNING,
                value=disk_usage,
                unit="%",
                message=f"⚠️  Logging status WARNING - {station_prefix}disk {disk_usage:.1f}% (>={cfg.disk_warning}%)",
                performance_data=perf_data,
            )
        else:
            return MetricResult(
                status=HealthStatus.OK,
                value=disk_usage,
                unit="%",
                message=f"✅ Logging status OK - {station_prefix}logging active, disk {disk_usage:.1f}%",
                performance_data=perf_data,
            )

    def check_position(
        self,
        fix_mode: Optional[str],
        satellites_used: Optional[int] = None,
        h_accuracy_m: Optional[float] = None,
        v_accuracy_m: Optional[float] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        height: Optional[float] = None,
        station: str = "",
    ) -> MetricResult:
        """Check position fix status.

        Args:
            fix_mode: Position fix mode ('fixed', 'float', 'single', 'none', etc.)
            satellites_used: Number of satellites used in solution
            h_accuracy_m: Horizontal accuracy in meters
            v_accuracy_m: Vertical accuracy in meters
            latitude: Latitude in degrees
            longitude: Longitude in degrees
            height: Height in meters
            station: Station ID for message formatting

        Returns:
            MetricResult with status, message, and performance data
        """
        station_prefix = f"{station}: " if station else ""

        if fix_mode is None:
            return MetricResult(
                status=HealthStatus.UNKNOWN,
                value=None,
                unit="",
                message=f"❓ Station position UNKNOWN - {station_prefix}position data unavailable"
                if station
                else "❓ Position data unavailable",
                performance_data="",
            )

        # Categorize fix modes
        good_fix_modes = {"fixed", "rtk_fixed", "3d", "3d_fix", "standalone"}
        warn_fix_modes = {"float", "rtk_float", "dgps", "single", "2d", "sbas", "ppp"}
        bad_fix_modes = {"none", "no_fix", "invalid"}

        # Determine status based on fix mode
        fix_mode_lower = fix_mode.lower()
        if fix_mode_lower in good_fix_modes:
            status = HealthStatus.OK
            fix_value = 1.0
        elif fix_mode_lower in warn_fix_modes:
            status = HealthStatus.WARNING
            fix_value = 0.5
        elif fix_mode_lower in bad_fix_modes:
            status = HealthStatus.CRITICAL
            fix_value = 0.0
        else:
            status = HealthStatus.WARNING
            fix_value = 0.5

        # Build performance data
        perf_data = f"fix_mode={fix_value};;;0;1"
        if satellites_used is not None:
            perf_data += f" sats_used={satellites_used};;;0;30"
        if h_accuracy_m is not None:
            perf_data += f" h_acc={h_accuracy_m:.3f}m;;;0;10"
        if v_accuracy_m is not None:
            perf_data += f" v_acc={v_accuracy_m:.3f}m;;;0;20"

        # Build message
        if status == HealthStatus.OK:
            message = f"✅ Station position OK - {station_prefix}{fix_mode}"
        elif status == HealthStatus.CRITICAL:
            message = f"❌ Station position CRITICAL - {station_prefix}no fix"
        else:
            message = f"⚠️  Station position WARNING - {station_prefix}{fix_mode} (not fixed)"

        # Add satellites to message
        if satellites_used is not None and status != HealthStatus.UNKNOWN:
            message += f", {satellites_used} sats"

        # Add position to message
        if latitude is not None and longitude is not None and status != HealthStatus.UNKNOWN:
            message += f" @ {latitude:.5f}, {longitude:.5f}"
            if height is not None:
                message += f", {height:.1f}m"

        return MetricResult(
            status=status,
            value=fix_mode,
            unit="",
            message=message,
            performance_data=perf_data,
        )

    def check_ping(
        self,
        is_reachable: bool,
        router_ok: bool = True,
        receiver_ok: bool = True,
        latency_ms: Optional[float] = None,
        packet_loss: Optional[float] = None,
        station: str = "",
    ) -> MetricResult:
        """Check ping/connectivity status.

        Args:
            is_reachable: True if station is reachable
            router_ok: True if router is responding
            receiver_ok: True if receiver is responding
            latency_ms: Optional ping latency in milliseconds
            packet_loss: Optional packet loss percentage
            station: Station ID for message formatting

        Returns:
            MetricResult with status, message, and performance data
        """
        cfg = self.config
        station_suffix = f" - {station}" if station else ""

        # Determine status
        if is_reachable and router_ok and receiver_ok:
            status = HealthStatus.OK
        elif is_reachable and (router_ok or receiver_ok):
            status = HealthStatus.WARNING
        else:
            status = HealthStatus.CRITICAL

        # Build message
        if status == HealthStatus.OK:
            message = f"{status.emoji} GPS Ping OK{station_suffix}"
        elif not router_ok:
            message = f"{status.emoji} Router not responding{station_suffix}"
        elif not receiver_ok:
            message = f"{status.emoji} Receiver not responding{station_suffix}"
        else:
            message = f"{status.emoji} GPS Ping CRITICAL - not reachable{station_suffix}"

        # Add latency to message
        if latency_ms is not None:
            message += f": {latency_ms:.1f}ms"
            if packet_loss is not None:
                message += f", {packet_loss:.1f}% loss"

        # Build performance data
        perf_parts = []
        if latency_ms is not None:
            perf_parts.append(f"ping={latency_ms:.3f}ms;{cfg.ping_warning};{cfg.ping_critical}")
        if packet_loss is not None:
            perf_parts.append(f"packet_loss={packet_loss:.1f}%;{cfg.packet_loss_warning};{cfg.packet_loss_critical}")
        if not perf_parts:
            perf_parts.append(f"reachable={1 if is_reachable else 0};;;0;1")

        return MetricResult(
            status=status,
            value=is_reachable,
            unit="",
            message=message,
            performance_data=" ".join(perf_parts),
        )

    def check_ports(
        self,
        ports_status: Optional[Dict[str, Dict[str, Any]]],
        receiver_type: Optional[str] = None,
        station: str = "",
    ) -> MetricResult:
        """Check receiver port status.

        Determines critical vs warning ports by receiver type:
        - PolaRX5: FTP (2160) or HTTP (8060) down = CRITICAL, control (28784) = WARNING
        - Trimble (NetRS/NetR9/NetR5): HTTP down = CRITICAL
        - Default: Any port down = WARNING, all ports down = CRITICAL

        Args:
            ports_status: Dict with port status info (ftp, http, control)
                         Each port: {'port': int, 'open': bool, 'status': str}
            receiver_type: Receiver type ('PolaRX5', 'NetRS', 'NetR9', etc.)
            station: Station ID for message formatting

        Returns:
            MetricResult with status, message, and performance data
        """
        station_prefix = f"{station}: " if station else ""

        if ports_status is None:
            return MetricResult(
                status=HealthStatus.UNKNOWN,
                value=None,
                unit="",
                message=f"❓ Receiver status UNKNOWN - {station_prefix}port data unavailable"
                if station
                else "❓ Port data unavailable",
                performance_data="",
            )

        # Define critical ports by receiver type
        receiver_type_upper = (receiver_type or "").upper()
        if "POLARX" in receiver_type_upper:
            # PolaRX5: FTP and HTTP are critical for data downloads
            critical_ports = {"ftp", "http"}
        elif any(t in receiver_type_upper for t in ["NETR", "NETRS", "NETR9", "NETR5"]):
            # Trimble: HTTP is critical
            critical_ports = {"http"}
        else:
            # Default: all ports are critical
            critical_ports = {"ftp", "http", "control"}

        # Categorize ports
        ports_open = []
        ports_closed_critical = []
        ports_closed_warning = []
        perf_parts = []

        for port_name, port_info in ports_status.items():
            if port_name == "overall_status":
                continue
            if not isinstance(port_info, dict):
                continue

            is_open = port_info.get("open", False)
            port_num = port_info.get("port", 0)
            port_str = f"{port_name}:{port_num}"

            if is_open:
                ports_open.append(port_str)
                perf_parts.append(f"{port_name}=1;;;0;1")
            else:
                perf_parts.append(f"{port_name}=0;;;0;1")
                if port_name in critical_ports:
                    ports_closed_critical.append(port_str)
                else:
                    ports_closed_warning.append(port_str)

        performance_data = " ".join(perf_parts)

        # Build port status list for message
        port_status_parts = []
        for port_name, port_info in ports_status.items():
            if port_name == "overall_status":
                continue
            if not isinstance(port_info, dict):
                continue
            is_open = port_info.get("open", False)
            port_num = port_info.get("port", 0)
            if is_open:
                port_status_parts.append(f"{port_name}:{port_num} ✓")
            else:
                if port_name in critical_ports:
                    port_status_parts.append(f"{port_name}:{port_num} ✗")
                else:
                    port_status_parts.append(f"{port_name}:{port_num} ⚠")

        ports_summary = ", ".join(port_status_parts)

        # Determine status
        if len(ports_closed_critical) == 0 and len(ports_closed_warning) == 0:
            # All ports OK
            return MetricResult(
                status=HealthStatus.OK,
                value=ports_status,
                unit="",
                message=f"✅ Receiver status OK - {station_prefix}{ports_summary}",
                performance_data=performance_data,
            )
        elif len(ports_closed_critical) > 0:
            # Critical port(s) down
            return MetricResult(
                status=HealthStatus.CRITICAL,
                value=ports_status,
                unit="",
                message=f"❌ Receiver status CRITICAL - {station_prefix}{ports_summary}",
                performance_data=performance_data,
            )
        else:
            # Only non-critical port(s) down (e.g., control port)
            return MetricResult(
                status=HealthStatus.WARNING,
                value=ports_status,
                unit="",
                message=f"⚠️  Receiver status WARNING - {station_prefix}{ports_summary}",
                performance_data=performance_data,
            )

    def calculate_overall_status(self, statuses: list) -> HealthStatus:
        """Calculate overall health status from individual statuses.

        Args:
            statuses: List of HealthStatus values or status strings

        Returns:
            Overall HealthStatus (worst status wins)
        """
        if not statuses:
            return HealthStatus.UNKNOWN

        # Convert strings to HealthStatus
        health_statuses = []
        for s in statuses:
            if isinstance(s, HealthStatus):
                health_statuses.append(s)
            elif isinstance(s, str):
                health_statuses.append(HealthStatus.from_string(s))
            else:
                health_statuses.append(HealthStatus.UNKNOWN)

        # Worst status wins
        if HealthStatus.CRITICAL in health_statuses:
            return HealthStatus.CRITICAL
        elif HealthStatus.WARNING in health_statuses:
            return HealthStatus.WARNING
        elif all(s == HealthStatus.OK for s in health_statuses if s != HealthStatus.UNKNOWN):
            return HealthStatus.OK
        return HealthStatus.UNKNOWN

    def get_status_string(self, status: HealthStatus) -> str:
        """Get simple status string for health data.

        Args:
            status: HealthStatus enum value

        Returns:
            Simple status string ('ok', 'warning', 'critical', 'unknown')
        """
        return status.value
