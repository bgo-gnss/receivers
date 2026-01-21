#!/usr/bin/env python3
"""Icinga 2 API client for GPS receiver monitoring.

Sends passive check results to Icinga monitoring system via REST API.

This module uses the centralized MetricChecker from receivers.health.metrics
for consistent threshold evaluation across all health monitoring components.

Usage:
    from receivers.monitoring.icinga_client import IcingaClient, CheckResult

    client = IcingaClient(
        host="ut-icinga-m-vip.vedur.is",
        port=5665,
        username="icingaweb",
        password="ji5Aeb8oopieGoh"
    )

    result = CheckResult(
        station="ORFC",
        check_name="GPS Ping",
        exit_status=0,
        plugin_output="OK - GPS receiver ORFC is reachable",
        performance_data="ping=80.5ms;1000;5000"
    )

    response = client.send_check_result(result)

API Documentation:
    https://icinga.com/docs/icinga-2/latest/doc/12-icinga2-api/#process-check-result
"""

import json
import logging
import requests
from typing import Optional, Dict, Any
from dataclasses import dataclass
from urllib.parse import quote

from ..health.metrics import MetricChecker, MetricResult, HealthStatus


# Nagios/Icinga exit codes (kept for backward compatibility)
EXIT_OK = 0
EXIT_WARNING = 1
EXIT_CRITICAL = 2
EXIT_UNKNOWN = 3


@dataclass
class CheckResult:
    """Represents a check result to send to Icinga.

    Attributes:
        station: Station ID (e.g., 'ORFC')
        check_name: Check name (e.g., 'GPS Ping', 'GPS Health')
        exit_status: Nagios exit code (0=OK, 1=WARNING, 2=CRITICAL, 3=UNKNOWN)
        plugin_output: Human-readable status message
        performance_data: Optional performance metrics in Nagios format
        check_source: Source host sending the check (default: 'eldey')
    """
    station: str
    check_name: str
    exit_status: int
    plugin_output: str
    performance_data: str = ""
    check_source: str = "eldey"

    def to_service_name(self) -> str:
        """Convert to Icinga service name format.

        Returns:
            Service name in format: {station}.gps.vedur.is!{check_name}
        """
        return f"{self.station.lower()}.gps.vedur.is!{self.check_name}"

    def to_api_payload(self) -> Dict[str, Any]:
        """Convert to Icinga API payload format.

        Returns:
            Dict with exit_status, plugin_output, performance_data, check_source
        """
        return {
            "exit_status": self.exit_status,
            "plugin_output": self.plugin_output,
            "performance_data": self.performance_data,
            "check_source": self.check_source
        }


