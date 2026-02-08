"""Unified status formatting for CLI and Icinga output.

This module provides consistent status formatting across all output formats,
using the centralized MetricChecker for threshold evaluation.

Usage:
    from receivers.health.status_formatter import StatusFormatter

    formatter = StatusFormatter()

    # Format a single metric for terminal display
    output = formatter.format_metric("voltage", 13.5, station="THOB")
    print(output)  # "Voltage: OK 13.50 V"

    # Format complete health data for terminal
    lines = formatter.format_health_summary(health_data)
    for line in lines:
        print(line)
"""

from typing import Any, Dict, List, Optional

from .metrics import MetricChecker, MetricResult, HealthStatus, load_thresholds


class StatusFormatter:
    """Unified status formatting for CLI and Icinga output.

    Provides consistent formatting across terminal display, Icinga messages,
    and JSON output using the centralized MetricChecker.
    """

    def __init__(
        self,
        metric_checker: Optional[MetricChecker] = None,
        receiver_type: Optional[str] = None,
    ):
        """Initialize StatusFormatter.

        Args:
            metric_checker: Optional pre-configured MetricChecker.
                           If None, creates one with default or receiver-specific thresholds.
            receiver_type: Optional receiver type for loading type-specific thresholds.
                          Only used if metric_checker is not provided.
        """
        if metric_checker is not None:
            self.checker = metric_checker
        elif receiver_type:
            config = load_thresholds(receiver_type=receiver_type)
            self.checker = MetricChecker(config)
        else:
            self.checker = MetricChecker()

    def format_voltage(
        self,
        voltage: Optional[float],
        station: str = "",
        for_terminal: bool = True,
    ) -> str:
        """Format voltage metric for display.

        Args:
            voltage: Voltage value in volts
            station: Station ID for message formatting
            for_terminal: If True, use compact terminal format; otherwise Icinga format

        Returns:
            Formatted status string
        """
        result = self.checker.check_voltage(voltage, station=station)
        return self._format_result("Voltage", result, "V", for_terminal)

    def format_temperature(
        self,
        temperature: Optional[float],
        station: str = "",
        for_terminal: bool = True,
    ) -> str:
        """Format temperature metric for display.

        Args:
            temperature: Temperature value in Celsius
            station: Station ID for message formatting
            for_terminal: If True, use compact terminal format; otherwise Icinga format

        Returns:
            Formatted status string
        """
        result = self.checker.check_temperature(temperature, station=station)
        return self._format_result("Temp", result, "C", for_terminal)

    def format_cpu_load(
        self,
        cpu_load: Optional[int],
        station: str = "",
        for_terminal: bool = True,
    ) -> str:
        """Format CPU load metric for display.

        Args:
            cpu_load: CPU load percentage
            station: Station ID for message formatting
            for_terminal: If True, use compact terminal format; otherwise Icinga format

        Returns:
            Formatted status string
        """
        result = self.checker.check_cpu_load(cpu_load, station=station)
        return self._format_result("CPU", result, "%", for_terminal)

    def format_satellites(
        self,
        total: Optional[int],
        by_constellation: Optional[Dict[str, int]] = None,
        station: str = "",
        for_terminal: bool = True,
    ) -> str:
        """Format satellite count metric for display.

        Args:
            total: Total number of satellites
            by_constellation: Optional dict with counts per constellation
            station: Station ID for message formatting
            for_terminal: If True, use compact terminal format; otherwise Icinga format

        Returns:
            Formatted status string
        """
        result = self.checker.check_satellites(
            total, by_constellation=by_constellation, station=station
        )

        if for_terminal:
            if result.status == HealthStatus.UNKNOWN:
                return "Satellites: N/A"

            emoji = result.emoji
            const_str = ""
            if by_constellation:
                const_str = " (" + ", ".join(f"{k}:{v}" for k, v in by_constellation.items()) + ")"
            return f"Satellites: {emoji} {total}{const_str}"
        else:
            return result.message

    def format_position(
        self,
        fix_mode: Optional[str],
        satellites_used: Optional[int] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        height: Optional[float] = None,
        station: str = "",
        for_terminal: bool = True,
    ) -> str:
        """Format position metric for display.

        Args:
            fix_mode: Position fix mode
            satellites_used: Number of satellites used
            latitude: Latitude in degrees
            longitude: Longitude in degrees
            height: Height in meters
            station: Station ID for message formatting
            for_terminal: If True, use compact terminal format; otherwise Icinga format

        Returns:
            Formatted status string
        """
        result = self.checker.check_position(
            fix_mode,
            satellites_used=satellites_used,
            latitude=latitude,
            longitude=longitude,
            height=height,
            station=station,
        )

        if for_terminal:
            if result.status == HealthStatus.UNKNOWN:
                return "Position: N/A"

            emoji = result.emoji
            pos_str = f"{fix_mode}"
            if satellites_used:
                pos_str += f", {satellites_used} sats"
            if latitude is not None and longitude is not None:
                pos_str += f" @ {latitude:.5f}, {longitude:.5f}"
                if height is not None:
                    pos_str += f", {height:.1f}m"
            return f"Position: {emoji} {pos_str}"
        else:
            return result.message

    def format_ports(
        self,
        ports_status: Optional[Dict[str, Dict[str, Any]]],
        receiver_type: Optional[str] = None,
        station: str = "",
        for_terminal: bool = True,
    ) -> str:
        """Format port status for display.

        Args:
            ports_status: Dict with port status info
            receiver_type: Receiver type for critical port determination
            station: Station ID for message formatting
            for_terminal: If True, use compact terminal format; otherwise Icinga format

        Returns:
            Formatted status string
        """
        result = self.checker.check_ports(
            ports_status, receiver_type=receiver_type, station=station
        )

        if for_terminal:
            if ports_status is None:
                return "Ports: N/A"

            port_parts = []
            for port_name in ["ftp", "http", "control"]:
                port_data = ports_status.get(port_name, {})
                if isinstance(port_data, dict) and "open" in port_data:
                    is_open = port_data.get("open", False)
                    port_num = port_data.get("port", "?")
                    if is_open:
                        port_parts.append(f"{port_name}:{port_num} OK")
                    else:
                        detail = port_data.get("detail", "closed")
                        port_parts.append(f"{port_name}:{port_num} {detail}")
                else:
                    port_parts.append(f"{port_name}: N/A")
            return "Ports: " + " | ".join(port_parts)
        else:
            return result.message

    def _format_result(
        self,
        metric_name: str,
        result: MetricResult,
        unit: str,
        for_terminal: bool,
    ) -> str:
        """Format a MetricResult for display.

        Args:
            metric_name: Display name for the metric
            result: MetricResult from checker
            unit: Unit suffix for display
            for_terminal: If True, use compact terminal format

        Returns:
            Formatted status string
        """
        if for_terminal:
            if result.status == HealthStatus.UNKNOWN:
                return f"{metric_name}: N/A"

            emoji = result.emoji
            value = result.value
            if value is None:
                return f"{metric_name}: N/A"

            if isinstance(value, float):
                return f"{metric_name}: {emoji} {value:.2f} {unit}"
            else:
                return f"{metric_name}: {emoji} {value}{unit}"
        else:
            return result.message

    def format_health_summary(
        self,
        health_data: Dict[str, Any],
        station_config: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Format complete health data for terminal display.

        Args:
            health_data: Health data dictionary from get_health_status()
            station_config: Optional station configuration for IP display

        Returns:
            List of formatted lines for terminal output
        """
        lines = []

        station_id = health_data.get("station_id", "UNKN")
        receiver_type = health_data.get("receiver_type", "Unknown")
        overall = health_data.get("overall_status", "unknown")

        # Get IP from station config
        ip = "N/A"
        if station_config:
            ip = (
                station_config.get("ip")
                or station_config.get("router", {}).get("ip")
                or station_config.get("host")
                or "N/A"
            )

        # Overall status with emoji
        status_emoji = {
            "healthy": "OK",
            "warning": "WARN",
            "critical": "CRIT",
            "unknown": "?",
        }
        overall_icon = status_emoji.get(overall, "?")

        # Header line
        lines.append(f"{station_id} ({receiver_type}) @ {ip}  [{overall_icon}] {overall.upper()}")

        # Receiver identity (firmware, serial) if available
        identity = health_data.get("receiver_identity", {})
        if identity:
            id_parts = []
            if identity.get("firmware_version"):
                id_parts.append(f"FW: {identity['firmware_version']}")
            if identity.get("serial_number"):
                id_parts.append(f"S/N: {identity['serial_number']}")
            if id_parts:
                lines.append(f"  Identity: {' | '.join(id_parts)}")

        # Metrics
        metrics = health_data.get("metrics", {})

        # Port status
        ports = metrics.get("ports", {})
        if ports:
            lines.append("  " + self.format_ports(ports, receiver_type=receiver_type))

        # Key metrics on one line
        metric_parts = []

        # Voltage
        power = metrics.get("power", {})
        voltage = power.get("voltage")
        if voltage is not None:
            metric_parts.append(self.format_voltage(voltage))

        # Temperature
        temp = metrics.get("temperature", {})
        temp_value = temp.get("value")
        if temp_value is not None:
            metric_parts.append(self.format_temperature(temp_value))

        # CPU
        cpu = metrics.get("cpu_load", {})
        cpu_value = cpu.get("value", cpu.get("percent"))
        if cpu_value is not None:
            metric_parts.append(self.format_cpu_load(cpu_value))

        if metric_parts:
            lines.append("  Metrics: " + " | ".join(metric_parts))

        # Satellites
        sats = metrics.get("satellites", {})
        total_sats = sats.get("total")
        if total_sats is not None:
            by_const = sats.get("by_constellation", {})
            lines.append("  " + self.format_satellites(total_sats, by_constellation=by_const))

        # Position
        pos = metrics.get("position", {})
        fix_mode = pos.get("fix_mode")
        if fix_mode:
            lines.append(
                "  "
                + self.format_position(
                    fix_mode,
                    satellites_used=pos.get("satellites_used"),
                    latitude=pos.get("latitude"),
                    longitude=pos.get("longitude"),
                    height=pos.get("height"),
                )
            )

        return lines

    def get_overall_status(self, health_data: Dict[str, Any]) -> HealthStatus:
        """Get overall health status from health data.

        Args:
            health_data: Health data dictionary

        Returns:
            HealthStatus enum value
        """
        overall = health_data.get("overall_status", "unknown")
        return HealthStatus.from_string(overall)
