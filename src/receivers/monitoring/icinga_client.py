#!/usr/bin/env python3
"""Icinga 2 API client for GPS receiver monitoring.

Sends passive check results to Icinga monitoring system via REST API.

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


# Nagios/Icinga exit codes
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

        # Suppress SSL warnings if not verifying
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
        # Determine exit status
        if is_reachable and router_ok and receiver_ok:
            exit_status = EXIT_OK
            status_icon = "✅"
        elif is_reachable and (router_ok or receiver_ok):
            exit_status = EXIT_WARNING
            status_icon = "⚠️"
        else:
            exit_status = EXIT_CRITICAL
            status_icon = "❌"

        # Build message
        if is_reachable and router_ok and receiver_ok:
            message = f"{status_icon} GPS Ping OK"
        elif not router_ok:
            message = f"{status_icon} Router not responding"
        elif not receiver_ok:
            message = f"{status_icon} Receiver not responding"
        else:
            message = f"{status_icon} GPS Ping CRITICAL - not reachable"

        # Add latency to message
        if latency_ms is not None:
            message += f": {latency_ms:.1f}ms"
            if packet_loss is not None:
                message += f", {packet_loss:.1f}% loss"

        # Build performance data
        perf_data_parts = []
        if latency_ms is not None:
            perf_data_parts.append(f"ping={latency_ms:.3f}ms;1000;5000")
        if packet_loss is not None:
            perf_data_parts.append(f"packet_loss={packet_loss:.1f}%;20;50")

        performance_data = " ".join(perf_data_parts)

        result = CheckResult(
            station=station,
            check_name="GPS Ping",
            exit_status=exit_status,
            plugin_output=message,
            performance_data=performance_data,
            check_source=self.check_source
        )

        return self.send_check_result(result)

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

    parser = argparse.ArgumentParser(description="Send check results to Icinga")
    parser.add_argument("station", help="Station ID (e.g., ORFC)")
    parser.add_argument(
        "--check-type",
        choices=["ping", "health"],
        default="ping",
        help="Check type to send"
    )
    parser.add_argument("--status", default="ok", help="Status (ok, warning, critical)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(levelname)s: %(message)s'
    )

    # Create client
    client = IcingaClient()

    # Send check
    if args.check_type == "ping":
        is_ok = args.status.lower() == "ok"
        response = client.send_ping_check(
            station=args.station,
            is_reachable=is_ok,
            router_ok=is_ok,
            receiver_ok=is_ok,
            latency_ms=80.5 if is_ok else None
        )
    else:
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