class IcingaClient:
    """Client for submitting passive check results to Icinga 2 API.

    Handles API communication, error handling, and logging.
    """

    def __init__(
        self,
        host: str = "ut-icinga-m-vip.vedur.is",
        port: int = 5665,
        username: str = "icingaweb",
        password: str = "ji5Aeb8oopieGoh",
        verify_ssl: bool = False,
        timeout: int = 10,
        check_source: str = "eldey"
    ):
        """Initialize Icinga client.

        Args:
            host: Icinga server hostname
            port: Icinga API port (default: 5665)
            username: API username
            password: API password
            verify_ssl: Verify SSL certificates (default: False)
            timeout: Request timeout in seconds
            check_source: Source hostname for check results
        """
        self.base_url = f"https://{host}:{port}/v1"
        self.auth = (username, password)
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.check_source = check_source

        self.logger = logging.getLogger('receivers.monitoring.icinga')

        # Initialize centralized metric checker for consistent threshold evaluation
        self.metric_checker = MetricChecker()

        # Suppress SSL warnings if not verifying
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _send_metric_check(
        self,
        station: str,
        check_name: str,
        result: MetricResult
    ) -> Dict[str, Any]:
        """Send a metric check result to Icinga using MetricResult.

        This is the unified method for sending any metric-based check.

        Args:
            station: Station ID (e.g., 'ORFC')
            check_name: Check name (e.g., 'Station temp', 'Station volt')
            result: MetricResult from MetricChecker evaluation

        Returns:
            API response dict
        """
        check_result = CheckResult(
            station=station,
            check_name=check_name,
            exit_status=result.exit_code,
            plugin_output=result.message,
            performance_data=result.performance_data,
            check_source=self.check_source
        )
        return self.send_check_result(check_result)

    def send_check_result(
        self,
        result: CheckResult,
        raise_on_error: bool = False
    ) -> Dict[str, Any]:
        """Send a passive check result to Icinga.

        Args:
            result: CheckResult object with check details
            raise_on_error: If True, raise exception on API errors

        Returns:
            API response dict with 'success', 'code', 'message', 'response'

        Raises:
            requests.RequestException: If raise_on_error=True and request fails
        """
        service_name = result.to_service_name()
        url_safe_service = quote(service_name, safe='')

        url = f"{self.base_url}/actions/process-check-result?service={url_safe_service}"
        payload = result.to_api_payload()

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

        try:
            self.logger.debug(f"Sending check result to {service_name}")
            self.logger.debug(f"Payload: {json.dumps(payload, indent=2)}")

            response = requests.post(
                url,
                auth=self.auth,
                headers=headers,
                json=payload,
                verify=self.verify_ssl,
                timeout=self.timeout
            )

            # Parse response
            try:
                response_data = response.json()
            except json.JSONDecodeError:
                response_data = {"raw": response.text}

            # Check for success
            if response.status_code == 200:
                self.logger.info(f"✅ Check result sent: {service_name} (exit={result.exit_status})")
                return {
                    "success": True,
                    "code": 200,
                    "message": response_data.get("results", [{}])[0].get("status", "Success"),
                    "response": response_data
                }

            # Handle 404 - service not found
            elif response.status_code == 404:
                self.logger.warning(f"⚠️  Service not found in Icinga: {service_name}")
                return {
                    "success": False,
                    "code": 404,
                    "message": "Service not found in Icinga configuration",
                    "response": response_data
                }

            # Handle other errors
            else:
                error_msg = response_data.get("status", f"HTTP {response.status_code}")
                self.logger.error(f"❌ API error: {error_msg}")

                if raise_on_error:
                    response.raise_for_status()

                return {
                    "success": False,
                    "code": response.status_code,
                    "message": error_msg,
                    "response": response_data
                }

        except requests.RequestException as e:
            self.logger.error(f"❌ Connection error: {e}")

            if raise_on_error:
                raise

            return {
                "success": False,
                "code": None,
                "message": str(e),
                "response": None
            }

    def send_ping_check(
        self,
        station: str,
        is_reachable: bool,
        router_ok: bool = True,
        receiver_ok: bool = True,
        latency_ms: Optional[float] = None,
        packet_loss: Optional[float] = None
    ) -> Dict[str, Any]:
        """Send a GPS Ping check result.

        Args:
            station: Station ID (e.g., 'ORFC')
            is_reachable: True if station is reachable
            router_ok: True if router is responding
            receiver_ok: True if receiver is responding
            latency_ms: Optional ping latency in milliseconds
            packet_loss: Optional packet loss percentage

        Returns:
            API response dict
        """
        result = self.metric_checker.check_ping(
            is_reachable,
            router_ok=router_ok,
            receiver_ok=receiver_ok,
            latency_ms=latency_ms,
            packet_loss=packet_loss,
            station=station
        )
        return self._send_metric_check(station, "GPS Ping", result)

    def send_temperature_check(
        self,
        station: str,
        temperature: Optional[float],
        unit: str = "C",
        warn_threshold: float = 50.0,
        crit_threshold: float = 60.0
    ) -> Dict[str, Any]:
        """Send a Station temp check result.

        Args:
            station: Station ID (e.g., 'THOB')
            temperature: Temperature value (None if unavailable)
            unit: Temperature unit (default: 'C')
            warn_threshold: Warning threshold (default: 50°C) - deprecated, uses centralized thresholds
            crit_threshold: Critical threshold (default: 60°C) - deprecated, uses centralized thresholds

        Returns:
            API response dict

        Note:
            The warn_threshold and crit_threshold parameters are deprecated.
            Thresholds are now managed by the centralized MetricChecker.
        """
        result = self.metric_checker.check_temperature(temperature, station=station, unit=unit)
        return self._send_metric_check(station, "Station temp", result)

    def send_voltage_check(
        self,
        station: str,
        voltage: Optional[float],
        warn_low: float = 12.0,
        crit_low: float = 11.0,
        warn_high: float = 15.0,
        crit_high: float = 16.0
    ) -> Dict[str, Any]:
        """Send a Station volt check result.

        Args:
            station: Station ID (e.g., 'THOB')
            voltage: Voltage value in volts (None if unavailable)
            warn_low: Low warning threshold - deprecated, uses centralized thresholds
            crit_low: Low critical threshold - deprecated, uses centralized thresholds
            warn_high: High warning threshold - deprecated, uses centralized thresholds
            crit_high: High critical threshold - deprecated, uses centralized thresholds

        Returns:
            API response dict

        Note:
            All threshold parameters are deprecated.
            Thresholds are now managed by the centralized MetricChecker.
        """
        result = self.metric_checker.check_voltage(voltage, station=station)
        return self._send_metric_check(station, "Station volt", result)

    def send_satellite_check(
        self,
        station: str,
        total_satellites: Optional[int],
        by_constellation: Optional[Dict[str, int]] = None,
        warn_threshold: int = 8,
        crit_threshold: int = 4
    ) -> Dict[str, Any]:
        """Send a Satellite status check result.

        Args:
            station: Station ID (e.g., 'THOB')
            total_satellites: Total number of satellites tracked
            by_constellation: Dict with counts per constellation (GPS, GLONASS, etc.)
            warn_threshold: Warning if below this count - deprecated, uses centralized thresholds
            crit_threshold: Critical if below this count - deprecated, uses centralized thresholds

        Returns:
            API response dict

        Note:
            Threshold parameters are deprecated.
            Thresholds are now managed by the centralized MetricChecker.
        """
        result = self.metric_checker.check_satellites(
            total_satellites,
            by_constellation=by_constellation,
            station=station
        )
        return self._send_metric_check(station, "Satellite status", result)

    def send_position_check(
        self,
        station: str,
        fix_mode: Optional[str],
        satellites_used: Optional[int] = None,
        h_accuracy_m: Optional[float] = None,
        v_accuracy_m: Optional[float] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        height: Optional[float] = None
    ) -> Dict[str, Any]:
        """Send a Station position check result.

        Args:
            station: Station ID (e.g., 'THOB')
            fix_mode: Position fix mode ('fixed', 'float', 'single', 'none', etc.)
            satellites_used: Number of satellites used in solution
            h_accuracy_m: Horizontal accuracy in meters
            v_accuracy_m: Vertical accuracy in meters
            latitude: Latitude in degrees
            longitude: Longitude in degrees
            height: Height in meters

        Returns:
            API response dict
        """
        result = self.metric_checker.check_position(
            fix_mode,
            satellites_used=satellites_used,
            h_accuracy_m=h_accuracy_m,
            v_accuracy_m=v_accuracy_m,
            latitude=latitude,
            longitude=longitude,
            height=height,
            station=station
        )
        return self._send_metric_check(station, "Station position", result)

    def send_logging_check(
        self,
        station: str,
        disk_status: Optional[str],
        logging_active: bool = True,
        disk_usage_percent: Optional[float] = None,
        warn_disk: float = 80.0,
        crit_disk: float = 90.0
    ) -> Dict[str, Any]:
        """Send a Logging status check result.

        Args:
            station: Station ID (e.g., 'THOB')
            disk_status: Disk/logging status ('ok', 'warning', 'critical', 'error')
            logging_active: Whether logging is currently active
            disk_usage_percent: Disk usage percentage
            warn_disk: Warning threshold for disk usage - deprecated, uses centralized thresholds
            crit_disk: Critical threshold for disk usage - deprecated, uses centralized thresholds

        Returns:
            API response dict

        Note:
            Disk threshold parameters are deprecated.
            Thresholds are now managed by the centralized MetricChecker.
        """
        result = self.metric_checker.check_disk_usage(
            disk_usage_percent,
            logging_active=logging_active,
            disk_status=disk_status,
            station=station
        )
        return self._send_metric_check(station, "Logging status", result)

    def send_receiver_status_check(
        self,
        station: str,
        ports_status: Optional[Dict[str, Dict[str, Any]]],
        receiver_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """Send a Receiver status check result.

        Critical vs Warning ports by receiver type:
        - PolaRX5: FTP (2160) or HTTP (8060) down = CRITICAL, control (28784) = WARNING
        - Trimble (NetRS/NetR9/NetR5): HTTP down = CRITICAL
        - Default: Any port down = WARNING, all ports down = CRITICAL

        Args:
            station: Station ID (e.g., 'THOB')
            ports_status: Dict with port status info (ftp, http, control)
                          Each port: {'port': int, 'open': bool, 'status': str}
            receiver_type: Receiver type ('PolaRX5', 'NetRS', 'NetR9', etc.)

        Returns:
            API response dict
        """
        result = self.metric_checker.check_ports(
            ports_status,
            receiver_type=receiver_type,
            station=station
        )
        return self._send_metric_check(station, "Receiver status", result)

    def send_cpu_check(
        self,
        station: str,
        cpu_load: Optional[int]
    ) -> Dict[str, Any]:
        """Send a CPU load check result.

        Args:
            station: Station ID (e.g., 'THOB')
            cpu_load: CPU load percentage (None if unavailable)

        Returns:
            API response dict
        """
        result = self.metric_checker.check_cpu_load(cpu_load, station=station)
        return self._send_metric_check(station, "CPU load", result)

    def send_uptime_check(
        self,
        station: str,
        uptime_seconds: Optional[int]
    ) -> Dict[str, Any]:
        """Send an uptime check result.

        Useful for detecting receiver restarts. Warning if uptime < 1 hour,
        OK otherwise.

        Args:
            station: Station ID (e.g., 'THOB')
            uptime_seconds: Receiver uptime in seconds (None if unavailable)

        Returns:
            API response dict
        """
        if uptime_seconds is None:
            return self.send_check_result(CheckResult(
                station=station,
                check_name="Receiver uptime",
                exit_status=EXIT_UNKNOWN,
                plugin_output=f"❓ Receiver uptime UNKNOWN - {station} uptime unavailable",
                performance_data="",
                check_source=self.check_source
            ))

        # Convert to human-readable
        days = uptime_seconds // 86400
        hours = (uptime_seconds % 86400) // 3600
        minutes = (uptime_seconds % 3600) // 60

        if days > 0:
            uptime_str = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            uptime_str = f"{hours}h {minutes}m"
        else:
            uptime_str = f"{minutes}m"

        # Warning if recently restarted (< 1 hour)
        if uptime_seconds < 3600:
            exit_status = EXIT_WARNING
            message = f"⚠️  Receiver uptime WARNING - {station} recently restarted ({uptime_str})"
        else:
            exit_status = EXIT_OK
            message = f"✅ Receiver uptime OK - {station} up {uptime_str}"

        performance_data = f"uptime={uptime_seconds}s;3600:;0:;0"

        return self.send_check_result(CheckResult(
            station=station,
            check_name="Receiver uptime",
            exit_status=exit_status,
            plugin_output=message,
            performance_data=performance_data,
            check_source=self.check_source
        ))

    def send_gps_ping_from_json(
        self,
        station: str,
        health_data: Optional[Dict[str, Any]],
        connection_error: Optional[str] = None
    ) -> Dict[str, Any]:
        """Send a GPS Ping check result - simple connectivity check.

        GPS Ping only checks if the host is reachable (any port responds).
        It does NOT check receiver status or overall health - that's for
        the Receiver status check.

        Args:
            station: Station ID (e.g., 'THOB')
            health_data: Health data dict if successful, None if failed
            connection_error: Error message if connection failed

        Returns:
            API response dict
        """
        if health_data is None:
            # No health data at all - host unreachable
            exit_status = EXIT_CRITICAL
            message = f"❌ GPS Ping CRITICAL - {station} unreachable"
            if connection_error:
                message += f": {connection_error}"
            performance_data = "reachable=0;;;0;1"
        else:
            # Check if we have any connection to the host
            connection = health_data.get('connection', {})
            tcp_status = connection.get('tcp', {}).get('status', 'unknown')
            host = connection.get('tcp', {}).get('host')

            # Also check if any port responded (from metrics.ports)
            metrics = health_data.get('metrics', {})
            ports = metrics.get('ports', {})
            any_port_open = any(
                p.get('open', False) for p in ports.values()
                if isinstance(p, dict)
            )

            if tcp_status == 'ok' or any_port_open:
                # Host is reachable - GPS Ping is OK
                exit_status = EXIT_OK
                message = f"✅ GPS Ping OK - {station} responding"
                performance_data = "reachable=1;;;0;1"
                if host:
                    message += f" @ {host}"
            else:
                # No connection at all
                exit_status = EXIT_CRITICAL
                message = f"❌ GPS Ping CRITICAL - {station} not responding"
                performance_data = "reachable=0;;;0;1"

        result = CheckResult(
            station=station,
            check_name="GPS Ping",
            exit_status=exit_status,
            plugin_output=message,
            performance_data=performance_data,
            check_source=self.check_source
        )

        return self.send_check_result(result)

    def send_health_from_json(
        self,
        health_data: Dict[str, Any],
        checks: Optional[list] = None
    ) -> Dict[str, Dict[str, Any]]:
        """Send multiple check results from health JSON data.

        Extracts metrics from health JSON and sends the appropriate checks.

        Args:
            health_data: Health data dict from `receivers health --json`
            checks: List of checks to send. If None, sends all available.
                   Options: 'ping', 'temp', 'volt', 'cpu', 'uptime', 'satellites',
                           'position', 'logging', 'receiver_status'

        Returns:
            Dict mapping check name to API response
        """
        station = health_data.get('station_id', 'UNKNOWN')
        metrics = health_data.get('metrics', {})
        data_quality = health_data.get('data_quality', {})

        available_checks = checks or [
            'ping', 'temp', 'volt', 'cpu', 'uptime', 'satellites',
            'position', 'logging', 'receiver_status'
        ]

        results = {}

        if 'ping' in available_checks:
            results['GPS Ping'] = self.send_gps_ping_from_json(
                station=station,
                health_data=health_data
            )

        if 'temp' in available_checks:
            temp_data = metrics.get('temperature', {})
            results['Station temp'] = self.send_temperature_check(
                station=station,
                temperature=temp_data.get('value'),
                unit=temp_data.get('unit', 'C')
            )

        if 'volt' in available_checks:
            # Voltage is in metrics.power.voltage (from PowerStatus SBF block)
            power_data = metrics.get('power', {})
            voltage = power_data.get('voltage')
            if voltage is not None:
                results['Station volt'] = self.send_voltage_check(
                    station=station,
                    voltage=voltage
                )

        if 'cpu' in available_checks:
            cpu_data = metrics.get('cpu_load', {})
            cpu_percent = cpu_data.get('percent') if isinstance(cpu_data, dict) else None
            results['CPU load'] = self.send_cpu_check(
                station=station,
                cpu_load=cpu_percent
            )

        if 'uptime' in available_checks:
            uptime_seconds = metrics.get('uptime_seconds')
            results['Receiver uptime'] = self.send_uptime_check(
                station=station,
                uptime_seconds=uptime_seconds
            )

        if 'satellites' in available_checks:
            sat_data = metrics.get('satellites', {})
            results['Satellite status'] = self.send_satellite_check(
                station=station,
                total_satellites=sat_data.get('total'),
                by_constellation=sat_data.get('by_constellation')
            )

        if 'position' in available_checks:
            pos_data = metrics.get('position', {})
            results['Station position'] = self.send_position_check(
                station=station,
                fix_mode=pos_data.get('fix_mode'),
                satellites_used=pos_data.get('satellites_used'),
                h_accuracy_m=pos_data.get('h_accuracy_m'),
                v_accuracy_m=pos_data.get('v_accuracy_m'),
                latitude=pos_data.get('latitude'),
                longitude=pos_data.get('longitude'),
                height=pos_data.get('height')
            )

        if 'logging' in available_checks:
            disk_data = data_quality.get('disk', {})
            results['Logging status'] = self.send_logging_check(
                station=station,
                disk_status=disk_data.get('status'),
                logging_active=True  # Assume active if we got health data
            )

        if 'receiver_status' in available_checks:
            ports_data = metrics.get('ports', {})
            receiver_type = health_data.get('receiver_type')
            results['Receiver status'] = self.send_receiver_status_check(
                station=station,
                ports_status=ports_data if ports_data else None,
                receiver_type=receiver_type
            )

        return results

    def send_health_check(
        self,
        station: str,
        overall_status: str,
        metrics: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Send a GPS Health check result.

        Args:
            station: Station ID (e.g., 'ORFC')
            overall_status: Overall status ('healthy', 'warning', 'critical')
            metrics: Optional dict with voltage, cpu, temp, satellites, etc.

        Returns:
            API response dict
        """
        # Map status to exit code
        status_map = {
            'healthy': EXIT_OK,
            'ok': EXIT_OK,
            'warning': EXIT_WARNING,
            'critical': EXIT_CRITICAL,
            'error': EXIT_CRITICAL,
            'unknown': EXIT_UNKNOWN
        }
        exit_status = status_map.get(overall_status.lower(), EXIT_UNKNOWN)

        # Build message
        if exit_status == EXIT_OK:
            message = f"✅ GPS Health OK - {station} receiver healthy"
        elif exit_status == EXIT_WARNING:
            message = f"⚠️  GPS Health WARNING - {station} needs attention"
        elif exit_status == EXIT_CRITICAL:
            message = f"❌ GPS Health CRITICAL - {station} has errors"
        else:
            message = f"❓ GPS Health UNKNOWN - {station} status unclear"

        # Build performance data
        perf_data_parts = []
        if metrics:
            if 'voltage' in metrics:
                volt = metrics['voltage']
                perf_data_parts.append(f"voltage={volt}V;12.0;11.0")

            if 'cpu_load' in metrics:
                cpu = metrics['cpu_load']
                perf_data_parts.append(f"cpu={cpu}%;80;90")

            if 'temperature' in metrics:
                temp = metrics['temperature']
                perf_data_parts.append(f"temp={temp}C;60;70")

            if 'satellites' in metrics:
                sats = metrics['satellites']
                perf_data_parts.append(f"sats={sats};4;2")

            if 'disk_usage' in metrics:
                disk = metrics['disk_usage']
                perf_data_parts.append(f"disk={disk}%;80;90")

        performance_data = " ".join(perf_data_parts)

        result = CheckResult(
            station=station,
            check_name="GPS Health",
            exit_status=exit_status,
            plugin_output=message,
            performance_data=performance_data,
            check_source=self.check_source
        )

        return self.send_check_result(result)


def main():
    """Command-line interface for testing Icinga client."""
    import argparse
    import subprocess

    parser = argparse.ArgumentParser(
        description="Send GPS receiver check results to Icinga",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Check types:
  ping              GPS Ping - connectivity check (live data)
  temp              Station temperature (live data)
  volt              Station voltage (live data)
  satellites        Satellite tracking status (live data)
  position          Position fix status (live data)
  logging           Logging/disk status (live data)
  receiver_status   Port connectivity status (live data)
  all               Send all checks using live health data
  live              Send all checks using live health data (alias for 'all')
  health            Overall health summary (test data only)

Examples:
  %(prog)s THOB --check-type all         # Send all checks with live data
  %(prog)s THOB --check-type temp        # Send only temperature check
  %(prog)s THOB --check-type satellites  # Send satellite status
  %(prog)s THOB --check-type ping --status critical  # Test critical ping
"""
    )
    parser.add_argument("station", help="Station ID (e.g., THOB, ORFC)")
    parser.add_argument(
        "--check-type", "-c",
        choices=["ping", "temp", "volt", "satellites", "position",
                 "logging", "receiver_status", "all", "live", "health"],
        default="all",
        help="Check type to send (default: all)"
    )
    parser.add_argument(
        "--status",
        default="ok",
        help="Status for test checks (ok, warning, critical)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without sending")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(levelname)s: %(message)s'
    )

    # Create client
    client = IcingaClient()

    # For live/all checks, fetch health data from receiver
    if args.check_type in ('all', 'live', 'ping', 'temp', 'volt', 'satellites', 'position', 'logging', 'receiver_status'):
        print(f"Fetching health data from {args.station}...")
        try:
            result = subprocess.run(
                ['receivers', 'health', args.station, '--json'],
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode != 0:
                print(f"ERROR: Failed to get health data: {result.stderr}")
                return 1

            health_data = json.loads(result.stdout)
            print(f"Got health data: overall_status={health_data.get('overall_status', 'unknown')}")

            if args.dry_run:
                print("\n=== DRY RUN - Would send the following checks ===")
                print(f"Station: {health_data.get('station_id')}")
                print(f"Temperature: {health_data.get('metrics', {}).get('temperature', {})}")
                print(f"Satellites: {health_data.get('metrics', {}).get('satellites', {})}")
                print(f"Position: {health_data.get('metrics', {}).get('position', {})}")
                print(f"Ports: {health_data.get('metrics', {}).get('ports', {})}")
                print(f"Disk: {health_data.get('data_quality', {}).get('disk', {})}")
                return 0

            # Determine which checks to send
            if args.check_type in ('all', 'live'):
                checks = ['ping', 'temp', 'satellites', 'position', 'logging', 'receiver_status']
            else:
                checks = [args.check_type]

            # Send checks
            responses = client.send_health_from_json(health_data, checks=checks)

            # Print results
            print(f"\n=== Results for {args.station} ===")
            all_success = True
            for check_name, response in responses.items():
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                msg = response.get("message", "Unknown")
                print(f"{status} {check_name}: {code} - {msg}")
                if not response.get("success"):
                    all_success = False

            return 0 if all_success else 1

        except subprocess.TimeoutExpired:
            print("ERROR: Timeout fetching health data")
            return 1
        except json.JSONDecodeError as e:
            print(f"ERROR: Failed to parse health JSON: {e}")
            return 1
        except FileNotFoundError:
            print("ERROR: 'receivers' command not found")
            return 1

    # Test data for ping/health checks
    if args.check_type == "ping":
        is_ok = args.status.lower() == "ok"
        response = client.send_ping_check(
            station=args.station,
            is_reachable=is_ok,
            router_ok=is_ok,
            receiver_ok=is_ok,
            latency_ms=80.5 if is_ok else None
        )
    else:  # health
        response = client.send_health_check(
            station=args.station,
            overall_status=args.status,
            metrics={"voltage": 13.2, "cpu_load": 45, "temperature": 42}
        )

    # Print response
    print(f"\nResponse: {json.dumps(response, indent=2)}")

    return 0 if response["success"] else 1


if __name__ == "__main__":
    exit(main())
