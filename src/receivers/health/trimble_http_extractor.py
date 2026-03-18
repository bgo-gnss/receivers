"""HTTP-based health data extractor for Trimble NetR9/NetRS/NetR5 receivers.

This module fetches health data from Trimble receivers via the /prog/show? HTTP API
and converts it to the standardized health data format matching PolaRX5 output.

Uses the centralized MetricChecker from receivers.health.metrics for consistent
threshold evaluation across all health monitoring components.

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

from .metrics import MetricChecker


class TrimbleHTTPExtractor:
    """Extract health data from Trimble receivers via /prog/show? HTTP API.

    Provides unified health data extraction for NetR9, NetRS, and NetR5 receivers,
    outputting data in the same standardized format as PolaRX5 health extraction.

    Uses the centralized MetricChecker for consistent threshold evaluation.
    """

    # Real Trimble /prog/show? API endpoints (NetR9/NetR5)
    HEALTH_ENDPOINTS = {
        "voltages": "/prog/show?Voltages",
        "temperature": "/prog/show?Temperature",
        "tracking": "/prog/show?TrackingStatus",
        "position": "/prog/show?Position",
        "serial": "/prog/show?SerialNumber",
        "firmware": "/prog/show?FirmwareVersion",
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
        self.logger = logging.getLogger(f"receivers.health.{station_id}")

        # Initialize centralized metric checker for consistent threshold evaluation
        from .metrics import load_thresholds
        power_type = None
        try:
            from ..config_utils import get_station_config
            cfg = get_station_config(station_id)
            if cfg:
                power_type = cfg.get("power_type") or None
        except Exception as e:
            self.logger.debug(f"Could not load station config for power_type: {e}")
        config = load_thresholds(receiver_type=receiver_type, power_type=power_type)
        self.metric_checker = MetricChecker(config)

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

        # Fetch merge.xml early — used as fallback for voltage/temp and
        # primary source for uptime + disk on all Trimble receivers.
        merge_xml = self._fetch_merge_xml()

        # NetR5 has very limited /prog/show? support (only SerialNumber works).
        # Skip unsupported endpoints to avoid wasting time on ERROR responses.
        has_prog_show = self.receiver_type != "NetR5"

        # Fetch and parse voltages (/prog/show? first, merge.xml fallback)
        voltage_data = self._fetch_and_parse_voltages() if has_prog_show else None
        if not voltage_data and merge_xml:
            voltage_data = self._parse_voltage_from_merge_xml(merge_xml)
        if voltage_data:
            health_data["metrics"]["power"] = voltage_data
            statuses.append(voltage_data.get("status", "unknown"))

        # Fetch and parse temperature (/prog/show? first, merge.xml fallback)
        temp_data = self._fetch_and_parse_temperature() if has_prog_show else None
        if not temp_data and merge_xml:
            temp_data = self._parse_temperature_from_merge_xml(merge_xml)
        if temp_data:
            health_data["metrics"]["temperature"] = temp_data
            statuses.append(temp_data.get("status", "unknown"))

        # Fetch and parse tracking status (not available on NetR5)
        tracking_data = None
        if has_prog_show:
            tracking_data = self._fetch_and_parse_tracking()
            if tracking_data:
                health_data["metrics"]["satellites"] = tracking_data
                statuses.append(tracking_data.get("status", "unknown"))

        # Fetch and parse position (not available on NetR5)
        position_data = None
        if has_prog_show:
            position_data = self._fetch_and_parse_position()
            if position_data:
                health_data["metrics"]["position"] = position_data
                # Position quality affects status
                if position_data.get("fix_type") and "3D" not in position_data.get(
                    "fix_type", ""
                ):
                    statuses.append("warning")

        # Fallback: if /prog/show? didn't return tracking/position, try posData.xml
        if not tracking_data or not position_data:
            pos_xml = self._fetch_individual_xml("posData.xml")
            if pos_xml:
                if not tracking_data:
                    tracking_data = self._parse_satellites_from_pos_xml(pos_xml)
                    if tracking_data:
                        health_data["metrics"]["satellites"] = tracking_data
                        statuses.append(tracking_data.get("status", "unknown"))
                if not position_data:
                    position_data = self._parse_position_from_pos_xml(pos_xml)
                    if position_data:
                        health_data["metrics"]["position"] = position_data

        # Fetch system info (serial, firmware)
        system_data = self._fetch_system_info()
        if system_data:
            health_data["metrics"]["system"] = system_data

            # Build receiver identity from system info
            identity = {}
            if system_data.get("serial_number"):
                identity["serial_number"] = system_data["serial_number"]
            if system_data.get("firmware_version"):
                identity["firmware_version"] = system_data["firmware_version"]
            if identity:
                identity["receiver_model"] = self.receiver_type
                health_data["receiver_identity"] = identity

        # Trimble doesn't provide these metrics - mark as unavailable
        health_data["metrics"]["cpu_load"] = {"available": False}
        health_data["metrics"]["memory"] = {"available": False}

        # Disk usage from merge.xml dataLogger fileSystem
        disk_data = self._parse_disk_from_merge_xml(merge_xml)
        if disk_data:
            health_data["metrics"]["disk"] = disk_data
            statuses.append(disk_data.get("status", "unknown"))
        else:
            health_data["metrics"]["disk"] = {"available": False}

        # Uptime from merge.xml, fall back to activity CGI (NetRS)
        uptime_data = self._parse_uptime_from_merge_xml(merge_xml)
        activity_html = None
        if not uptime_data and self.receiver_type == "NetRS":
            activity_html = self._fetch_activity_page()
            if activity_html:
                uptime_data = self._parse_uptime_from_activity_html(activity_html)
        if uptime_data:
            health_data["metrics"]["uptime"] = uptime_data
        else:
            health_data["metrics"]["uptime"] = {"available": False}

        # Logging sessions from activity page (NetRS) or merge.xml (NetR9)
        logging_data = None
        if activity_html:
            logging_data = self._parse_logging_from_activity_html(activity_html)
        if not logging_data and merge_xml:
            logging_data = self._parse_logging_from_merge_xml(merge_xml)
        if not logging_data and self.receiver_type != "NetR5" and not activity_html:
            # Last resort: try activity page for non-NetRS Trimble receivers
            activity_html = self._fetch_activity_page()
            if activity_html:
                logging_data = self._parse_logging_from_activity_html(activity_html)
        if logging_data:
            health_data["metrics"]["logging_sessions"] = logging_data

        # Correct port status if any HTTP endpoint succeeded after initial test
        # (receiver may be slow to respond initially but data extraction works)
        http_data_fetched = any([
            voltage_data, temp_data, tracking_data, position_data,
            system_data, merge_xml,
        ])
        if http_data_fetched and "ports" in health_data["metrics"]:
            http_port = health_data["metrics"]["ports"].get("http", {})
            if not http_port.get("open"):
                health_data["metrics"]["ports"]["http"]["open"] = True
                health_data["metrics"]["ports"]["http"]["status"] = "ok"
                # Also fix the connection status
                health_data["connection"]["http_port"]["accessible"] = True
                health_data["connection"]["http_port"]["status"] = "ok"
                # Fix stale status in overall calculation list —
                # the initial connection test status (index 0) is now wrong
                if statuses and statuses[0] in ("critical", "warning"):
                    statuses[0] = "ok"

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
                "status": "warning",
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
            "port": int(self.port),
            "open": http_open,
            "status": "ok" if http_open else "critical",
        }

        # Check FTP port if configured
        if self.ftp_port:
            ftp_open = self._check_tcp_port(self.host, self.ftp_port)
            ports["ftp"] = {
                "port": int(self.ftp_port),
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
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((host, port))
            return result == 0
        except Exception as e:
            self.logger.debug(f"Port check failed for {host}:{port}: {e}")
            return False
        finally:
            if sock is not None:
                sock.close()

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
                        "threshold_warning": self.metric_checker.config.voltage_warning_low,
                        "threshold_critical": self.metric_checker.config.voltage_critical_low,
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
                "threshold_warning": self.metric_checker.config.voltage_warning_low,
                "threshold_critical": self.metric_checker.config.voltage_critical_low,
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
                "threshold_warning": self.metric_checker.config.voltage_warning_low,
                "threshold_critical": self.metric_checker.config.voltage_critical_low,
            }

        except Exception as e:
            self.logger.error(f"Error fetching NetRS voltage: {e}")
            return None

    def _fetch_merge_xml(self) -> Optional[str]:
        """Fetch the merge.xml dynamic data from the Trimble web UI.

        Discovers the CACHEDIR path from the root page, then fetches
        merge.xml with powerData and dataLogger parameters.

        If merge.xml returns empty data (newer ASTRA firmware), falls back
        to individual XML endpoints (powerData.xml, posData.xml) which work
        across all firmware versions.

        Returns:
            Raw XML text or None
        """
        try:
            response = requests.get(
                self.base_url, auth=self.auth, timeout=self.timeout
            )
            if response.status_code != 200:
                return None

            match = re.search(r"(CACHEDIR\d+)", response.text)
            if not match:
                return None

            cache_dir = match.group(1)
            self._cache_dir = cache_dir  # Store for individual XML fallback

            url = f"{self.base_url}/{cache_dir}/xml/dynamic/merge.xml?powerData=&dataLogger="
            response = requests.get(url, auth=self.auth, timeout=self.timeout)
            if response.status_code != 200:
                return None

            xml_text = response.text

            # If merge.xml returned empty/no power data (newer ASTRA firmware),
            # fetch individual XML files which work on all firmware versions
            if "<power>" not in xml_text:
                self.logger.debug("merge.xml has no power data, trying powerData.xml")
                power_xml = self._fetch_individual_xml("powerData.xml")
                if power_xml:
                    # Inject power data into the XML so existing parsers work
                    xml_text = xml_text.replace("</data>", power_xml + "</data>")

            return xml_text

        except Exception as e:
            self.logger.debug(f"Could not fetch merge.xml: {e}")
            return None

    def _fetch_individual_xml(self, filename: str) -> Optional[str]:
        """Fetch an individual dynamic XML file from the Trimble web UI.

        Works on all firmware versions. Individual XML endpoints include:
        - powerData.xml: voltage, temperature, uptime
        - posData.xml: position, satellites, DOP
        - trackingData.xml: detailed satellite tracking

        Args:
            filename: XML filename (e.g., 'powerData.xml')

        Returns:
            Raw XML text or None
        """
        cache_dir = getattr(self, '_cache_dir', None)
        if not cache_dir:
            return None

        try:
            url = f"{self.base_url}/{cache_dir}/xml/dynamic/{filename}"
            response = requests.get(url, auth=self.auth, timeout=self.timeout)
            if response.status_code == 200 and response.text.strip():
                return response.text
        except Exception as e:
            self.logger.debug(f"Could not fetch {filename}: {e}")

        return None

    def _parse_uptime_from_merge_xml(self, xml_text: Optional[str]) -> Optional[Dict[str, Any]]:
        """Parse uptime from merge.xml response.

        XML format: <uptime><day>N</day><hour>N</hour><min>N</min><sec>N</sec></uptime>
        """
        if not xml_text:
            return None

        day_m = re.search(r"<day>(\d+)</day>", xml_text)
        hour_m = re.search(r"<hour>(\d+)</hour>", xml_text)
        min_m = re.search(r"<min>(\d+)</min>", xml_text)
        sec_m = re.search(r"<sec>(\d+)</sec>", xml_text)

        if not day_m:
            return None

        days = int(day_m.group(1))
        hours = int(hour_m.group(1)) if hour_m else 0
        minutes = int(min_m.group(1)) if min_m else 0
        seconds = int(sec_m.group(1)) if sec_m else 0

        total_seconds = (days * 86400) + (hours * 3600) + (minutes * 60) + seconds

        return {
            "seconds": total_seconds,
            "days": days,
            "hours": hours,
            "minutes": minutes,
            "formatted": f"{days}d {hours}h {minutes}m",
            "source": "merge_xml",
        }

    def _parse_disk_from_merge_xml(self, xml_text: Optional[str]) -> Optional[Dict[str, Any]]:
        """Parse disk usage from merge.xml dataLogger fileSystem.

        XML format:
            <fileSystem><name>/Internal</name><size>8315994112</size>
            <available>5763072</available><state>Mounted</state></fileSystem>
        """
        if not xml_text:
            return None

        # Find the /Internal filesystem block
        fs_match = re.search(
            r"<fileSystem>\s*<name>/Internal</name>\s*"
            r"<size>(\d+)</size>\s*<available>(\d+)</available>",
            xml_text,
        )
        if not fs_match:
            return None

        total_bytes = int(fs_match.group(1))
        available_bytes = int(fs_match.group(2))

        if total_bytes == 0:
            return None

        used_bytes = total_bytes - available_bytes
        total_mb = round(total_bytes / (1024 * 1024), 1)
        used_mb = round(used_bytes / (1024 * 1024), 1)
        free_mb = round(available_bytes / (1024 * 1024), 1)
        usage_percent = round((used_bytes / total_bytes) * 100, 1)

        # Evaluate status using metric checker thresholds
        status = "ok"
        if usage_percent > 97:
            status = "critical"
        elif usage_percent > 90:
            status = "warning"

        return {
            "usage_percent": usage_percent,
            "used_mb": used_mb,
            "total_mb": total_mb,
            "free_mb": free_mb,
            "status": status,
            "source": "merge_xml",
        }

    def _parse_voltage_from_merge_xml(
        self, xml_text: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Parse voltage from merge.xml <power> block.

        XML format:
            <power>
              <P1><voltage>19.35</voltage><capacity>100</capacity><active>TRUE</active></P1>
              <P2><voltage>0.60</voltage><capacity>0</capacity></P2>
              <B1><voltage>8.37</voltage><capacity>100</capacity></B1>
            </power>

        Selects the active power port (has <active>TRUE</active>), falling back
        to P1 then P2 if no active marker is found.
        """
        if not xml_text:
            return None

        # First, try to find the active power port (P1 or P2 with <active>TRUE</active>)
        active_match = re.search(
            r"<(P[12])><voltage>([\d.]+)</voltage>.*?<active>TRUE</active>.*?</\1>",
            xml_text, re.DOTALL
        )
        if active_match:
            voltage = float(active_match.group(2))
            port_name = active_match.group(1)
        else:
            # Fall back to P1 then P2 (whichever exists with non-zero voltage)
            for port in ["P1", "P2"]:
                match = re.search(
                    rf"<{port}>\s*<voltage>([\d.]+)</voltage>", xml_text
                )
                if match and float(match.group(1)) > 1.0:
                    voltage = float(match.group(1))
                    port_name = port
                    break
            else:
                return None

        status = self._voltage_status(voltage)

        self.logger.debug(
            f"Voltage from XML ({port_name}): {voltage:.1f}V (status: {status})"
        )

        return {
            "voltage": voltage,
            "status": status,
            "source": "merge_xml",
        }

    def _parse_temperature_from_merge_xml(
        self, xml_text: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Parse temperature from merge.xml <power> block.

        XML format:
            <T1><celsius>48.91</celsius></T1>
        """
        if not xml_text:
            return None

        temp_match = re.search(r"<celsius>([-\d.]+)</celsius>", xml_text)
        if not temp_match:
            return None

        temperature = float(temp_match.group(1))
        status = self._temperature_status(temperature)

        self.logger.debug(
            f"Temperature from merge.xml: {temperature:.1f}°C (status: {status})"
        )

        return {
            "value": temperature,
            "status": status,
            "source": "merge_xml",
        }

    def _parse_logging_from_merge_xml(
        self, xml_text: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Parse logging sessions from merge.xml dataLogger block.

        Each <session> has <name>, <enabled>, <status>, and <filePath>.
        Status values: 0=idle, 2=logging.

        Returns:
            Logging sessions dict with session names and count, or None
        """
        if not xml_text:
            return None

        sessions = re.findall(r"<session>(.*?)</session>", xml_text, re.DOTALL)
        if not sessions:
            return None

        # Map Trimble session names to our canonical names
        session_map = {
            "24hr_15s": "15s_24hr",
            "15s_24hr": "15s_24hr",
            "1hr_1s": "1Hz_1hr",
            "1Hz_1hr": "1Hz_1hr",
            "1hr_15s": "15s_1hr",
            "status_1hr": "status_1hr",
        }

        active = []
        for session_xml in sessions:
            name_m = re.search(r"<name>(.*?)</name>", session_xml)
            enabled_m = re.search(r"<enabled>(\d)</enabled>", session_xml)
            status_m = re.search(r"<status>(\d+)</status>", session_xml)
            path_m = re.search(r"<filePath>(.*?)</filePath>", session_xml)

            if not name_m or not enabled_m:
                continue

            name = name_m.group(1)
            enabled = enabled_m.group(1) == "1"
            is_logging = status_m and status_m.group(1) == "2"
            file_path = path_m.group(1) if path_m else ""

            if enabled and is_logging and file_path:
                canonical = session_map.get(name, name)
                active.append({"session": canonical, "file": file_path})

        if not active:
            return None

        return {
            "active_sessions": len(active),
            "sessions": active,
            "status": "ok",
        }

    def _fetch_activity_page(self) -> Optional[str]:
        """Fetch the Activity CGI page HTML from Trimble receiver.

        Returns:
            HTML string or None
        """
        url = f"{self.base_url}{self.NETRS_ENDPOINTS['activity']}"
        try:
            response = requests.get(url, auth=self.auth, timeout=self.timeout)
            if response.status_code == 200:
                return response.text
        except Exception:
            pass
        return None

    def _parse_uptime_from_activity_html(self, html: str) -> Optional[Dict[str, Any]]:
        """Parse uptime from Activity CGI HTML.

        Expected format:
            System has been running for 159 days 1 hour 31 minutes

        Returns:
            Parsed uptime data or None
        """
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

    def _parse_logging_from_activity_html(self, html: str) -> Optional[Dict[str, Any]]:
        """Parse logging session info from Activity CGI HTML.

        Expected format:
            <td>Session</td><td>24hr_15s</td><td>logging to /202602/a/INSK...T00</td>
            <td>Session</td><td>1hr_1s</td><td>logging to /202602/b/INSK...T00</td>

        Returns:
            Logging sessions dict with session names and count, or None
        """
        sessions = re.findall(
            r"<td>Session</td>\s*<td>([^<]+)</td>\s*<td>logging to ([^<]+)</td>", html
        )
        if not sessions:
            return None

        # Map receiver session names to our canonical names
        session_map = {
            "24hr_15s": "15s_24hr",
            "1hr_1s": "1Hz_1hr",
            "1hr_15s": "15s_1hr",
        }

        active = []
        for name, path in sessions:
            canonical = session_map.get(name, name)
            active.append({"session": canonical, "file": path})

        return {
            "active_sessions": len(active),
            "sessions": active,
            "status": "ok",
        }

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
            match = re.search(r"temp=([-\d.]+)", response, re.IGNORECASE)
            if not match:
                # Try alternative patterns
                match = re.search(r"([-\d.]+)\s*°?C", response)

            if not match:
                return None

            temperature = float(match.group(1))
            status = self._temperature_status(temperature)

            return {
                "value": temperature,
                "unit": "C",
                "status": status,
                "threshold_warning": self.metric_checker.config.temp_warning_high,
                "threshold_critical": self.metric_checker.config.temp_critical_high,
            }

        except Exception as e:
            self.logger.error(f"Error parsing temperature response: {e}")
            return None

    def _fetch_and_parse_tracking(self) -> Optional[Dict[str, Any]]:
        """Fetch and parse satellite tracking status.

        NetR9/NetR5 format:
            <Show TrackingStatus>
            Prn=9   Sys=GPS Elv=24 Azm=328 IODE=67  URA=2 L1snr=41 L2snr=38
            Prn=17  Sys=GLN Elv=-45 Azm=000 IODE=69  URA=4
            <end of Show TrackingStatus>

        NetRS format (GPS-only, no Sys field):
            <ShowTrackingStatus>
            Chan=0  PRN=12  Elv=16   Azm=341 L1snr=40 L2snr=32 L2Csnr=0  IODE=39  URA=2.0
            <end of ShowTrackingStatus>

        Returns:
            Parsed tracking data or None
        """
        response = self._fetch_endpoint("tracking")
        if not response:
            self.logger.warning("TrackingStatus endpoint returned no data")
            return None

        try:
            # Try NetR9/NetR5 format first: Prn=XX Sys=XXX Elv=XX Azm=XXX
            sat_pattern = r"Prn=(\d+)\s+Sys=(\w+)\s+Elv=([-\d]+)\s+Azm=(\d+)"
            matches = re.findall(sat_pattern, response)

            # Fall back to NetRS format: Chan=X PRN=Y Elv=Z Azm=W (GPS-only)
            if not matches:
                netrs_pattern = r"PRN=(\d+)\s+Elv=([-\d]+)\s+Azm=(\d+)"
                netrs_matches = re.findall(netrs_pattern, response)
                if netrs_matches:
                    # Convert to same tuple format with "GPS" as system
                    matches = [(prn, "GPS", elv, azm) for prn, elv, azm in netrs_matches]

            if not matches:
                # Valid response with 0 satellites (e.g. antenna disconnected)
                # vs unrecognized format — check for actual section delimiters,
                # not just substring (error responses like "Unknown Command :
                # show?TrackingStatus" also contain the word "TrackingStatus")
                if "<Show TrackingStatus>" in response or "<ShowTrackingStatus>" in response or "<end of Show TrackingStatus>" in response:
                    self.logger.info("TrackingStatus: 0 satellites tracking")
                    return {
                        "total": 0,
                        "visible": 0,
                        "status": self._satellite_status(0),
                        "by_constellation": {
                            "GPS": 0, "GLONASS": 0, "Galileo": 0,
                            "BeiDou": 0, "SBAS": 0,
                        },
                        "satellites": [],
                        "threshold_warning": self.metric_checker.config.sat_warning,
                        "threshold_critical": self.metric_checker.config.sat_critical,
                    }
                self.logger.warning(
                    f"TrackingStatus response did not match expected format "
                    f"(first 200 chars): {response[:200]}"
                )
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
                if elevation <= 0:
                    continue

                # Check for actual signal lock (L1 SNR) — almanac-only
                # entries have no SNR (e.g. HEDI with disconnected antenna
                # reports 12 satellites at predicted positions but 0 tracked)
                line_match = re.search(
                    rf"PRN?={prn}\s+.*?L1snr=(\d+)", response, re.IGNORECASE
                )
                l1_snr = int(line_match.group(1)) if line_match else 0
                if l1_snr == 0:
                    continue  # Satellite visible but not tracked (no signal lock)

                total_tracking += 1
                sat_info = {
                    "prn": int(prn),
                    "system": sys,
                    "elevation": elevation,
                    "azimuth": int(azm),
                    "l1_snr": l1_snr,
                }
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
                "threshold_warning": self.metric_checker.config.sat_warning,
                "threshold_critical": self.metric_checker.config.sat_critical,
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

            # If no parseable position data was found, return None
            # (avoids injecting status=unknown that drags overall to unknown)
            if "latitude" not in position_data and "fix_type" not in position_data:
                return None

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

            return position_data

        except Exception as e:
            self.logger.error(f"Error parsing position response: {e}")
            return None

    def _parse_satellites_from_pos_xml(self, xml_text: str) -> Optional[Dict[str, Any]]:
        """Parse satellite tracking data from posData.xml.

        Used as fallback when /prog/show?TrackingStatus is unavailable
        (NetR5, or NetR9 with newer ASTRA firmware).

        XML format:
            <numFixSvs>34</numFixSvs>
            <SvsUsed>
                <sv sys="0" antenna="0">24</sv>   <!-- GPS -->
                <sv sys="2" antenna="0">4</sv>    <!-- GLONASS -->
                <sv sys="3" antenna="0">12</sv>   <!-- Galileo -->
            </SvsUsed>

        Trimble sys codes: 0=GPS, 1=SBAS, 2=GLONASS, 3=Galileo, 4=BeiDou, 5=QZSS.

        Returns:
            Satellite tracking dict compatible with _fetch_and_parse_tracking output,
            or None if parsing fails.
        """
        try:
            sys_map = {
                "0": "GPS",
                "1": "SBAS",
                "2": "GLONASS",
                "3": "Galileo",
                "4": "BeiDou",
                "5": "QZSS",
            }

            # Count satellites by constellation from <sv sys="N"> elements
            sv_matches = re.findall(r'<sv\s+sys="(\d)"[^>]*>(\d+)</sv>', xml_text)

            if not sv_matches:
                # Check if XML is valid but reports 0 satellites
                num_fix_match = re.search(r"<numFixSvs>(\d+)</numFixSvs>", xml_text)
                if num_fix_match or "<SvsUsed" in xml_text:
                    total = int(num_fix_match.group(1)) if num_fix_match else 0
                    self.logger.info(f"posData.xml: {total} satellites (no sv elements)")
                    return {
                        "total": total,
                        "visible": total,
                        "status": self._satellite_status(total),
                        "by_constellation": {
                            "GPS": 0, "GLONASS": 0, "Galileo": 0,
                            "BeiDou": 0, "SBAS": 0,
                        },
                        "source": "posData.xml",
                        "threshold_warning": self.metric_checker.config.sat_warning,
                        "threshold_critical": self.metric_checker.config.sat_critical,
                    }
                return None

            gps_count = 0
            glonass_count = 0
            galileo_count = 0
            beidou_count = 0
            sbas_count = 0

            for sys_code, _prn in sv_matches:
                name = sys_map.get(sys_code, "")
                if name == "GPS":
                    gps_count += 1
                elif name == "GLONASS":
                    glonass_count += 1
                elif name == "Galileo":
                    galileo_count += 1
                elif name == "BeiDou":
                    beidou_count += 1
                elif name == "SBAS":
                    sbas_count += 1

            total = len(sv_matches)

            # Prefer <numFixSvs> if available (may differ from sv count)
            num_fix_match = re.search(r"<numFixSvs>(\d+)</numFixSvs>", xml_text)
            if num_fix_match:
                total = int(num_fix_match.group(1))

            status = self._satellite_status(total)

            self.logger.debug(
                f"Satellites from posData.xml: {total} total "
                f"(GPS={gps_count}, GLO={glonass_count}, GAL={galileo_count})"
            )

            return {
                "total": total,
                "visible": total,
                "status": status,
                "by_constellation": {
                    "GPS": gps_count,
                    "GLONASS": glonass_count,
                    "Galileo": galileo_count,
                    "BeiDou": beidou_count,
                    "SBAS": sbas_count,
                },
                "source": "posData.xml",
                "threshold_warning": self.metric_checker.config.sat_warning,
                "threshold_critical": self.metric_checker.config.sat_critical,
            }

        except Exception as e:
            self.logger.error(f"Error parsing satellites from posData.xml: {e}")
            return None

    def _parse_position_from_pos_xml(self, xml_text: str) -> Optional[Dict[str, Any]]:
        """Parse position and DOP data from posData.xml.

        Used as fallback when /prog/show?Position is unavailable
        (NetR5, or NetR9 with newer ASTRA firmware).

        XML format:
            <lat>63.454993011</lat>
            <lon>-18.307764444</lon>
            <hgt>81.099</hgt>
            <fixType>PosAutonString</fixType>   (or <posType> on NetR5)
            <DOP><PDOP>0.7</PDOP><HDOP>0.4</HDOP><VDOP>0.6</VDOP><TDOP>0.4</TDOP></DOP>

        Returns:
            Position dict compatible with _fetch_and_parse_position output,
            or None if parsing fails.
        """
        try:
            position_data: Dict[str, Any] = {}

            lat_match = re.search(r"<lat>([-\d.]+)</lat>", xml_text)
            if lat_match:
                position_data["latitude"] = float(lat_match.group(1))

            lon_match = re.search(r"<lon>([-\d.]+)</lon>", xml_text)
            if lon_match:
                position_data["longitude"] = float(lon_match.group(1))

            hgt_match = re.search(r"<hgt>([-\d.]+)</hgt>", xml_text)
            if hgt_match:
                position_data["height"] = float(hgt_match.group(1))

            # Fix type: try <fixType> first, then <posType>, then <soln>
            for tag in ["fixType", "posType", "soln"]:
                fix_match = re.search(rf"<{tag}>([^<]+)</{tag}>", xml_text)
                if fix_match:
                    position_data["fix_type"] = fix_match.group(1)
                    break

            # DOP values
            pdop_match = re.search(r"<PDOP>([\d.]+)</PDOP>", xml_text)
            if pdop_match:
                position_data["pdop"] = float(pdop_match.group(1))

            hdop_match = re.search(r"<HDOP>([\d.]+)</HDOP>", xml_text)
            if hdop_match:
                position_data["hdop"] = float(hdop_match.group(1))

            vdop_match = re.search(r"<VDOP>([\d.]+)</VDOP>", xml_text)
            if vdop_match:
                position_data["vdop"] = float(vdop_match.group(1))

            tdop_match = re.search(r"<TDOP>([\d.]+)</TDOP>", xml_text)
            if tdop_match:
                position_data["tdop"] = float(tdop_match.group(1))

            # Satellite count from position fix
            sv_matches = re.findall(r'<sv\s+sys="\d"', xml_text)
            if sv_matches:
                position_data["satellites_used"] = len(sv_matches)

            if "latitude" not in position_data and "fix_type" not in position_data:
                return None

            # Determine status from fix type
            fix_type = position_data.get("fix_type", "")
            if "3D" in fix_type or "Auton" in fix_type:
                position_data["status"] = "ok"
            elif "2D" in fix_type:
                position_data["status"] = "warning"
            elif fix_type:
                position_data["status"] = "warning"
            else:
                position_data["status"] = "unknown"

            position_data["source"] = "posData.xml"

            self.logger.debug(
                f"Position from posData.xml: "
                f"{position_data.get('latitude', '?')}, "
                f"{position_data.get('longitude', '?')}, "
                f"PDOP={position_data.get('pdop', '?')}"
            )

            return position_data

        except Exception as e:
            self.logger.error(f"Error parsing position from posData.xml: {e}")
            return None

    def _fetch_system_info(self) -> Optional[Dict[str, Any]]:
        """Fetch system information (serial number, firmware, antenna, etc).

        NetR5 only supports /prog/show?SerialNumber; firmware, antenna, and
        refstation endpoints all return "ERROR: Unknown Command".

        Returns:
            System info dictionary or None
        """
        system_info = {}
        has_prog_show = self.receiver_type != "NetR5"

        # Get serial number (works on all Trimble models including NetR5)
        serial_response = self._fetch_endpoint("serial")
        if serial_response:
            match = re.search(r"sn=(\S+)", serial_response)
            if match:
                system_info["serial_number"] = match.group(1)

        # Remaining /prog/show? endpoints only work on NetR9/NetRS
        if has_prog_show:
            # Get firmware version
            firmware_response = self._fetch_endpoint("firmware")
            if firmware_response:
                match = re.search(r"version=(\S+)", firmware_response)
                if not match:
                    # Try bare value (some receivers return just the version string)
                    stripped = firmware_response.strip()
                    if stripped and len(stripped) < 60 and not stripped.startswith("ERROR:"):
                        system_info["firmware_version"] = stripped
                else:
                    system_info["firmware_version"] = match.group(1)

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
        """Determine voltage status using centralized thresholds.

        Args:
            voltage: Voltage reading in volts

        Returns:
            Status string (ok, warning, critical)
        """
        result = self.metric_checker.check_voltage(voltage)
        return result.status.value

    def _temperature_status(self, temperature: float) -> str:
        """Determine temperature status using centralized thresholds.

        Args:
            temperature: Temperature in Celsius

        Returns:
            Status string (ok, warning, critical)
        """
        result = self.metric_checker.check_temperature(temperature)
        return result.status.value

    def _satellite_status(self, count: int) -> str:
        """Determine satellite tracking status using centralized thresholds.

        Args:
            count: Number of satellites tracking

        Returns:
            Status string (ok, warning, critical)
        """
        result = self.metric_checker.check_satellites(count)
        return result.status.value

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
        else:
            non_unknown = [s for s in statuses if s != "unknown"]
            if non_unknown and all(s == "ok" for s in non_unknown):
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
