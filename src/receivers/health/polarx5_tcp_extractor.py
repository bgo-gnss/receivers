"""TCP-based live health data extractor for Septentrio PolaRX5 receivers.

This module extracts real-time health data from PolaRX5 receivers via the TCP
command interface (default port 28784). It queries SBF blocks on demand using
the `esoc` (exeSBFOnce) command.

SBF Blocks Used:
- 4101 PowerStatus: Power supply voltage
- 4014 ReceiverStatus: CPU load, temperature, uptime
- 4059 DiskStatus: Internal storage status

Usage:
    extractor = PolaRX5TCPExtractor('10.6.1.201', 'ISFS')
    health_data = extractor.extract_health_data()
"""

import logging
import socket
import struct
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple


class PolaRX5TCPExtractor:
    """Extract live health data from PolaRX5 via TCP command interface."""

    # Default TCP command port for Septentrio receivers
    CONTROL_PORT = 28784

    # SBF block IDs
    BLOCK_POWER_STATUS = 4101
    BLOCK_RECEIVER_STATUS = 4014
    BLOCK_DISK_STATUS = 4059

    def __init__(
        self,
        host: str,
        station_id: str,
        port: int = CONTROL_PORT,
        timeout: float = 10.0
    ):
        """Initialize TCP health extractor.

        Args:
            host: Receiver hostname or IP address
            station_id: Station identifier for logging
            port: TCP command port (default: 28784)
            timeout: Socket timeout in seconds
        """
        self.host = host
        self.station_id = station_id
        self.port = port
        self.timeout = timeout
        self.logger = logging.getLogger(f"receivers.health.tcp.{station_id}")

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
            # Bytes 0-7: SBF header
            # Bytes 8-11: TOW
            # Bytes 12-13: WNc
            # Bytes 14-15: PowerSource + VinVoltage
            if length >= 16 and len(sbf_data) >= 16:
                # Byte 15: Voltage in scaled format
                vin_raw = sbf_data[15]
                # Formula: voltage = (raw + 100) / 10
                voltage = (vin_raw + 100) / 10.0

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

            # ReceiverStatus structure (56 bytes typical):
            # Bytes 14: CPULoad (uint8, %)
            # Bytes 16-19: UpTime (uint32, seconds)
            # Bytes 20-21: Temperature (int16, 0.01°C scale)
            if length >= 22 and len(sbf_data) >= 22:
                # CPU Load at offset 14
                cpu_load = sbf_data[14]
                result["cpu_load"] = {
                    "percent": cpu_load,
                    "status": self._check_cpu_status(cpu_load),
                }

                # UpTime at offset 16-19
                uptime = struct.unpack('<I', sbf_data[16:20])[0]
                result["uptime_seconds"] = uptime

                # Temperature at offset 20-21 (signed, 0.01°C)
                temp_raw = struct.unpack('<h', sbf_data[20:22])[0]
                temperature = temp_raw / 100.0
                result["temperature"] = {
                    "value": round(temperature, 1),
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

            # Read initial prompt
            sock.recv(1024)

            # Send SBF once request
            # IP10 is the default connection identifier for TCP port 10
            cmd = f"esoc, IP10, {block_name}\n"
            sock.send(cmd.encode())

            # Wait for response
            time.sleep(0.3)
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
