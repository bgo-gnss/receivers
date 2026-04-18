"""Health data parser for Trimble NetR9/NetRS receivers.

Parses HTTP API responses from Trimble receivers and converts them to standardized
health data format for consistency across receiver types.
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union


class TrimbleHealthParser:
    """Parser for Trimble receiver health data from HTTP API responses."""

    def __init__(self, station_id: str, receiver_type: str):
        """Initialize health parser.

        Args:
            station_id: Station identifier
            receiver_type: Receiver type (NetR9 or NetRS)
        """
        self.station_id = station_id.upper()
        self.receiver_type = receiver_type
        self.logger = logging.getLogger(f"receivers.trimble.health.{self.station_id}")

    def parse_voltage_response(self, response_text: str) -> Dict[str, Any]:
        """Parse voltage information from HTTP response.

        Based on old NetR9 getVolt() implementation that looks for voltage values
        in the HTTP response and finds the maximum voltage reading.

        Args:
            response_text: Raw HTTP response text

        Returns:
            Dictionary with voltage metrics
        """
        try:
            # Extract voltage values using regex (pattern from old system)
            voltage_pattern = r"(\d+\.?\d*)\s*V"
            voltage_matches = re.findall(voltage_pattern, response_text, re.IGNORECASE)

            if not voltage_matches:
                return {
                    "status": "unknown",
                    "error": "No voltage readings found in response",
                }

            # Convert to float and find maximum (like old system)
            voltages = [float(v) for v in voltage_matches]
            max_voltage = max(voltages)

            # Determine status based on voltage thresholds
            if max_voltage < 10.0:
                status = "critical"
            elif max_voltage < 11.5:
                status = "warning"
            else:
                status = "ok"

            return {
                "value": max_voltage,
                "unit": "V",
                "status": status,
                "all_readings": voltages,
                "count": len(voltages),
            }

        except Exception as e:
            self.logger.error(f"Error parsing voltage response: {e}")
            return {"status": "error", "error": str(e)}

    def parse_temperature_response(self, response_text: str) -> Dict[str, Any]:
        """Parse temperature information from HTTP response.

        Args:
            response_text: Raw HTTP response text

        Returns:
            Dictionary with temperature metrics
        """
        try:
            # Extract temperature values using regex
            temp_patterns = [
                r"(\d+\.?\d*)\s*°?C",
                r"(\d+\.?\d*)\s*deg",
                r"Temperature:\s*(\d+\.?\d*)",
            ]

            temperatures = []
            for pattern in temp_patterns:
                matches = re.findall(pattern, response_text, re.IGNORECASE)
                if matches:
                    temperatures.extend([float(t) for t in matches])

            if not temperatures:
                return {
                    "status": "unknown",
                    "error": "No temperature readings found in response",
                }

            # Use maximum temperature
            max_temp = max(temperatures)

            # Determine status based on temperature thresholds
            if max_temp > 70.0:
                status = "critical"
            elif max_temp > 60.0:
                status = "warning"
            else:
                status = "ok"

            return {
                "value": max_temp,
                "unit": "C",
                "status": status,
                "all_readings": temperatures,
                "count": len(temperatures),
            }

        except Exception as e:
            self.logger.error(f"Error parsing temperature response: {e}")
            return {"status": "error", "error": str(e)}

    def parse_logging_response(self, response_text: str) -> Dict[str, Any]:
        """Parse logging status from HTTP response.

        Args:
            response_text: Raw HTTP response text

        Returns:
            Dictionary with logging status
        """
        try:
            # Look for logging status indicators
            logging_active = any(
                [
                    "logging" in response_text.lower(),
                    "recording" in response_text.lower(),
                    "active" in response_text.lower(),
                    "enabled" in response_text.lower(),
                ]
            )

            # Look for error indicators
            has_errors = any(
                [
                    "error" in response_text.lower(),
                    "failed" in response_text.lower(),
                    "stopped" in response_text.lower(),
                    "disabled" in response_text.lower(),
                ]
            )

            if has_errors:
                status = "error"
            elif logging_active:
                status = "active"
            else:
                status = "inactive"

            return {
                "status": status,
                "logging_active": logging_active,
                "has_errors": has_errors,
                "raw_response": response_text[:200],  # First 200 chars for debugging
            }

        except Exception as e:
            self.logger.error(f"Error parsing logging response: {e}")
            return {"status": "error", "error": str(e)}

    def parse_sessions_response(self, response_text: str) -> Dict[str, Any]:
        """Parse session information from HTTP response.

        Args:
            response_text: Raw HTTP response text

        Returns:
            Dictionary with session metrics
        """
        try:
            # Count active sessions (simplified parsing)
            session_count = response_text.lower().count("active")

            # Look for session status indicators
            sessions_running = session_count > 0

            return {
                "active_sessions": session_count,
                "status": "active" if sessions_running else "inactive",
                "sessions_detected": sessions_running,
            }

        except Exception as e:
            self.logger.error(f"Error parsing sessions response: {e}")
            return {"status": "error", "error": str(e)}

    def parse_tracking_response(self, response_text: str) -> Dict[str, Any]:
        """Parse satellite tracking from HTTP response.

        Args:
            response_text: Raw HTTP response text

        Returns:
            Dictionary with tracking metrics
        """
        try:
            # Extract satellite count using regex
            sat_patterns = [
                r"(\d+)\s*satellites?",
                r"SVs?\s*:\s*(\d+)",
                r"tracking\s*(\d+)",
            ]

            satellite_counts = []
            for pattern in sat_patterns:
                matches = re.findall(pattern, response_text, re.IGNORECASE)
                if matches:
                    satellite_counts.extend([int(s) for s in matches])

            if not satellite_counts:
                return {
                    "status": "unknown",
                    "error": "No satellite tracking data found",
                }

            # Use maximum satellite count
            max_sats = max(satellite_counts)

            # Determine tracking status
            if max_sats < 4:
                status = "poor"
            elif max_sats < 8:
                status = "fair"
            else:
                status = "good"

            return {
                "satellites": max_sats,
                "status": status,
                "all_counts": satellite_counts,
            }

        except Exception as e:
            self.logger.error(f"Error parsing tracking response: {e}")
            return {"status": "error", "error": str(e)}

    def create_standard_health_report(
        self, health_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create standardized health report from parsed data.

        Args:
            health_data: Dictionary containing parsed health metrics

        Returns:
            Standardized health report format
        """
        # Determine overall status
        statuses = []
        for _key, data in health_data.items():
            if isinstance(data, dict) and "status" in data:
                if data["status"] == "critical":
                    statuses.append("critical")
                elif data["status"] == "warning":
                    statuses.append("warning")
                elif data["status"] in ["ok", "active", "good"]:
                    statuses.append("healthy")
                else:
                    statuses.append("unknown")

        # Overall status priority: critical > warning > unknown > healthy
        if "critical" in statuses:
            overall_status = "critical"
        elif "warning" in statuses:
            overall_status = "warning"
        elif "unknown" in statuses:
            overall_status = "unknown"
        else:
            overall_status = "healthy"

        return {
            "station_id": self.station_id,
            "receiver_type": self.receiver_type,
            "timestamp": datetime.now(),
            "overall_status": overall_status,
            "metrics": health_data,
            "data_source": "http_api",
        }
