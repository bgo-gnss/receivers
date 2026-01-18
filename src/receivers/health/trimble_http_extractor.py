"""HTTP-based health data extractor for Trimble NetR9/NetRS receivers.

This module fetches health data from Trimble receivers via HTTP API endpoints
and converts it to the standardized health data format.

HTTP Endpoints Used:
- /status: Overall receiver status
- /voltage: Power supply voltage
- /temperature: Internal temperature
- /tracking: Satellite tracking information
- /logging: Logging session status
- /sessions: Active session information
"""

import logging
import requests
from typing import Dict, Any, Optional
from datetime import datetime, timezone


class TrimbleHTTPExtractor:
    """Extract health data from Trimble receivers via HTTP API."""

    # HTTP endpoints for health data
    HEALTH_ENDPOINTS = {
        "status": "/status",
        "voltage": "/voltage",
        "temperature": "/temperature",
        "tracking": "/tracking",
        "logging": "/logging",
        "sessions": "/sessions",
    }

    def __init__(self, host: str, station_id: str = "UNKNOWN", port: int = 80):
        """Initialize HTTP health extractor.

        Args:
            host: Receiver hostname or IP address
            station_id: Station identifier for logging
            port: HTTP port (default: 80)
        """
        self.host = host
        self.station_id = station_id
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.logger = logging.getLogger(f"receivers.health.trimble.{station_id}")
        self.timeout = 10  # seconds

    def extract_health_data(self) -> Dict[str, Any]:
        """Extract health data from all available HTTP endpoints.

        Returns:
            Dictionary with extracted health data in standardized format
        """
        from ..trimble.health_parser import TrimbleHealthParser

        # Determine receiver type (will be set by caller, defaulting to NetR9)
        receiver_type = "NetR9"
        parser = TrimbleHealthParser(
            station_id=self.station_id, receiver_type=receiver_type
        )

        health_data = {
            "extraction_time": datetime.now(timezone.utc).isoformat() + "Z",
            "metrics": {},
            "data_quality": {},
            "network": {},
        }

        # Fetch and parse voltage
        voltage_response = self._fetch_endpoint("voltage")
        if voltage_response:
            voltage_data = parser.parse_voltage_response(voltage_response)
            if voltage_data.get("status") != "error":
                health_data["metrics"]["power"] = {
                    "voltage": voltage_data.get("value"),
                    "unit": voltage_data.get("unit", "V"),
                    "status": self._map_status(voltage_data.get("status")),
                }

        # Fetch and parse temperature
        temp_response = self._fetch_endpoint("temperature")
        if temp_response:
            temp_data = parser.parse_temperature_response(temp_response)
            if temp_data.get("status") != "error":
                health_data["metrics"]["temperature"] = {
                    "value": temp_data.get("value"),
                    "unit": temp_data.get("unit", "C"),
                    "status": self._map_status(temp_data.get("status")),
                }

        # Fetch and parse tracking
        tracking_response = self._fetch_endpoint("tracking")
        if tracking_response:
            tracking_data = parser.parse_tracking_response(tracking_response)
            if tracking_data.get("status") != "error":
                health_data["data_quality"]["satellite_tracking"] = {
                    "satellites": tracking_data.get("satellites"),
                    "status": self._map_tracking_status(tracking_data.get("status")),
                }

        # Fetch and parse logging
        logging_response = self._fetch_endpoint("logging")
        if logging_response:
            logging_data = parser.parse_logging_response(logging_response)
            if logging_data.get("status") != "error":
                health_data["data_quality"]["logging"] = {
                    "active": logging_data.get("logging_active", False),
                    "has_errors": logging_data.get("has_errors", False),
                    "status": self._map_logging_status(logging_data.get("status")),
                }

        # Fetch and parse sessions
        sessions_response = self._fetch_endpoint("sessions")
        if sessions_response:
            sessions_data = parser.parse_sessions_response(sessions_response)
            if sessions_data.get("status") != "error":
                health_data["data_quality"]["sessions"] = {
                    "active_count": sessions_data.get("active_sessions", 0),
                    "status": self._map_status(sessions_data.get("status")),
                }

        return health_data

    def _fetch_endpoint(self, endpoint_name: str) -> Optional[str]:
        """Fetch data from HTTP endpoint.

        Args:
            endpoint_name: Name of endpoint to fetch (from HEALTH_ENDPOINTS)

        Returns:
            Response text or None if fetch failed
        """
        if endpoint_name not in self.HEALTH_ENDPOINTS:
            self.logger.warning(f"Unknown endpoint: {endpoint_name}")
            return None

        endpoint_path = self.HEALTH_ENDPOINTS[endpoint_name]
        url = f"{self.base_url}{endpoint_path}"

        try:
            self.logger.debug(f"Fetching {endpoint_name} from {url}")

            response = requests.get(url, timeout=self.timeout)

            if response.status_code == 200:
                self.logger.debug(
                    f"Successfully fetched {endpoint_name} ({len(response.text)} bytes)"
                )
                return response.text
            elif response.status_code == 404:
                self.logger.warning(
                    f"Endpoint not found: {endpoint_name} (404) - "
                    f"receiver may not support this endpoint"
                )
                return None
            else:
                self.logger.warning(
                    f"HTTP {response.status_code} for {endpoint_name}: "
                    f"{response.text[:100]}"
                )
                return None

        except requests.Timeout:
            self.logger.error(
                f"Timeout fetching {endpoint_name} after {self.timeout}s"
            )
            return None
        except requests.ConnectionError as e:
            self.logger.error(f"Connection error fetching {endpoint_name}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error fetching {endpoint_name}: {e}")
            return None

    @staticmethod
    def _map_status(status: str) -> str:
        """Map parser status to standardized status.

        Args:
            status: Status from parser (ok, warning, critical, error, unknown)

        Returns:
            Standardized status (ok, warning, critical, unknown)
        """
        status_map = {
            "ok": "ok",
            "warning": "warning",
            "critical": "critical",
            "error": "critical",
            "unknown": "unknown",
            "active": "ok",
            "inactive": "warning",
            "good": "ok",
            "fair": "warning",
            "poor": "critical",
        }
        return status_map.get(status.lower(), "unknown")

    @staticmethod
    def _map_tracking_status(status: str) -> str:
        """Map tracking status to standardized status.

        Args:
            status: Tracking status (good, fair, poor)

        Returns:
            Standardized status
        """
        tracking_map = {"good": "ok", "fair": "ok", "poor": "warning", "error": "critical"}
        return tracking_map.get(status.lower(), "unknown")

    @staticmethod
    def _map_logging_status(status: str) -> str:
        """Map logging status to standardized status.

        Args:
            status: Logging status (active, inactive, error)

        Returns:
            Standardized status
        """
        logging_map = {"active": "ok", "inactive": "warning", "error": "critical"}
        return logging_map.get(status.lower(), "unknown")
