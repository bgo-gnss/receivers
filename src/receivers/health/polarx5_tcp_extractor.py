"""TCP-based live health data extractor for Septentrio PolaRX5 receivers.

This module extracts real-time health data from PolaRX5 receivers via the TCP
command interface (default port 28784). It queries SBF blocks on demand using
the `esoc` (exeSBFOnce) command.

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
import socket
import struct
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple


class PolaRX5TCPExtractor:
    """Extract live health data from PolaRX5 via TCP command interface."""

    # Default TCP command port for Septentrio receivers
    CONTROL_PORT = 28784

    # SBF block IDs
    BLOCK_POWER_STATUS = 4101
    BLOCK_RECEIVER_STATUS = 4014
    BLOCK_DISK_STATUS = 4059
    BLOCK_PVT_GEODETIC2 = 4007
    BLOCK_CHANNEL_STATUS = 4013
    BLOCK_QUALITY_IND = 4082

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

        except Exception as e:
            self.logger.error(f"Error extracting health data: {e}")
            health_data["error"] = str(e)

        return health_data

    def _query_power_status(self) -> Optional[Dict[str, Any]]:
        """Query PowerStatus SBF block for voltage info.

        Returns:
            Dictionary with power metrics or None on failure
        """
        sbf_data = self._send_sbf_request("PowerStatus")
        if not sbf_data:
            return None

        try:
            # Parse SBF header
            msg_id, length = self._parse_sbf_header(sbf_data)
            if msg_id != self.BLOCK_POWER_STATUS:
                self.logger.warning(f"Unexpected block ID {msg_id}, expected {self.BLOCK_POWER_STATUS}")
                return None

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
        sbf_data = self._send_sbf_request("ReceiverStatus")
        if not sbf_data:
            return None

        try:
            msg_id, length = self._parse_sbf_header(sbf_data)
            if msg_id != self.BLOCK_RECEIVER_STATUS:
                self.logger.warning(f"Unexpected block ID {msg_id}, expected {self.BLOCK_RECEIVER_STATUS}")
                return None

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

    def _query_disk_status(self) -> Optional[Dict[str, Any]]:
        """Query DiskStatus SBF block for disk usage.

        Returns:
            Dictionary with disk metrics or None on failure
        """
        sbf_data = self._send_sbf_request("DiskStatus")
        if not sbf_data:
            return None

        try:
            msg_id, length = self._parse_sbf_header(sbf_data)
            if msg_id != self.BLOCK_DISK_STATUS:
                self.logger.warning(f"Unexpected block ID {msg_id}, expected {self.BLOCK_DISK_STATUS}")
                return None

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

    def _send_sbf_request(self, block_name: str) -> Optional[bytes]:
        """Send SBF request and receive response.

        Args:
            block_name: Name of SBF block to request (e.g., "PowerStatus")

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

            # Wait for response
            time.sleep(0.5)
            response = sock.recv(4096)

            # Find SBF sync pattern ($@)
            sync_pos = response.find(b'$@')
            if sync_pos >= 0:
                return response[sync_pos:]
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

    @staticmethod
    def _check_voltage_status(voltage: float) -> str:
        """Check voltage status against thresholds."""
        if voltage < 11.0 or voltage > 16.0:
            return "critical"
        elif voltage < 11.8 or voltage > 15.0:
            return "warning"
        return "ok"

    @staticmethod
    def _check_cpu_status(cpu_load: int) -> str:
        """Check CPU load status."""
        if cpu_load > 90:
            return "critical"
        elif cpu_load > 75:
            return "warning"
        return "ok"

    @staticmethod
    def _check_temperature_status(temperature: float) -> str:
        """Check temperature status."""
        if temperature > 70 or temperature < -20:
            return "critical"
        elif temperature > 60 or temperature < -10:
            return "warning"
        return "ok"

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

        Returns:
            Dictionary with port status for each configured port
        """
        port_status = {}
        all_ok = True

        for name, port_num in self.port_config.items():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)  # Quick timeout for port checks
                result = sock.connect_ex((self.host, port_num))
                sock.close()

                is_open = result == 0
                port_status[name] = {
                    "port": port_num,
                    "open": is_open,
                    "status": "ok" if is_open else "error"
                }
                if not is_open:
                    all_ok = False
            except Exception as e:
                port_status[name] = {
                    "port": port_num,
                    "open": False,
                    "status": "error",
                    "error": str(e)
                }
                all_ok = False

        port_status["overall_status"] = "ok" if all_ok else "warning"
        return port_status

    def _query_pvt_geodetic(self) -> Optional[Dict[str, Any]]:
        """Query PVTGeodetic2 SBF block for position and accuracy.

        Returns:
            Dictionary with position data or None on failure
        """
        # Note: Use "PVTGeodetic" command name (receiver responds to this, not "PVTGeodetic2")
        sbf_data = self._send_sbf_request("PVTGeodetic")
        if not sbf_data:
            return None

        try:
            msg_id, length = self._parse_sbf_header(sbf_data)
            if msg_id != self.BLOCK_PVT_GEODETIC2:
                self.logger.warning(f"Unexpected block ID {msg_id}, expected {self.BLOCK_PVT_GEODETIC2}")
                return None

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
        """Query ChannelStatus SBF block for satellite tracking info.

        Returns:
            Dictionary with satellite counts per constellation or None on failure
        """
        sbf_data = self._send_sbf_request("ChannelStatus")
        if not sbf_data:
            return None

        try:
            msg_id, length = self._parse_sbf_header(sbf_data)
            if msg_id != self.BLOCK_CHANNEL_STATUS:
                self.logger.warning(f"Unexpected block ID {msg_id}, expected {self.BLOCK_CHANNEL_STATUS}")
                return None

            # ChannelStatus structure:
            # Bytes 0-7: SBF header
            # Bytes 8-11: TOW
            # Bytes 12-13: WNc
            # Byte 14: N (number of channels)
            # Byte 15: SBLength (size of each ChannelStateInfo)
            # Followed by N ChannelStateInfo sub-blocks

            if length < 16 or len(sbf_data) < 16:
                return None

            n_channels = sbf_data[14]
            sb_length = sbf_data[15]

            if sb_length == 0 or n_channels == 0:
                return {"total": 0, "by_constellation": {}}

            # Count satellites by constellation (using SVID ranges from Septentrio SBF Reference)
            tracking_counts: Dict[str, int] = {}
            total_tracked = 0

            offset = 16  # Start of first ChannelStateInfo

            for _ in range(n_channels):
                if offset + sb_length > len(sbf_data):
                    break

                # ChannelStateInfo structure:
                # Byte 0: SVID (satellite vehicle ID)
                svid = sbf_data[offset]
                if svid == 0:  # No satellite on this channel
                    offset += sb_length
                    continue

                # Determine constellation from SVID ranges (Septentrio convention)
                const_name = self._svid_to_constellation(svid)

                # Skip invalid SVIDs (returns None)
                if const_name is None:
                    offset += sb_length
                    continue

                if const_name not in tracking_counts:
                    tracking_counts[const_name] = 0
                tracking_counts[const_name] += 1
                total_tracked += 1

                offset += sb_length

            return {
                "total": total_tracked,
                "by_constellation": tracking_counts,
                "status": "ok" if total_tracked >= 4 else "warning"
            }

        except Exception as e:
            self.logger.error(f"Error parsing ChannelStatus: {e}")

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
