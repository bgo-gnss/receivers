"""HTTP-based health data extractor for Trimble NetR9/NetRS/NetR5 receivers.

This module fetches health data from Trimble receivers via the /prog/show? HTTP API
and converts it to the standardized health data format matching PolaRX5 output.

Trimble API Endpoints:
- /prog/show?Voltages: Power supply voltage readings
- /prog/show?Temperature: Internal temperature
- /prog/show?TrackingStatus: Satellite tracking information
- /prog/show?Position: Position solution and DOP values
- /prog/show?SerialNumber: Device serial number
- /prog/show?GpsTime: Current GPS time
- /prog/show?RefStation: Reference station configuration
- /prog/show?Antenna: Antenna information
"""

import logging
import re
import socket
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from requests.auth import HTTPBasicAuth


class TrimbleHTTPExtractor:
    """Extract health data from Trimble receivers via /prog/show? HTTP API.

    Provides unified health data extraction for NetR9, NetRS, and NetR5 receivers,
    outputting data in the same standardized format as PolaRX5 health extraction.
    """

    # Real Trimble /prog/show? API endpoints (NetR9/NetR5)
    HEALTH_ENDPOINTS = {
        "voltages": "/prog/show?Voltages",
        "temperature": "/prog/show?Temperature",
        "tracking": "/prog/show?TrackingStatus",
        "position": "/prog/show?Position",
        "serial": "/prog/show?SerialNumber",
        "gpstime": "/prog/show?GpsTime",
        "refstation": "/prog/show?RefStation",
        "antenna": "/prog/show?Antenna",
    }

    # NetRS uses different parameter format for voltage API
    NETRS_VOLTAGE_ENDPOINTS = [
        "/prog/show?voltage&input=1",  # Primary voltage
        "/prog/show?voltage&input=2",  # Secondary voltage
    ]

    # NetRS CGI page as fallback for additional data (uptime, etc.)
    NETRS_ENDPOINTS = {
        "activity": "/perl-scripts/rstatusActivity.cgi",  # Uptime, extra info
    }

    # Voltage thresholds (same as PolaRX5 for consistency)
    VOLTAGE_WARNING = 11.5
    VOLTAGE_CRITICAL = 10.0

    # Temperature thresholds (Celsius)
    TEMP_WARNING = 60.0
    TEMP_CRITICAL = 70.0

    # Satellite tracking thresholds
    SAT_WARNING = 4
    SAT_CRITICAL = 2

    def __init__(
        self,
        host: str,
        station_id: str = "UNKNOWN",
        port: int = 8060,
        receiver_type: str = "NetR9",
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 10,
        ftp_port: Optional[int] = None,
    ):
        """Initialize HTTP health extractor.

        Args:
            host: Receiver hostname or IP address
            station_id: Station identifier for logging
            port: HTTP port (default: 8060 for Trimble receivers)
            receiver_type: Receiver type (NetR9, NetRS, NetR5)
            username: HTTP Basic Auth username (optional)
            password: HTTP Basic Auth password (optional)
            timeout: Request timeout in seconds
            ftp_port: FTP port for connection checking (optional)
        """
        self.host = host
        self.station_id = station_id.upper()
        self.port = port
        self.ftp_port = ftp_port
        self.receiver_type = receiver_type
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout
        self.logger = logging.getLogger(f"receivers.health.trimble.{station_id}")

        # HTTP Basic Auth if credentials provided
        self.auth = None
        if username and password:
            self.auth = HTTPBasicAuth(username, password)

    def extract_health_data(self) -> Dict[str, Any]:
        """Extract health data from all available HTTP endpoints.

        Returns:
            Dictionary with extracted health data in standardized format
            matching the PolaRX5 health data schema.
        """
        start_time = datetime.now(timezone.utc)

        # Initialize health data structure (matching PolaRX5 format)
        health_data = {
            "station_id": self.station_id,
            "receiver_type": self.receiver_type,
            "timestamp": start_time.isoformat().replace("+00:00", "Z"),
            "schema_version": "1.0",
            "connection": {},
            "metrics": {},
            "data_quality": {},
            "network": {},
            "overall_status": "unknown",
            "status_summary": {"healthy": 0, "warning": 0, "critical": 0, "unknown": 0},
            "extraction_metadata": {
                "extraction_time": start_time.isoformat().replace("+00:00", "Z"),
                "data_source": "trimble_http_api",
                "tool_version": "1.0.0",
            },
        }

        statuses = []

        # Test HTTP connection
        conn_status = self._test_connection()
        health_data["connection"]["http_port"] = conn_status
        statuses.append(conn_status.get("status", "unknown"))

        # Check port status and populate metrics["ports"] for CLI display
        # Use HTTP connection test result for HTTP port (more reliable than raw socket)
        port_status = self._check_port_status(http_accessible=conn_status.get("accessible", False))
        if port_status:
            health_data["metrics"]["ports"] = port_status
            # Add port statuses to overall status calculation
            for port_data in port_status.values():
                if port_data.get("open"):
                    statuses.append("ok")
                else:
                    statuses.append("warning")

        # Fetch and parse voltages
        voltage_data = self._fetch_and_parse_voltages()
        if voltage_data:
            health_data["metrics"]["power"] = voltage_data
            statuses.append(voltage_data.get("status", "unknown"))

        # Fetch and parse temperature
        temp_data = self._fetch_and_parse_temperature()
        if temp_data:
            health_data["metrics"]["temperature"] = temp_data
            statuses.append(temp_data.get("status", "unknown"))

        # Fetch and parse tracking status
        tracking_data = self._fetch_and_parse_tracking()
        if tracking_data:
            health_data["metrics"]["satellites"] = tracking_data
            statuses.append(tracking_data.get("status", "unknown"))

        # Fetch and parse position
        position_data = self._fetch_and_parse_position()
        if position_data:
            health_data["metrics"]["position"] = position_data
            # Position quality affects status
            if position_data.get("fix_type") and "3D" not in position_data.get(
                "fix_type", ""
            ):
                statuses.append("warning")

        # Fetch system info (serial, firmware)
        system_data = self._fetch_system_info()
        if system_data:
            health_data["metrics"]["system"] = system_data

        # Trimble doesn't provide these metrics - mark as unavailable
        health_data["metrics"]["cpu_load"] = {"available": False}
        health_data["metrics"]["memory"] = {"available": False}
        health_data["metrics"]["disk"] = {"available": False}
        health_data["metrics"]["uptime"] = {"available": False}

        # Network features not available on Trimble
        health_data["network"]["ntrip_client"] = {"available": False}
        health_data["network"]["ntrip_server"] = {"available": False}

        # Calculate overall status
        health_data["overall_status"] = self._calculate_overall_status(statuses)
        health_data["status_summary"] = self._count_statuses(statuses)

        # Calculate extraction duration
        end_time = datetime.now(timezone.utc)
        duration_ms = int((end_time - start_time).total_seconds() * 1000)
        health_data["extraction_metadata"]["extraction_duration_ms"] = duration_ms

        return health_data

    def _test_connection(self) -> Dict[str, Any]:
        """Test HTTP connection to receiver.

        Returns:
            Connection status dictionary
        """
        start = datetime.now()
        try:
            response = requests.get(
                f"{self.base_url}/prog/show?SerialNumber",
                auth=self.auth,
                timeout=self.timeout,
            )
            duration_ms = int((datetime.now() - start).total_seconds() * 1000)

            if response.status_code == 200:
                return {
                    "status": "ok",
                    "port": self.port,
                    "response_time_ms": duration_ms,
                    "accessible": True,
                }
            else:
                return {
                    "status": "warning",
                    "port": self.port,
                    "response_time_ms": duration_ms,
                    "accessible": False,
                    "error": f"HTTP {response.status_code}",
                }
        except requests.Timeout:
            return {
                "status": "critical",
                "port": self.port,
                "accessible": False,
                "error": f"Timeout after {self.timeout}s",
            }
        except requests.ConnectionError as e:
            return {
                "status": "critical",
                "port": self.port,
                "accessible": False,
                "error": str(e),
            }
        except Exception as e:
            return {
                "status": "critical",
                "port": self.port,
                "accessible": False,
                "error": str(e),
            }

    def _check_port_status(self, http_accessible: bool = False) -> Dict[str, Dict[str, Any]]:
        """Check HTTP and FTP port status.

        Args:
            http_accessible: Whether HTTP API is accessible (from _test_connection result)

        Returns:
            Dictionary with port status for http and ftp (if configured)
        """
        ports = {}

        # Use HTTP connection test result for HTTP port (more reliable than raw socket
        # check when firewalls are involved)
        http_open = http_accessible
        ports["http"] = {
            "port": self.port,
            "open": http_open,
            "status": "ok" if http_open else "critical",
        }

        # Check FTP port if configured
        if self.ftp_port:
            ftp_open = self._check_tcp_port(self.host, self.ftp_port)
            ports["ftp"] = {
                "port": self.ftp_port,
                "open": ftp_open,
                "status": "ok" if ftp_open else "critical",
            }

        return ports

    def _check_tcp_port(self, host: str, port: int) -> bool:
        """Check if a TCP port is reachable.

        Args:
            host: Host to check
            port: Port number to check

        Returns:
            True if port is open, False otherwise
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception as e:
            self.logger.debug(f"Port check failed for {host}:{port}: {e}")
            return False

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
            response = requests.get(url, auth=self.auth, timeout=self.timeout)

            if response.status_code == 200:
                self.logger.debug(
                    f"Successfully fetched {endpoint_name} ({len(response.text)} bytes)"
                )
                return response.text
            elif response.status_code == 404:
                self.logger.debug(
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
            self.logger.error(f"Timeout fetching {endpoint_name} after {self.timeout}s")
            return None
        except requests.ConnectionError as e:
            self.logger.error(f"Connection error fetching {endpoint_name}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error fetching {endpoint_name}: {e}")
            return None

    def _fetch_and_parse_voltages(self) -> Optional[Dict[str, Any]]:
        """Fetch and parse voltage data.

        For NetR9/NetR5 - Response format:
            <Show Voltages>
            port=0 B1 volts=8.36 cap=100%
            port=1 ETH volts=0.00 cap=0%
            port=2 P2 volts=15.06 cap=100%
            <end of Show Voltages>

        For NetRS - Fetched from Activity CGI page (HTML format).

        Returns:
            Parsed voltage data or None
        """
        response = self._fetch_endpoint("voltages")

        # If /prog/show?Voltages fails or returns error, try NetRS Activity page
        if not response or "ERROR" in response:
            return self._fetch_voltages_from_activity_page()

        try:
            # Parse voltage readings: port=X NAME volts=XX.XX cap=XX%
            voltage_pattern = r"port=(\d+)\s+(\w+)\s+volts=([\d.]+)\s+cap=(\d+)%"
            matches = re.findall(voltage_pattern, response)

            if not matches:
                # Try simpler pattern
                simple_pattern = r"([\d.]+)\s*V"
                simple_matches = re.findall(simple_pattern, response)
                if simple_matches:
                    voltages = [float(v) for v in simple_matches]
                    max_voltage = max(voltages)
                    status = self._voltage_status(max_voltage)
                    return {
                        "voltage": max_voltage,
                        "unit": "V",
                        "status": status,
                        "threshold_warning": self.VOLTAGE_WARNING,
                        "threshold_critical": self.VOLTAGE_CRITICAL,
                    }
                # Try NetRS Activity page as fallback
                return self._fetch_voltages_from_activity_page()

            # Parse all port readings
            ports = []
            max_voltage = 0.0
            for port_num, port_name, volts, cap in matches:
                voltage = float(volts)
                ports.append(
                    {
                        "port": int(port_num),
                        "name": port_name,
                        "voltage": voltage,
                        "capacity_percent": int(cap),
                    }
                )
                if voltage > max_voltage:
                    max_voltage = voltage

            status = self._voltage_status(max_voltage)

            return {
                "voltage": max_voltage,
                "unit": "V",
                "status": status,
                "ports": ports,
                "threshold_warning": self.VOLTAGE_WARNING,
                "threshold_critical": self.VOLTAGE_CRITICAL,
            }

        except Exception as e:
            self.logger.error(f"Error parsing voltage response: {e}")
            return self._fetch_voltages_from_activity_page()

    def _fetch_voltages_from_activity_page(self) -> Optional[Dict[str, Any]]:
        """Fetch voltage data from NetRS using input-specific API endpoints.

        NetRS uses different API format: /prog/show?voltage&input=X
        Response format: Voltage input=X volts=XX.XX

        Returns:
            Parsed voltage data or None
        """
        try:
            ports = []
            max_voltage = 0.0
            port_names = ["Primary", "Secondary"]

            for i, endpoint in enumerate(self.NETRS_VOLTAGE_ENDPOINTS):
                url = f"{self.base_url}{endpoint}"
                self.logger.debug(f"Fetching NetRS voltage from: {url}")

                try:
                    response = requests.get(url, auth=self.auth, timeout=self.timeout)

                    if response.status_code != 200:
                        continue

                    text = response.text.strip()
                    # Parse: "Voltage input=X volts=XX.XX"
                    match = re.search(r"volts=([\d.]+)", text)

                    if match:
                        voltage = float(match.group(1))
                        ports.append({
                            "port": i,
                            "name": port_names[i] if i < len(port_names) else f"Input{i+1}",
                            "voltage": voltage,
                        })
                        if voltage > max_voltage:
                            max_voltage = voltage

                        self.logger.debug(f"NetRS voltage input {i+1}: {voltage}V")

                except requests.Timeout:
                    self.logger.debug(f"Timeout fetching voltage input {i+1}")
                except Exception as e:
                    self.logger.debug(f"Error fetching voltage input {i+1}: {e}")

            if not ports:
                self.logger.debug("No voltage readings from NetRS API")
                return None

            status = self._voltage_status(max_voltage)

            return {
                "voltage": max_voltage,
                "unit": "V",
                "status": status,
                "ports": ports,
                "threshold_warning": self.VOLTAGE_WARNING,
                "threshold_critical": self.VOLTAGE_CRITICAL,
            }

        except Exception as e:
            self.logger.error(f"Error fetching NetRS voltage: {e}")
            return None

    def _fetch_uptime_from_activity_page(self) -> Optional[Dict[str, Any]]:
        """Fetch uptime data from NetRS Activity CGI page.

        Response format (HTML):
            <b>Run Time:</b><br />System has been running for 159 days 1 hour 31 minutes

        Returns:
            Parsed uptime data or None
        """
        url = f"{self.base_url}{self.NETRS_ENDPOINTS['activity']}"

        try:
            response = requests.get(url, auth=self.auth, timeout=self.timeout)

            if response.status_code != 200:
                return None

            html = response.text

            # Parse uptime: "running for X days Y hour Z minutes"
            uptime_match = re.search(
                r"running for (\d+) days? (\d+) hours? (\d+) minutes?", html
            )

            if not uptime_match:
                return None

            days = int(uptime_match.group(1))
            hours = int(uptime_match.group(2))
            minutes = int(uptime_match.group(3))

            total_seconds = (days * 86400) + (hours * 3600) + (minutes * 60)

            return {
                "seconds": total_seconds,
                "days": days,
                "hours": hours,
                "minutes": minutes,
                "formatted": f"{days}d {hours}h {minutes}m",
                "source": "activity_page",
            }

        except Exception:
            return None

    def _fetch_and_parse_temperature(self) -> Optional[Dict[str, Any]]:
        """Fetch and parse temperature data.

        Response format:
            Temperature temp=15.3

        Returns:
            Parsed temperature data or None
        """
        response = self._fetch_endpoint("temperature")
        if not response:
            return None

        try:
            # Parse: Temperature temp=XX.X
            match = re.search(r"temp=([\d.]+)", response, re.IGNORECASE)
            if not match:
                # Try alternative patterns
                match = re.search(r"([\d.]+)\s*°?C", response)

            if not match:
                return None

            temperature = float(match.group(1))
            status = self._temperature_status(temperature)

            return {
                "value": temperature,
                "unit": "C",
                "status": status,
                "threshold_warning": self.TEMP_WARNING,
                "threshold_critical": self.TEMP_CRITICAL,
            }

        except Exception as e:
            self.logger.error(f"Error parsing temperature response: {e}")
            return None

    def _fetch_and_parse_tracking(self) -> Optional[Dict[str, Any]]:
        """Fetch and parse satellite tracking status.

        Response format:
            <Show TrackingStatus>
            Prn=9   Sys=GPS Elv=24 Azm=328 IODE=67  URA=2 L1snr=41 L2snr=38
            Prn=31  Sys=GPS Elv=54 Azm=205 IODE=95  URA=2 L1snr=48 L2snr=46 L2Csnr=47
            Prn=17  Sys=GLN Elv=-45 Azm=000 IODE=69  URA=4
            ...
            <end of Show TrackingStatus>

        Returns:
            Parsed tracking data or None
        """
        response = self._fetch_endpoint("tracking")
        if not response:
            return None

        try:
            # Parse satellite lines: Prn=XX Sys=XXX Elv=XX Azm=XXX ...snr=XX
            sat_pattern = r"Prn=(\d+)\s+Sys=(\w+)\s+Elv=([-\d]+)\s+Azm=(\d+)"
            matches = re.findall(sat_pattern, response)

            if not matches:
                return None

            # Count satellites by system (only those with positive elevation = tracking)
            gps_count = 0
            glonass_count = 0
            galileo_count = 0
            beidou_count = 0
            sbas_count = 0
            total_tracking = 0

            satellites = []
            for prn, sys, elv, azm in matches:
                elevation = int(elv)
                # Only count satellites above horizon (elevation > 0)
                if elevation > 0:
                    total_tracking += 1
                    sat_info = {
                        "prn": int(prn),
                        "system": sys,
                        "elevation": elevation,
                        "azimuth": int(azm),
                    }

                    # Try to get SNR for this satellite
                    line_match = re.search(
                        rf"Prn={prn}\s+.*?L1snr=(\d+)", response
                    )
                    if line_match:
                        sat_info["l1_snr"] = int(line_match.group(1))

                    satellites.append(sat_info)

                    if sys == "GPS":
                        gps_count += 1
                    elif sys == "GLN":
                        glonass_count += 1
                    elif sys == "GAL":
                        galileo_count += 1
                    elif sys == "BDS":
                        beidou_count += 1
                    elif sys == "SBS":
                        sbas_count += 1

            status = self._satellite_status(total_tracking)

            return {
                "total": total_tracking,  # CLI expects "total"
                "visible": len(matches),
                "status": status,
                "by_constellation": {  # CLI expects "by_constellation" with proper case
                    "GPS": gps_count,
                    "GLONASS": glonass_count,
                    "Galileo": galileo_count,
                    "BeiDou": beidou_count,
                    "SBAS": sbas_count,
                },
                "satellites": satellites[:20],  # Limit to 20 for JSON size
                "threshold_warning": self.SAT_WARNING,
                "threshold_critical": self.SAT_CRITICAL,
            }

        except Exception as e:
            self.logger.error(f"Error parsing tracking response: {e}")
            return None

    def _fetch_and_parse_position(self) -> Optional[Dict[str, Any]]:
        """Fetch and parse position data.

        Response format:
            <Show Position>
            GpsWeek     2402
            WeekSeconds 137995.2
            Latitude    66.1930960854 deg
            Longitude   -17.1090319429 deg
            Altitude    128.192 meters
            Qualifiers  WGS84,3D,Autonomous
            Satellites  4,5,9,11,16,18,21,25,26,28,29,31
            ClockOffset 0.000005 msec
            ClockDrift  -0.000041 ppm
            VelNorth     0.06 m/sec
            VelEast      0.01 m/sec
            VelUp        0.07 m/sec
            PDOP        1.8
            HDOP        0.8
            VDOP        1.6
            TDOP        0.9
            <end of Show Position>

        Returns:
            Parsed position data or None
        """
        response = self._fetch_endpoint("position")
        if not response:
            return None

        try:
            position_data = {}

            # Parse latitude
            lat_match = re.search(r"Latitude\s+([-\d.]+)", response)
            if lat_match:
                position_data["latitude"] = float(lat_match.group(1))

            # Parse longitude
            lon_match = re.search(r"Longitude\s+([-\d.]+)", response)
            if lon_match:
                position_data["longitude"] = float(lon_match.group(1))

            # Parse altitude (stored as "height" for CLI compatibility)
            alt_match = re.search(r"Altitude\s+([-\d.]+)", response)
            if alt_match:
                position_data["height"] = float(alt_match.group(1))

            # Parse fix type/qualifiers
            qual_match = re.search(r"Qualifiers\s+(\S+)", response)
            if qual_match:
                position_data["fix_type"] = qual_match.group(1)

            # Parse DOP values
            pdop_match = re.search(r"PDOP\s+([\d.]+)", response)
            if pdop_match:
                position_data["pdop"] = float(pdop_match.group(1))

            hdop_match = re.search(r"HDOP\s+([\d.]+)", response)
            if hdop_match:
                position_data["hdop"] = float(hdop_match.group(1))

            vdop_match = re.search(r"VDOP\s+([\d.]+)", response)
            if vdop_match:
                position_data["vdop"] = float(vdop_match.group(1))

            tdop_match = re.search(r"TDOP\s+([\d.]+)", response)
            if tdop_match:
                position_data["tdop"] = float(tdop_match.group(1))

            # Parse clock offset
            clock_match = re.search(r"ClockOffset\s+([-\d.]+)", response)
            if clock_match:
                position_data["clock_offset_ms"] = float(clock_match.group(1))

            # Parse satellite list
            sats_match = re.search(r"Satellites\s+([\d,]+)", response)
            if sats_match:
                sat_list = sats_match.group(1).split(",")
                position_data["satellites_used"] = len(sat_list)

            # Determine status based on fix type (3D fix = ok)
            fix_type = position_data.get("fix_type", "")
            if "3D" in fix_type:
                position_data["status"] = "ok"
            elif "2D" in fix_type:
                position_data["status"] = "warning"
            elif fix_type:
                position_data["status"] = "warning"
            else:
                position_data["status"] = "unknown"

            return position_data if position_data else None

        except Exception as e:
            self.logger.error(f"Error parsing position response: {e}")
            return None

    def _fetch_system_info(self) -> Optional[Dict[str, Any]]:
        """Fetch system information (serial number, antenna, etc).

        Returns:
            System info dictionary or None
        """
        system_info = {}

        # Get serial number
        serial_response = self._fetch_endpoint("serial")
        if serial_response:
            match = re.search(r"sn=(\S+)", serial_response)
            if match:
                system_info["serial_number"] = match.group(1)

        # Get antenna info
        antenna_response = self._fetch_endpoint("antenna")
        if antenna_response:
            name_match = re.search(r'name="([^"]+)"', antenna_response)
            if name_match:
                system_info["antenna_type"] = name_match.group(1)

            height_match = re.search(r"height=([\d.]+)", antenna_response)
            if height_match:
                system_info["antenna_height"] = float(height_match.group(1))

        # Get reference station info
        refstation_response = self._fetch_endpoint("refstation")
        if refstation_response:
            name_match = re.search(r"Name='([^']+)'", refstation_response)
            if name_match:
                system_info["station_name"] = name_match.group(1)

        return system_info if system_info else None

    def _voltage_status(self, voltage: float) -> str:
        """Determine voltage status.

        Args:
            voltage: Voltage reading in volts

        Returns:
            Status string (ok, warning, critical)
        """
        if voltage < self.VOLTAGE_CRITICAL:
            return "critical"
        elif voltage < self.VOLTAGE_WARNING:
            return "warning"
        return "ok"

    def _temperature_status(self, temperature: float) -> str:
        """Determine temperature status.

        Args:
            temperature: Temperature in Celsius

        Returns:
            Status string (ok, warning, critical)
        """
        if temperature > self.TEMP_CRITICAL:
            return "critical"
        elif temperature > self.TEMP_WARNING:
            return "warning"
        return "ok"

    def _satellite_status(self, count: int) -> str:
        """Determine satellite tracking status.

        Args:
            count: Number of satellites tracking

        Returns:
            Status string (good/ok, fair/warning, poor/critical)
        """
        if count < self.SAT_CRITICAL:
            return "critical"
        elif count < self.SAT_WARNING:
            return "warning"
        return "ok"

    def _calculate_overall_status(self, statuses: List[str]) -> str:
        """Calculate overall health status from individual statuses.

        Args:
            statuses: List of status strings

        Returns:
            Overall status (healthy, warning, critical, unknown)
        """
        if not statuses:
            return "unknown"

        if "critical" in statuses:
            return "critical"
        elif "warning" in statuses:
            return "warning"
        elif all(s == "ok" for s in statuses if s != "unknown"):
            return "healthy"
        return "unknown"

    def _count_statuses(self, statuses: List[str]) -> Dict[str, int]:
        """Count statuses by category.

        Args:
            statuses: List of status strings

        Returns:
            Dictionary with status counts
        """
        counts = {"healthy": 0, "warning": 0, "critical": 0, "unknown": 0}
        for status in statuses:
            if status == "ok":
                counts["healthy"] += 1
            elif status == "warning":
                counts["warning"] += 1
            elif status == "critical":
                counts["critical"] += 1
            else:
                counts["unknown"] += 1
        return counts


# Convenience function for quick health check
def extract_trimble_health(
    host: str,
    station_id: str,
    port: int = 8060,
    receiver_type: str = "NetR9",
    username: Optional[str] = None,
    password: Optional[str] = None,
    ftp_port: Optional[int] = None,
) -> Dict[str, Any]:
    """Extract health data from a Trimble receiver.

    Args:
        host: Receiver hostname or IP address
        station_id: Station identifier
        port: HTTP port (default: 8060)
        receiver_type: Receiver type (NetR9, NetRS, NetR5)
        username: HTTP Basic Auth username (optional)
        password: HTTP Basic Auth password (optional)
        ftp_port: FTP port for connection checking (optional)

    Returns:
        Health data dictionary in standardized format
    """
    extractor = TrimbleHTTPExtractor(
        host=host,
        station_id=station_id,
        port=port,
        receiver_type=receiver_type,
        username=username,
        password=password,
        ftp_port=ftp_port,
    )
    return extractor.extract_health_data()
