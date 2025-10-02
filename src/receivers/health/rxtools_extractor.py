"""RxTools-based health data extractor for Septentrio PolaRX5 receivers.

This module uses Septentrio's RxTools bin2asc command to extract health data
from SBF (Septentrio Binary Format) files downloaded in status_1hr sessions.

Health Messages Extracted (SBF blocks):
- 4101 PowerStatus: Power supply information (voltage, power source, battery)
- 4059 DiskStatus: Internal storage status (free space, usage %)
- 4014 ReceiverStatus: Overall receiver status (CPU load, uptime, error codes)
- 4054 WiFiAPStatus: WiFi access point status (connected clients, signal)
- 4102 LogStatus: Logging session status (active sessions, errors)
- 4122 NTRIPServerStatus: NTRIP server status (client connections)
- 4053 NTRIPClientStatus: NTRIP client status (connection, corrections age)
- 4027 ReceiverSetup: Receiver configuration and firmware version
"""

import logging
import subprocess
import shutil
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
import re


class RxToolsNotFoundError(Exception):
    """Raised when RxTools bin2asc is not found in PATH."""
    pass


class RxToolsExtractor:
    """Extract health data from SBF files using RxTools bin2asc."""

    # SBF block IDs for health messages
    HEALTH_BLOCKS = {
        "4101": "PowerStatus",
        "4059": "DiskStatus",
        "4014": "ReceiverStatus",
        "4054": "WiFiAPStatus",
        "4102": "LogStatus",
        "4122": "NTRIPServerStatus",
        "4053": "NTRIPClientStatus",
        "4027": "ReceiverSetup",
    }

    def __init__(self, station_id: str = "UNKNOWN"):
        """Initialize RxTools extractor.

        Args:
            station_id: Station identifier for logging
        """
        self.station_id = station_id
        self.logger = logging.getLogger(f"receivers.health.rxtools.{station_id}")
        self._bin2asc_path = None

    def check_rxtools_available(self) -> bool:
        """Check if RxTools bin2asc is available.

        Returns:
            True if bin2asc is found, False otherwise
        """
        if self._bin2asc_path is None:
            self._bin2asc_path = shutil.which("bin2asc")

        return self._bin2asc_path is not None

    def extract_health_from_sbf(
        self, sbf_file: Path, output_dir: Optional[Path] = None
    ) -> Dict[str, Any]:
        """Extract health data from SBF file.

        Args:
            sbf_file: Path to SBF file (can be .sbf or .sbf.gz)
            output_dir: Optional directory for ASCII output (temp if not specified)

        Returns:
            Dictionary with extracted health data

        Raises:
            RxToolsNotFoundError: If bin2asc is not available
            FileNotFoundError: If SBF file doesn't exist
        """
        if not self.check_rxtools_available():
            raise RxToolsNotFoundError(
                "RxTools bin2asc not found in PATH. "
                "Please install RxTools from Septentrio: "
                "https://www.septentrio.com/en/products/gps-gnss-receiver-software/rxtools"
            )

        if not sbf_file.exists():
            raise FileNotFoundError(f"SBF file not found: {sbf_file}")

        self.logger.info(f"Extracting health data from {sbf_file}")

        # Create output directory for ASCII files
        if output_dir is None:
            output_dir = sbf_file.parent / "ascii"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Convert SBF to ASCII using bin2asc
        ascii_files = self._convert_sbf_to_ascii(sbf_file, output_dir)

        # Parse ASCII files for health data
        health_data = self._parse_ascii_health_data(ascii_files)

        return health_data

    def _convert_sbf_to_ascii(
        self, sbf_file: Path, output_dir: Path
    ) -> Dict[str, Path]:
        """Convert SBF file to ASCII format using bin2asc.

        Args:
            sbf_file: Path to SBF file
            output_dir: Directory for ASCII output files

        Returns:
            Dictionary mapping block IDs to ASCII file paths
        """
        ascii_files = {}

        # Convert each health block type separately for easier parsing
        for block_id, block_name in self.HEALTH_BLOCKS.items():
            output_file = output_dir / f"{sbf_file.stem}_{block_name}.txt"

            try:
                # bin2asc command: extract specific block type
                # -f <input> -s -o <output> -b <block_id>
                cmd = [
                    self._bin2asc_path,
                    "-f",
                    str(sbf_file),
                    "-s",  # Skip non-matching blocks
                    "-b",
                    block_id,  # Block ID to extract
                    "-o",
                    str(output_file),
                ]

                self.logger.debug(f"Running: {' '.join(cmd)}")

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )

                if result.returncode == 0 and output_file.exists():
                    ascii_files[block_id] = output_file
                    self.logger.debug(
                        f"Extracted {block_name} (block {block_id}) to {output_file}"
                    )
                else:
                    self.logger.warning(
                        f"Failed to extract {block_name} (block {block_id}): "
                        f"{result.stderr}"
                    )

            except subprocess.TimeoutExpired:
                self.logger.error(f"bin2asc timeout for block {block_id}")
            except Exception as e:
                self.logger.error(f"Error extracting block {block_id}: {e}")

        return ascii_files

    def _parse_ascii_health_data(self, ascii_files: Dict[str, Path]) -> Dict[str, Any]:
        """Parse ASCII files to extract health metrics.

        Args:
            ascii_files: Dictionary mapping block IDs to ASCII file paths

        Returns:
            Dictionary with structured health data
        """
        health_data = {
            "extraction_time": datetime.utcnow().isoformat() + "Z",
            "metrics": {},
            "data_quality": {},
            "network": {},
            "receiver_specific": {},
        }

        # Parse each block type
        for block_id, ascii_file in ascii_files.items():
            block_name = self.HEALTH_BLOCKS[block_id]

            try:
                if block_id == "4101":  # PowerStatus
                    health_data["metrics"].update(
                        self._parse_power_status(ascii_file)
                    )
                elif block_id == "4059":  # DiskStatus
                    health_data["data_quality"].update(
                        self._parse_disk_status(ascii_file)
                    )
                elif block_id == "4014":  # ReceiverStatus
                    health_data["metrics"].update(
                        self._parse_receiver_status(ascii_file)
                    )
                elif block_id == "4054":  # WiFiAPStatus
                    health_data["network"].update(
                        self._parse_wifi_status(ascii_file)
                    )
                elif block_id == "4102":  # LogStatus
                    health_data["data_quality"].update(
                        self._parse_log_status(ascii_file)
                    )
                elif block_id == "4122":  # NTRIPServerStatus
                    health_data["network"].update(
                        self._parse_ntrip_server_status(ascii_file)
                    )
                elif block_id == "4053":  # NTRIPClientStatus
                    health_data["network"].update(
                        self._parse_ntrip_client_status(ascii_file)
                    )
                elif block_id == "4027":  # ReceiverSetup
                    health_data["receiver_specific"].update(
                        self._parse_receiver_setup(ascii_file)
                    )

            except Exception as e:
                self.logger.error(f"Error parsing {block_name}: {e}")

        return health_data

    def _parse_power_status(self, ascii_file: Path) -> Dict[str, Any]:
        """Parse PowerStatus block (4101).

        Returns:
            Dictionary with power metrics (voltage, source, battery status)
        """
        power_data = {}

        try:
            content = ascii_file.read_text()

            # Extract voltage (example: "ExtSupply: 12.3 V")
            voltage_match = re.search(r"ExtSupply:\s*([\d.]+)\s*V", content)
            if voltage_match:
                voltage = float(voltage_match.group(1))
                power_data["power"] = {
                    "voltage": voltage,
                    "unit": "V",
                    "status": self._check_voltage_status(voltage),
                }

            # Extract power source
            source_match = re.search(r"PowerSource:\s*(\w+)", content)
            if source_match:
                power_data["power_source"] = source_match.group(1)

        except Exception as e:
            self.logger.error(f"Error parsing PowerStatus: {e}")

        return power_data

    def _parse_disk_status(self, ascii_file: Path) -> Dict[str, Any]:
        """Parse DiskStatus block (4059).

        Returns:
            Dictionary with disk metrics (free space, usage %)
        """
        disk_data = {}

        try:
            content = ascii_file.read_text()

            # Extract disk usage (example: "DiskUsage: 23456/102400 MB")
            usage_match = re.search(r"DiskUsage:\s*([\d]+)/([\d]+)\s*MB", content)
            if usage_match:
                used_mb = int(usage_match.group(1))
                total_mb = int(usage_match.group(2))
                usage_pct = (used_mb / total_mb * 100) if total_mb > 0 else 0

                disk_data["disk_usage"] = {
                    "used_mb": used_mb,
                    "total_mb": total_mb,
                    "usage_percent": round(usage_pct, 1),
                    "status": self._check_disk_status(usage_pct),
                }

        except Exception as e:
            self.logger.error(f"Error parsing DiskStatus: {e}")

        return disk_data

    def _parse_receiver_status(self, ascii_file: Path) -> Dict[str, Any]:
        """Parse ReceiverStatus block (4014).

        Returns:
            Dictionary with receiver metrics (CPU, uptime, errors)
        """
        receiver_data = {}

        try:
            content = ascii_file.read_text()

            # Extract CPU load (example: "CPULoad: 25%")
            cpu_match = re.search(r"CPULoad:\s*([\d]+)%", content)
            if cpu_match:
                cpu_load = int(cpu_match.group(1))
                receiver_data["cpu_load"] = {
                    "percent": cpu_load,
                    "status": self._check_cpu_status(cpu_load),
                }

            # Extract temperature (example: "Temperature: 45.2 C")
            temp_match = re.search(r"Temperature:\s*([\d.]+)\s*C", content)
            if temp_match:
                temperature = float(temp_match.group(1))
                receiver_data["temperature"] = {
                    "value": temperature,
                    "unit": "C",
                    "status": self._check_temperature_status(temperature),
                }

            # Extract uptime (example: "UpTime: 123456 s")
            uptime_match = re.search(r"UpTime:\s*([\d]+)\s*s", content)
            if uptime_match:
                receiver_data["uptime_seconds"] = int(uptime_match.group(1))

        except Exception as e:
            self.logger.error(f"Error parsing ReceiverStatus: {e}")

        return receiver_data

    def _parse_wifi_status(self, ascii_file: Path) -> Dict[str, Any]:
        """Parse WiFiAPStatus block (4054).

        Returns:
            Dictionary with WiFi metrics (clients, signal)
        """
        wifi_data = {}

        try:
            content = ascii_file.read_text()

            # Extract connected clients
            clients_match = re.search(r"ConnectedClients:\s*([\d]+)", content)
            if clients_match:
                wifi_data["wifi"] = {
                    "connected_clients": int(clients_match.group(1)),
                    "status": "ok",
                }

        except Exception as e:
            self.logger.error(f"Error parsing WiFiAPStatus: {e}")

        return wifi_data

    def _parse_log_status(self, ascii_file: Path) -> Dict[str, Any]:
        """Parse LogStatus block (4102).

        Returns:
            Dictionary with logging metrics (active sessions, errors)
        """
        log_data = {}

        try:
            content = ascii_file.read_text()

            # Extract active logging sessions
            sessions_match = re.search(r"ActiveSessions:\s*([\d]+)", content)
            if sessions_match:
                log_data["logging"] = {
                    "active_sessions": int(sessions_match.group(1)),
                    "status": "ok",
                }

        except Exception as e:
            self.logger.error(f"Error parsing LogStatus: {e}")

        return log_data

    def _parse_ntrip_server_status(self, ascii_file: Path) -> Dict[str, Any]:
        """Parse NTRIPServerStatus block (4122).

        Returns:
            Dictionary with NTRIP server metrics
        """
        ntrip_data = {}

        try:
            content = ascii_file.read_text()

            # Extract client connections
            clients_match = re.search(r"Clients:\s*([\d]+)", content)
            if clients_match:
                ntrip_data["ntrip_server"] = {
                    "clients": int(clients_match.group(1)),
                    "status": "ok",
                }

        except Exception as e:
            self.logger.error(f"Error parsing NTRIPServerStatus: {e}")

        return ntrip_data

    def _parse_ntrip_client_status(self, ascii_file: Path) -> Dict[str, Any]:
        """Parse NTRIPClientStatus block (4053).

        Returns:
            Dictionary with NTRIP client metrics
        """
        ntrip_data = {}

        try:
            content = ascii_file.read_text()

            # Extract connection status
            connected_match = re.search(r"Connected:\s*(\w+)", content)
            if connected_match:
                ntrip_data["ntrip_client"] = {
                    "connected": connected_match.group(1).lower() == "yes",
                    "status": "ok"
                    if connected_match.group(1).lower() == "yes"
                    else "warning",
                }

        except Exception as e:
            self.logger.error(f"Error parsing NTRIPClientStatus: {e}")

        return ntrip_data

    def _parse_receiver_setup(self, ascii_file: Path) -> Dict[str, Any]:
        """Parse ReceiverSetup block (4027).

        Returns:
            Dictionary with receiver configuration (firmware version, etc.)
        """
        setup_data = {}

        try:
            content = ascii_file.read_text()

            # Extract firmware version
            fw_match = re.search(r"FirmwareVersion:\s*([\d.]+)", content)
            if fw_match:
                setup_data["firmware_version"] = fw_match.group(1)

            # Extract receiver type
            type_match = re.search(r"ReceiverType:\s*([^\n]+)", content)
            if type_match:
                setup_data["receiver_model"] = type_match.group(1).strip()

        except Exception as e:
            self.logger.error(f"Error parsing ReceiverSetup: {e}")

        return setup_data

    # Status check helper methods

    @staticmethod
    def _check_voltage_status(voltage: float) -> str:
        """Check voltage status."""
        if voltage < 11.0:
            return "critical"
        elif voltage < 11.5:
            return "warning"
        return "ok"

    @staticmethod
    def _check_disk_status(usage_pct: float) -> str:
        """Check disk usage status."""
        if usage_pct > 90:
            return "critical"
        elif usage_pct > 80:
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
        if temperature > 70:
            return "critical"
        elif temperature > 60:
            return "warning"
        return "ok"
