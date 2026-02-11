"""TCP-based live health data extractor for Septentrio PolaRX5 receivers.

This module extracts real-time health data from PolaRX5 receivers via the TCP
command interface (default port 28784). It queries SBF blocks on demand using
the `esoc` (exeSBFOnce) command.

Uses the centralized MetricChecker from receivers.health.metrics for consistent
threshold evaluation across all health monitoring components.

SBF Blocks Used:
- 4101 PowerStatus: Power supply voltage
- 4014 ReceiverStatus: CPU load, temperature, uptime
- 4059 DiskStatus: Internal storage status
- 4007 PVTGeodetic2: Position (lat, lon, alt) and accuracy
- 4013 ChannelStatus: Satellite tracking per channel
- 4082 QualityInd: Quality indicators including satellite counts

Usage:
    extractor = PolaRX5TCPExtractor('10.6.1.201', 'ISFS')
    health_data = extractor.extract_health_data()
"""

import logging
import re
import socket
import struct
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .metrics import MetricChecker, load_thresholds


class PolaRX5TCPExtractor:
    """Extract live health data from PolaRX5 via TCP command interface."""

    # Default TCP command port for Septentrio receivers
    CONTROL_PORT = 28784

    # SBF block IDs
    BLOCK_POWER_STATUS = 4101
    BLOCK_RECEIVER_STATUS = 4014
    BLOCK_DISK_STATUS = 4059
    BLOCK_PVT_GEODETIC2 = 4007
    BLOCK_PVT_SAT_CARTESIAN = 4008  # Satellites used in PVT solution
    BLOCK_CHANNEL_STATUS = 4013
    BLOCK_QUALITY_IND = 4082
    BLOCK_NTRIP_SERVER_STATUS = 4122  # NTRIP server connections
    BLOCK_NTRIP_CLIENT_STATUS = 4053  # NTRIP client connection
    BLOCK_RECEIVER_SETUP = 4027  # ReceiverSetup - model, firmware, serial

    def __init__(
        self,
        host: str,
        station_id: str,
        port: int = CONTROL_PORT,
        timeout: float = 10.0,
        port_config: Optional[Dict[str, int]] = None
    ):
        """Initialize TCP health extractor.

        Args:
            host: Receiver hostname or IP address
            station_id: Station identifier for logging
            port: TCP command port (default: 28784)
            timeout: Socket timeout in seconds
            port_config: Optional dict with port names and numbers to check
                        e.g., {"ftp": 2160, "http": 8060, "control": 28784}
        """
        self.host = host
        self.station_id = station_id
        self.port = port
        self.timeout = timeout
        self.logger = logging.getLogger(f"receivers.health.tcp.{station_id}")
        self._connection_id = None  # Will be detected from prompt (e.g., "IP11")

        # Initialize centralized metric checker for consistent threshold evaluation
        # Load thresholds with receiver-type and power-type overrides
        power_type = None
        try:
            from ..config_utils import get_station_config
            cfg = get_station_config(station_id)
            if cfg:
                power_type = cfg.get("power_type") or None
        except Exception:
            pass
        config = load_thresholds(receiver_type="PolaRX5", power_type=power_type)
        self.metric_checker = MetricChecker(config)

        # Port configuration for status checks
        self.port_config = port_config or {
            "ftp": 2160,
            "http": 8060,
            "control": 28784
        }

    def extract_health_data(self) -> Dict[str, Any]:
        """Extract health data from all available SBF blocks.

        Returns:
            Dictionary with extracted health data in standardized format
        """
        health_data = {
            "extraction_time": datetime.now(timezone.utc).isoformat(),
            "extraction_method": "tcp_command",
            "metrics": {},
            "data_quality": {},
        }

        try:
            # Check port status (FTP, HTTP, control)
            port_status = self._check_port_status()
            if port_status:
                health_data["metrics"]["ports"] = port_status

            # Query PowerStatus for voltage
            power_data = self._query_power_status()
            if power_data:
                health_data["metrics"]["power"] = power_data

            # Query ReceiverStatus for CPU, temperature, uptime
            receiver_data = self._query_receiver_status()
            if receiver_data:
                if "cpu_load" in receiver_data:
                    health_data["metrics"]["cpu_load"] = receiver_data["cpu_load"]
                if "temperature" in receiver_data:
                    health_data["metrics"]["temperature"] = receiver_data["temperature"]
                if "uptime_seconds" in receiver_data:
                    health_data["metrics"]["uptime_seconds"] = receiver_data["uptime_seconds"]
                if "rx_status" in receiver_data:
                    health_data["metrics"]["rx_status"] = receiver_data["rx_status"]

            # Query DiskStatus for disk usage
            disk_data = self._query_disk_status()
            if disk_data:
                health_data["data_quality"]["disk"] = disk_data

            # Query PVTGeodetic2 for position and accuracy
            position_data = self._query_pvt_geodetic()
            if position_data:
                health_data["metrics"]["position"] = position_data

            # Query ChannelStatus for satellite tracking
            satellite_data = self._query_satellite_tracking()
            if satellite_data:
                health_data["metrics"]["satellites"] = satellite_data

            # Query NTRIPClientStatus for RTK corrections
            ntrip_client = self._query_ntrip_client_status()
            if ntrip_client:
                health_data["metrics"]["ntrip_client"] = ntrip_client

            # Query NTRIPServerStatus for NTRIP caster
            ntrip_server = self._query_ntrip_server_status()
            if ntrip_server:
                health_data["metrics"]["ntrip_server"] = ntrip_server

            # Query ReceiverSetup for identity (model, firmware, serial)
            setup_data = self._query_receiver_setup()
            if setup_data:
                health_data["receiver_identity"] = setup_data

            # Query logging sessions via ASCII command (lst, LogSession)
            logging_data = self._query_logging_sessions()
            if logging_data:
                health_data["metrics"]["logging_sessions"] = logging_data

        except Exception as e:
            self.logger.error(f"Error extracting health data: {e}")
            health_data["error"] = str(e)

        return health_data

    def _query_power_status(self) -> Optional[Dict[str, Any]]:
        """Query PowerStatus SBF block for voltage info.

        Returns:
            Dictionary with power metrics or None on failure
        """
        sbf_data = self._send_sbf_request("PowerStatus", self.BLOCK_POWER_STATUS)
        if not sbf_data:
            return None

        try:
            # Parse SBF header (block ID already verified in _send_sbf_request)
            _, length = self._parse_sbf_header(sbf_data)

            # PowerStatus structure (16 bytes total):
            # Bytes 0-7: SBF header (sync, CRC, ID+Rev, Length)
            # Bytes 8-11: TOW (Time of Week)
            # Bytes 12-13: WNc (Week Number)
            # Bytes 14-15: PowerInfo (uint16, little-endian)
            #   - Lower 4 bits: PowerSource (1=Vin, 2=Vbat, etc.)
            #   - Upper 12 bits: VinVoltage (value / 40 = volts)
            # Formula verified against Septentrio bin2asc official output
            if length >= 16 and len(sbf_data) >= 16:
                # Read bytes 14-15 as 16-bit little-endian
                power_info = struct.unpack('<H', sbf_data[14:16])[0]
                # Voltage = upper 12 bits / 40
                voltage = (power_info >> 4) / 40.0

                return {
                    "voltage": round(voltage, 2),
                    "unit": "V",
                    "status": self._check_voltage_status(voltage),
                }

        except Exception as e:
            self.logger.error(f"Error parsing PowerStatus: {e}")

        return None

    def _query_receiver_status(self) -> Optional[Dict[str, Any]]:
        """Query ReceiverStatus SBF block for CPU, temperature, uptime.

        Returns:
            Dictionary with receiver metrics or None on failure
        """
        sbf_data = self._send_sbf_request("ReceiverStatus", self.BLOCK_RECEIVER_STATUS)
        if not sbf_data:
            return None

        try:
            # Block ID already verified in _send_sbf_request
            _, length = self._parse_sbf_header(sbf_data)

            result = {}

            # ReceiverStatus structure (56 bytes typical for PolaRX5):
            # Bytes 0-7: SBF header (sync, CRC, ID+Rev, Length)
            # Bytes 8-11: TOW (Time of Week)
            # Bytes 12-13: WNc (Week Number)
            # Byte 14: CPULoad (uint8, %)
            # Byte 15: ExtError (uint8)
            # Bytes 16-19: UpTime (uint32, seconds)
            # Bytes 20-23: RxStatus (uint32)
            # Bytes 24-27: RxError (uint32)
            # Byte 28: N (number of AGCState entries)
            # Byte 29: SBLength (size of AGCState)
            # Byte 30: CmdCount (uint8)
            # Byte 31: Temperature (int8, offset by 100°C)
            if length >= 32 and len(sbf_data) >= 32:
                # CPU Load at offset 14
                cpu_load = sbf_data[14]
                result["cpu_load"] = {
                    "percent": cpu_load,
                    "status": self._check_cpu_status(cpu_load),
                }

                # UpTime at offset 16-19
                uptime = struct.unpack('<I', sbf_data[16:20])[0]
                result["uptime_seconds"] = uptime

                # RxStatus at offset 20-23 (uint32 bitfield)
                rx_status = struct.unpack('<I', sbf_data[20:24])[0]
                result["rx_status"] = rx_status

                # Temperature at offset 31 (int8, formula: temp = raw - 100)
                # Verified against RxControl display
                temp_raw = sbf_data[31]
                temperature = temp_raw - 100
                result["temperature"] = {
                    "value": temperature,
                    "unit": "C",
                    "status": self._check_temperature_status(temperature),
                }

            return result

        except Exception as e:
            self.logger.error(f"Error parsing ReceiverStatus: {e}")

        return None

    def _query_ntrip_client_status(self) -> Optional[Dict[str, Any]]:
        """Query NTRIPClientStatus SBF block for NTRIP client connection info.

        NTRIPClientStatus (SBF 4053) reports RTK correction stream status.

        Returns:
            Dictionary with NTRIP client status or None on failure
        """
        sbf_data = self._send_sbf_request(
            "NTRIPClientStatus", self.BLOCK_NTRIP_CLIENT_STATUS
        )
        if not sbf_data:
            return None

        try:
            _, length = self._parse_sbf_header(sbf_data)

            # NTRIPClientStatus structure:
            # Bytes 0-7: SBF header (sync, CRC, ID+Rev, Length)
            # Bytes 8-11: TOW (Time of Week)
            # Bytes 12-13: WNc (Week Number)
            # Byte 14: CDIndex (uint8) - connection descriptor index
            # Byte 15: Status (uint8) - 0=Idle, 1=Connected, 2=Error
            # Byte 16: ErrorCode (uint8)
            if length >= 17 and len(sbf_data) >= 17:
                cd_index = sbf_data[14]
                status_byte = sbf_data[15]
                error_code = sbf_data[16]
                # Septentrio NTRIPClientStatus status values:
                # 0=Idle, 1=Connecting, 2=Connected, 3=Error, 4=Sending
                status_map = {
                    0: "idle",
                    1: "connecting",
                    2: "connected",
                    3: "error",
                    4: "connected",  # Sending/receiving data = active connection
                }
                return {
                    "cd_index": f"NTR{cd_index + 1}",
                    "status": status_map.get(status_byte, f"unknown_{status_byte}"),
                    "error_code": error_code if status_byte == 3 else None,
                }

        except Exception as e:
            self.logger.error(f"Error parsing NTRIPClientStatus: {e}")

        return None

    def _query_ntrip_server_status(self) -> Optional[Dict[str, Any]]:
        """Query NTRIPServerStatus SBF block for NTRIP server connection info.

        NTRIPServerStatus (SBF 4122) reports NTRIP caster connection status.

        Returns:
            Dictionary with NTRIP server status or None on failure
        """
        sbf_data = self._send_sbf_request(
            "NTRIPServerStatus", self.BLOCK_NTRIP_SERVER_STATUS
        )
        if not sbf_data:
            return None

        try:
            _, length = self._parse_sbf_header(sbf_data)

            # NTRIPServerStatus structure:
            # Bytes 0-7: SBF header (sync, CRC, ID+Rev, Length)
            # Bytes 8-11: TOW (Time of Week)
            # Bytes 12-13: WNc (Week Number)
            # Byte 14: CDIndex (uint8) - connection descriptor index
            # Byte 15: Status (uint8) - 0=Idle, 1=Connected, 2=Error
            # Byte 16: ErrorCode (uint8)
            if length >= 17 and len(sbf_data) >= 17:
                cd_index = sbf_data[14]
                status_byte = sbf_data[15]
                error_code = sbf_data[16]
                # Septentrio NTRIPServerStatus status values:
                # 0=Idle, 1=Connecting, 2=Connected, 3=Error, 4=Sending
                status_map = {
                    0: "idle",
                    1: "connecting",
                    2: "connected",
                    3: "error",
                    4: "connected",  # Sending/receiving data = active connection
                }
                return {
                    "cd_index": f"NTR{cd_index + 1}",
                    "status": status_map.get(status_byte, f"unknown_{status_byte}"),
                    "error_code": error_code if status_byte == 3 else None,
                }

        except Exception as e:
            self.logger.error(f"Error parsing NTRIPServerStatus: {e}")

        return None

    def _query_receiver_setup(self) -> Optional[Dict[str, Any]]:
        """Query ReceiverSetup SBF block (4027) for receiver identity.

        Extracts model name, firmware version, and serial number from the
        ReceiverSetup block. This identifies the actual hardware connected,
        enabling mismatch detection against configured receiver type.

        ReceiverSetup structure (after 14-byte standard SBF header+TOW+WNc):
        - Bytes 14-73:   MarkerName (60 chars, null-padded)
        - Bytes 74-93:   MarkerNumber (20 chars)
        - Bytes 94-113:  Observer (20 chars)
        - Bytes 114-153: Agency (40 chars)
        - Bytes 154-173: RxSerialNumber (20 chars)
        - Bytes 174-193: RxName (20 chars) — receiver model
        - Bytes 194-213: RxVersion (20 chars) — firmware version

        Returns:
            Dictionary with receiver identity or None on failure
        """
        sbf_data = self._send_sbf_request(
            "ReceiverSetup", self.BLOCK_RECEIVER_SETUP
        )
        if not sbf_data:
            return None

        try:
            _, length = self._parse_sbf_header(sbf_data)

            # Need at least 214 bytes to extract through RxVersion
            if length < 214 or len(sbf_data) < 214:
                self.logger.debug(
                    f"ReceiverSetup response too short: {length} bytes"
                )
                return None

            def _extract_string(data: bytes, start: int, size: int) -> str:
                """Extract null-terminated string from fixed-size field."""
                raw = data[start:start + size]
                return raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()

            serial_number = _extract_string(sbf_data, 154, 20)
            receiver_model = _extract_string(sbf_data, 174, 20)
            firmware_version = _extract_string(sbf_data, 194, 20)

            if not any([serial_number, receiver_model, firmware_version]):
                return None

            identity = {}
            if receiver_model:
                identity["receiver_model"] = receiver_model
            if firmware_version:
                identity["firmware_version"] = firmware_version
            if serial_number:
                identity["serial_number"] = serial_number

            self.logger.info(
                f"Receiver identity: model={receiver_model}, "
                f"firmware={firmware_version}, serial={serial_number}"
            )
            return identity

        except Exception as e:
            self.logger.error(f"Error parsing ReceiverSetup: {e}")

        return None

    def _query_logging_sessions(self) -> Optional[Dict[str, Any]]:
        """Query active logging sessions via ASCII command interface.

        Sends 'getLogSession' to list configured sessions and their state.
        Parses the response to identify which sessions are actively logging.

        The response format contains lines like:
            LogSession, LOG1, Enabled, DSK1, "15s_24hr", After1Year, High, Continuous
            LogSession, LOG4, Disabled, DSK1, "geod_15m", After1Year, Medium, Continuous
            LogSession, LOG6, Unused, DSK1, "", Never, Medium, Continuous

        Returns:
            Logging sessions dict compatible with db_writer._write_logging_status(),
            or None if query fails or no sessions found.
        """
        response = self._send_ascii_command("getLogSession")
        if not response:
            return None

        return self._parse_log_session_response(response)

    def _parse_log_session_response(
        self, response: str
    ) -> Optional[Dict[str, Any]]:
        """Parse getLogSession response to extract active sessions.

        Response format (one line per LOG slot):
            LogSession, LOG1, Enabled, DSK1, "15s_24hr", After1Year, High, Continuous
            LogSession, LOG2, Enabled, DSK1, "1Hz_1hr", After30Days, High, Continuous
            LogSession, LOG5, Enabled, DSK1, "status_1hr", After1Year, High, Continuous
            LogSession, LOG4, Disabled, DSK1, "geod_15m", ...
            LogSession, LOG6, Unused, DSK1, "", Never, ...

        State field: Enabled/Disabled/Unused

        Known session names are mapped to canonical names used by the
        dashboard (15s_24hr, 1Hz_1hr, status_1hr).

        Args:
            response: Raw text response from receiver

        Returns:
            Logging sessions dict or None
        """
        # Map receiver session names to our canonical names
        session_map = {
            "15s_24hr": "15s_24hr",
            "1hz_1hr": "1Hz_1hr",
            "1Hz_1hr": "1Hz_1hr",
            "status_1hr": "status_1hr",
        }

        active_sessions: List[Dict[str, str]] = []

        for line in response.split("\n"):
            line = line.strip()

            # Look for LogSession lines
            if "LogSession" not in line:
                continue

            # Skip lines that are just the command echo
            if line.startswith("$R: getLogSession"):
                continue

            # Check if session is Enabled (vs Disabled/Unused)
            if "Enabled" not in line:
                continue

            # Extract session name from quoted string
            name_match = re.search(r'"([^"]+)"', line)
            if not name_match:
                continue

            session_name = name_match.group(1)
            if not session_name:
                continue

            # Map to canonical name
            canonical = session_map.get(session_name)
            if not canonical:
                canonical = session_map.get(session_name.lower())
            if not canonical:
                continue

            # Avoid duplicates
            if not any(s["session"] == canonical for s in active_sessions):
                active_sessions.append({"session": canonical})

        if not active_sessions:
            return None

        return {
            "active_sessions": len(active_sessions),
            "sessions": active_sessions,
            "status": "ok",
        }

    def _send_ascii_command(self, command: str) -> Optional[str]:
        """Send an ASCII command to the receiver and return text response.

        Opens a new TCP connection, sends the command, collects the text
        response until the prompt reappears or timeout, then closes.

        Args:
            command: ASCII command string (e.g., 'lst, LogSession')

        Returns:
            Response text or None on failure
        """
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))

            # Read initial prompt (e.g., "IP11>")
            prompt = sock.recv(1024)
            conn_id = self._parse_connection_id(prompt)
            self.logger.debug(f"ASCII command connection as {conn_id}")

            # Send command
            sock.send((command + "\n").encode("utf-8"))

            # Collect text response until prompt reappears or timeout
            response = b""
            end_time = time.time() + 3.0

            while time.time() < end_time:
                try:
                    sock.settimeout(1.0)
                    chunk = sock.recv(8192)
                    if chunk:
                        response += chunk

                        # Check if response ends with prompt (IPxx>)
                        decoded = response.decode("utf-8", errors="ignore")
                        if (
                            decoded.rstrip().endswith(">")
                            and "IP" in decoded[-20:]
                        ):
                            break
                except socket.timeout:
                    if response:
                        break

            return response.decode("utf-8", errors="ignore")

        except socket.timeout:
            self.logger.debug(f"Timeout sending ASCII command: {command}")
            return None
        except ConnectionRefusedError:
            self.logger.debug(
                f"Connection refused for ASCII command to {self.host}:{self.port}"
            )
            return None
        except Exception as e:
            self.logger.debug(f"ASCII command '{command}' failed: {e}")
            return None
        finally:
            if sock:
                sock.close()

    def _query_disk_status(self) -> Optional[Dict[str, Any]]:
        """Query DiskStatus SBF block for disk usage.

        Returns:
            Dictionary with disk metrics or None on failure
        """
        sbf_data = self._send_sbf_request("DiskStatus", self.BLOCK_DISK_STATUS)
        if not sbf_data:
            return None

        try:
            # Block ID already verified in _send_sbf_request
            _, length = self._parse_sbf_header(sbf_data)

            # DiskStatus has variable structure, extract what we can
            if length >= 28 and len(sbf_data) >= 28:
                # Try to extract disk info (structure varies by firmware)
                return {
                    "status": "ok",
                    "raw_length": length,
                }

        except Exception as e:
            self.logger.error(f"Error parsing DiskStatus: {e}")

        return None

    def _send_sbf_request(
        self, block_name: str, expected_block_id: Optional[int] = None
    ) -> Optional[bytes]:
        """Send SBF request and receive response.

        Args:
            block_name: Name of SBF block to request (e.g., "PowerStatus")
            expected_block_id: Optional block ID to search for. If provided,
                              scans the response for this specific block ID.
                              This handles receivers with continuous SBF output.

        Returns:
            Raw SBF data bytes or None on failure
        """
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))

            # Read initial prompt to detect connection ID (e.g., "IP11>")
            prompt = sock.recv(1024)
            conn_id = self._parse_connection_id(prompt)

            # Send SBF once request using detected connection ID
            # The esoc command streams SBF data to the specified connection
            cmd = f"esoc, {conn_id}, {block_name}\n"
            sock.send(cmd.encode())

            # Collect data and scan for expected block
            # Use overall timeout to avoid hanging on receivers with continuous output
            response = b""
            end_time = time.time() + 2.0  # 2 second total timeout

            while time.time() < end_time:
                try:
                    sock.settimeout(0.5)
                    chunk = sock.recv(8192)
                    if chunk:
                        response += chunk

                        # If we have a specific block to find, scan after each receive
                        if expected_block_id is not None:
                            result = self._find_sbf_block(response, expected_block_id)
                            if result is not None:
                                return result
                        else:
                            # No specific block, return first SBF found
                            sync_pos = response.find(b"$@")
                            if sync_pos >= 0:
                                return response[sync_pos:]
                except socket.timeout:
                    # On receivers without continuous output, timeout means no more data
                    if len(response) == 0:
                        continue  # Keep waiting if we haven't received anything yet
                    # If we've received data but no more coming, check what we have
                    if expected_block_id is None:
                        sync_pos = response.find(b"$@")
                        if sync_pos >= 0:
                            return response[sync_pos:]
                    break

            # Final check of accumulated data
            if expected_block_id is not None:
                result = self._find_sbf_block(response, expected_block_id)
                if result is not None:
                    return result
                self.logger.warning(
                    f"Block ID {expected_block_id} not found in response for {block_name}"
                )
            else:
                self.logger.warning(f"No SBF sync found in {block_name} response")

            return None

        except socket.timeout:
            self.logger.error(f"Timeout querying {block_name}")
            return None
        except ConnectionRefusedError:
            self.logger.error(f"Connection refused to {self.host}:{self.port}")
            return None
        except Exception as e:
            self.logger.error(f"Error querying {block_name}: {e}")
            return None
        finally:
            if sock:
                sock.close()

    def _find_sbf_block(self, data: bytes, expected_block_id: int) -> Optional[bytes]:
        """Scan data for a specific SBF block ID.

        Args:
            data: Raw bytes to scan
            expected_block_id: The SBF block ID to find

        Returns:
            SBF data starting at the found block, or None if not found
        """
        pos = 0
        while pos < len(data) - 8:
            sync_pos = data.find(b"$@", pos)
            if sync_pos < 0:
                break

            # Need at least 8 bytes for SBF header
            if sync_pos + 8 > len(data):
                break

            # Parse block ID (lower 13 bits of bytes 4-5)
            id_rev = struct.unpack("<H", data[sync_pos + 4 : sync_pos + 6])[0]
            block_id = id_rev & 0x1FFF
            length = struct.unpack("<H", data[sync_pos + 6 : sync_pos + 8])[0]

            if block_id == expected_block_id:
                return data[sync_pos:]

            # Move to next potential SBF block
            pos = sync_pos + max(length, 8)

        return None

    def _parse_connection_id(self, prompt: bytes) -> str:
        """Parse connection ID from receiver prompt.

        Args:
            prompt: Initial prompt bytes from receiver (e.g., b'IP11>')

        Returns:
            Connection identifier string (e.g., "IP11")
        """
        # Prompt format is typically "IPxx>" where xx is connection number
        try:
            prompt_str = prompt.decode('ascii', errors='ignore').strip()
            if prompt_str.startswith('IP') and prompt_str.endswith('>'):
                return prompt_str[:-1]  # Remove trailing '>'
        except Exception:
            pass
        # Default fallback
        return "IP11"

    def _parse_sbf_header(self, sbf_data: bytes) -> Tuple[int, int]:
        """Parse SBF message header.

        Args:
            sbf_data: Raw SBF data starting with sync bytes

        Returns:
            Tuple of (message_id, length)
        """
        # SBF header structure:
        # Bytes 0-1: Sync ($@)
        # Bytes 2-3: CRC
        # Bytes 4-5: ID + Revision (lower 13 bits = ID)
        # Bytes 6-7: Length
        id_rev = struct.unpack('<H', sbf_data[4:6])[0]
        message_id = id_rev & 0x1FFF
        length = struct.unpack('<H', sbf_data[6:8])[0]
        return message_id, length

    def _check_voltage_status(self, voltage: float) -> str:
        """Check voltage status against centralized thresholds."""
        result = self.metric_checker.check_voltage(voltage)
        return result.status.value

    def _check_cpu_status(self, cpu_load: int) -> str:
        """Check CPU load status against centralized thresholds."""
        result = self.metric_checker.check_cpu_load(cpu_load)
        return result.status.value

    def _check_temperature_status(self, temperature: float) -> str:
        """Check temperature status against centralized thresholds."""
        result = self.metric_checker.check_temperature(temperature)
        return result.status.value

    def test_connection(self) -> bool:
        """Test if TCP connection to receiver works.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))
            sock.close()
            return True
        except Exception as e:
            self.logger.debug(f"Connection test failed: {e}")
            return False

    def _check_port_status(self) -> Dict[str, Any]:
        """Check status of configured ports (FTP, HTTP, control).

        Uses 5s timeout and retries once on timeout to reduce false negatives
        on slow 3G/4G links.  'refused' is definitive and never retried.

        Returns:
            Dictionary with port status for each configured port.
            Status values: 'open', 'timeout', 'refused', 'error'
        """
        port_status = {}
        all_ok = True

        for name, port_num in self.port_config.items():
            result_entry = self._check_single_port(name, port_num)
            # Retry once on timeout (not refused — that's definitive)
            if result_entry["detail"] == "timeout":
                self.logger.debug(
                    f"Port {name}:{port_num} timed out, retrying once..."
                )
                result_entry = self._check_single_port(name, port_num)
            port_status[name] = result_entry
            if not result_entry["open"]:
                all_ok = False

        port_status["overall_status"] = "ok" if all_ok else "warning"
        return port_status

    def _check_single_port(
        self, _name: str, port_num: int
    ) -> Dict[str, Any]:
        """Check a single TCP port with 5s timeout.

        Returns:
            Dictionary with port, open, status, detail keys.
        """
        import errno

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)  # 5s for slow 3G/4G links
            result = sock.connect_ex((self.host, port_num))
            sock.close()

            is_open = result == 0
            if is_open:
                return {"port": port_num, "open": True, "status": "ok", "detail": "open"}
            elif result == errno.ECONNREFUSED:
                return {"port": port_num, "open": False, "status": "warning", "detail": "refused"}
            else:
                return {"port": port_num, "open": False, "status": "critical", "detail": "timeout"}
        except socket.timeout:
            return {"port": port_num, "open": False, "status": "critical", "detail": "timeout"}
        except ConnectionRefusedError:
            return {"port": port_num, "open": False, "status": "warning", "detail": "refused"}
        except Exception as e:
            return {"port": port_num, "open": False, "status": "error", "detail": str(e)}

    def _query_pvt_geodetic(self) -> Optional[Dict[str, Any]]:
        """Query PVTGeodetic2 SBF block for position and accuracy.

        Returns:
            Dictionary with position data or None on failure
        """
        # Note: Use "PVTGeodetic" command name (receiver responds to this, not "PVTGeodetic2")
        sbf_data = self._send_sbf_request("PVTGeodetic", self.BLOCK_PVT_GEODETIC2)
        if not sbf_data:
            return None

        try:
            # Block ID already verified in _send_sbf_request
            _, length = self._parse_sbf_header(sbf_data)

            # PVTGeodetic2 structure:
            # Bytes 0-7: SBF header
            # Bytes 8-11: TOW (Time of Week)
            # Bytes 12-13: WNc (Week Number)
            # Byte 14: Mode (uint8)
            # Byte 15: Error (uint8)
            # Bytes 16-23: Latitude (float64, radians)
            # Bytes 24-31: Longitude (float64, radians)
            # Bytes 32-39: Height (float64, meters above ellipsoid)
            # Bytes 40-43: Undulation (float32)
            # Bytes 44-47: Vn (float32, north velocity)
            # Bytes 48-51: Ve (float32, east velocity)
            # Bytes 52-55: Vu (float32, up velocity)
            # Bytes 56-59: COG (float32, course over ground)
            # Bytes 60-67: RxClkBias (float64)
            # Bytes 68-71: RxClkDrift (float32)
            # Byte 72: TimeSystem (uint8)
            # Byte 73: Datum (uint8)
            # Byte 74: NrSV (uint8, number of satellites used)
            # Byte 75: WACorrInfo (uint8)
            # Bytes 76-77: ReferenceID (uint16)
            # Bytes 78-79: MeanCorrAge (uint16)
            # Bytes 80-83: SignalInfo (uint32)
            # Byte 84: AlertFlag (uint8)
            # Byte 85: NrBases (uint8)
            # Bytes 86-87: PPPInfo (uint16)
            # Bytes 88-89: Latency (uint16)
            # Bytes 90-91: HAccuracy (uint16, mm)
            # Bytes 92-93: VAccuracy (uint16, mm)

            import math

            if length >= 94 and len(sbf_data) >= 94:
                # Extract position
                lat_rad = struct.unpack('<d', sbf_data[16:24])[0]
                lon_rad = struct.unpack('<d', sbf_data[24:32])[0]
                height = struct.unpack('<d', sbf_data[32:40])[0]

                # Convert radians to degrees
                lat_deg = math.degrees(lat_rad)
                lon_deg = math.degrees(lon_rad)

                # Check for Do-Not-Use values (NaN or very large)
                if math.isnan(lat_deg) or abs(lat_deg) > 90:
                    return None
                if math.isnan(lon_deg) or abs(lon_deg) > 180:
                    return None

                # Extract accuracy (in mm, convert to m)
                h_accuracy_mm = struct.unpack('<H', sbf_data[90:92])[0]
                v_accuracy_mm = struct.unpack('<H', sbf_data[92:94])[0]

                # Number of satellites used
                nr_sv = sbf_data[74]

                # Fix mode
                mode = sbf_data[14]
                mode_names = {
                    0: "no_fix",
                    1: "standalone",
                    2: "dgps",
                    3: "fixed",  # RTK fixed
                    4: "float",  # RTK float
                    5: "sbas",
                    6: "ppp"
                }

                return {
                    "latitude": round(lat_deg, 8),
                    "longitude": round(lon_deg, 8),
                    "height": round(height, 3),
                    "h_accuracy_m": round(h_accuracy_mm / 1000.0, 3) if h_accuracy_mm < 65535 else None,
                    "v_accuracy_m": round(v_accuracy_mm / 1000.0, 3) if v_accuracy_mm < 65535 else None,
                    "satellites_used": nr_sv,
                    "fix_mode": mode_names.get(mode, f"unknown_{mode}"),
                    "status": "ok" if mode >= 1 else "warning"
                }

        except Exception as e:
            self.logger.error(f"Error parsing PVTGeodetic2: {e}")

        return None

    def _query_satellite_tracking(self) -> Optional[Dict[str, Any]]:
        """Query PVTSatCartesian SBF block for satellites used in position solution.

        Uses PVTSatCartesian (4008) instead of ChannelStatus (4013) because
        ChannelStatus reports ALL allocated channels including disabled constellations,
        while PVTSatCartesian reports only satellites actually used in the PVT solution.
        This matches what the receiver's web interface displays.

        Returns:
            Dictionary with satellite counts per constellation or None on failure
        """
        sbf_data = self._send_sbf_request("PVTSatCartesian", self.BLOCK_PVT_SAT_CARTESIAN)
        if not sbf_data:
            return None

        try:
            # Block ID already verified in _send_sbf_request
            _, length = self._parse_sbf_header(sbf_data)

            # PVTSatCartesian structure:
            # Bytes 0-7: SBF header
            # Bytes 8-11: TOW
            # Bytes 12-13: WNc
            # Byte 14: N (number of satellites used in PVT solution)
            # Byte 15: SBLength (size of each SatInfo sub-block)
            # Followed by N SatInfo sub-blocks

            if length < 16 or len(sbf_data) < 16:
                return None

            n_satellites = sbf_data[14]
            sb_length = sbf_data[15]

            if n_satellites == 0:
                return {"total": 0, "by_constellation": {}, "status": "warning"}

            # Count satellites by constellation
            tracking_counts: Dict[str, int] = {}

            offset = 16  # Start of first SatInfo

            for _ in range(n_satellites):
                if offset + sb_length > len(sbf_data):
                    break

                # SatInfo structure:
                # Byte 0: SVID (satellite vehicle ID)
                svid = sbf_data[offset]

                # Determine constellation from SVID ranges
                const_name = self._svid_to_constellation(svid)

                if const_name is not None:
                    if const_name not in tracking_counts:
                        tracking_counts[const_name] = 0
                    tracking_counts[const_name] += 1

                offset += sb_length

            return {
                "total": n_satellites,
                "by_constellation": tracking_counts,
                "status": "ok" if n_satellites >= 4 else "warning"
            }

        except Exception as e:
            self.logger.error(f"Error parsing PVTSatCartesian: {e}")

        return None

    @staticmethod
    def _svid_to_constellation(svid: int) -> Optional[str]:
        """Convert Septentrio SVID to constellation name.

        SVID ranges from Septentrio SBF Reference Guide (v3.6+):
        - GPS: 1-37 (PRN 1-32 + reserved)
        - GLONASS: 38-62 (slot 1-24 + reserved)
        - Galileo: 63-106 (E01-E36, some firmware uses 63-68 for E33-E36)
        - SBAS: 120-158
        - BeiDou: 141-180 (legacy) or 201-263 (newer firmware C01-C63)
        - QZSS: 181-202
        - IRNSS/NavIC: 191-197
        - Invalid: 0, 255

        Args:
            svid: Satellite Vehicle ID from ChannelStatus

        Returns:
            Constellation name string, or None for invalid SVIDs
        """
        # Invalid SVIDs
        if svid == 0 or svid == 255:
            return None
        elif 1 <= svid <= 37:
            return "GPS"
        elif 38 <= svid <= 62:
            return "GLONASS"
        elif 63 <= svid <= 106:
            return "Galileo"
        elif 120 <= svid <= 158:
            return "SBAS"
        elif 141 <= svid <= 180:
            return "BeiDou"
        elif 181 <= svid <= 202:
            return "QZSS"
        elif 191 <= svid <= 197:
            return "IRNSS"
        elif 201 <= svid <= 263:
            return "BeiDou"
        else:
            return f"Unknown_{svid}"
