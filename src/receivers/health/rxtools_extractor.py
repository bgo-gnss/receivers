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
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


class RxToolsNotFoundError(Exception):
    """Raised when RxTools bin2asc is not found in PATH."""
    pass


class RxToolsExtractor:
    """Extract health data from SBF files using RxTools bin2asc."""

    # SBF block names for health messages (used with bin2asc -m flag)
    HEALTH_BLOCKS = {
        "PowerStatus": "PowerStatus",
        "DiskStatus": "DiskStatus",
        "ReceiverStatus2": "ReceiverStatus",  # Use ReceiverStatus2 for PolaRX5
        "WiFiAPStatus": "WiFiAPStatus",
        "LogStatus": "LogStatus",
        "NTRIPServerStatus": "NTRIPServerStatus",
        "NTRIPClientStatus": "NTRIPClientStatus",
        "ReceiverSetup1": "ReceiverSetup",
        "PVTGeodetic2": "Position",  # Position, accuracy, satellites used
        "ChannelStatus": "Satellites",  # Satellite tracking per channel
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
            Dictionary mapping block names to ASCII file paths
        """
        ascii_files = {}

        # Ensure bin2asc path is set
        if self._bin2asc_path is None:
            self._bin2asc_path = shutil.which("bin2asc")
        if self._bin2asc_path is None:
            self.logger.error("bin2asc not found in PATH")
            return ascii_files

        # Convert each health block type separately for easier parsing
        for block_name, friendly_name in self.HEALTH_BLOCKS.items():
            try:
                # bin2asc command: extract specific message type
                # -f <input> -m <message_name> -t -x -p <output_dir>
                cmd = [
                    self._bin2asc_path,
                    "-f",
                    str(sbf_file),
                    "-m",
                    block_name,  # Message name (e.g., PowerStatus, ReceiverStatus2)
                    "-t",  # Include column titles
                    "-x",  # Include file header
                    "-p",
                    str(output_dir),
                ]

                self.logger.debug(f"Running: {' '.join(cmd)}")

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=str(output_dir),
                )

                # bin2asc creates output file with pattern: inputname.sbf_SBF_BlockName.txt
                output_file = output_dir / f"{sbf_file.name}_SBF_{block_name}.txt"

                if result.returncode == 0 and output_file.exists():
                    ascii_files[block_name] = output_file
                    self.logger.debug(
                        f"Extracted {friendly_name} ({block_name}) to {output_file}"
                    )
                else:
                    self.logger.debug(
                        f"No {friendly_name} ({block_name}) data in file"
                    )

            except subprocess.TimeoutExpired:
                self.logger.error(f"bin2asc timeout for {block_name}")
            except Exception as e:
                self.logger.error(f"Error extracting {block_name}: {e}")

        return ascii_files

    def _parse_ascii_health_data(self, ascii_files: Dict[str, Path]) -> Dict[str, Any]:
        """Parse ASCII files to extract health metrics.

        Args:
            ascii_files: Dictionary mapping block names to ASCII file paths

        Returns:
            Dictionary with structured health data
        """
        from datetime import timezone

        health_data = {
            "extraction_time": datetime.now(timezone.utc).isoformat(),
            "metrics": {},
            "data_quality": {},
            "network": {},
            "receiver_specific": {},
        }

        # Parse each block type
        for block_name, ascii_file in ascii_files.items():
            friendly_name = self.HEALTH_BLOCKS.get(block_name, block_name)

            try:
                if block_name == "PowerStatus":
                    health_data["metrics"].update(
                        self._parse_power_status(ascii_file)
                    )
                elif block_name == "DiskStatus":
                    health_data["data_quality"].update(
                        self._parse_disk_status(ascii_file)
                    )
                elif block_name == "ReceiverStatus2":
                    health_data["metrics"].update(
                        self._parse_receiver_status(ascii_file)
                    )
                elif block_name == "WiFiAPStatus":
                    health_data["network"].update(
                        self._parse_wifi_status(ascii_file)
                    )
                elif block_name == "LogStatus":
                    health_data["data_quality"].update(
                        self._parse_log_status(ascii_file)
                    )
                elif block_name == "NTRIPServerStatus":
                    health_data["network"].update(
                        self._parse_ntrip_server_status(ascii_file)
                    )
                elif block_name == "NTRIPClientStatus":
                    health_data["network"].update(
                        self._parse_ntrip_client_status(ascii_file)
                    )
                elif block_name == "ReceiverSetup1":
                    health_data["receiver_specific"].update(
                        self._parse_receiver_setup(ascii_file)
                    )
                elif block_name == "PVTGeodetic2":
                    health_data["metrics"].update(
                        self._parse_pvt_geodetic(ascii_file)
                    )
                elif block_name == "ChannelStatus":
                    health_data["metrics"].update(
                        self._parse_channel_status(ascii_file)
                    )

            except Exception as e:
                self.logger.error(f"Error parsing {friendly_name}: {e}")

        return health_data

    def _read_csv_file(self, ascii_file: Path) -> list:
        """Read bin2asc CSV output file and return list of row dictionaries.

        bin2asc output format:
            - Header lines (file info, block name) starting with '-' or text
            - Column header line (comma-separated field names)
            - Separator line (dashes)
            - Data rows (comma-separated values)

        Args:
            ascii_file: Path to bin2asc output file

        Returns:
            List of dictionaries, each representing a data row
        """
        rows = []
        try:
            lines = ascii_file.read_text().strip().split('\n')

            # Find the header line (contains column names with commas)
            header_idx = None
            for i, line in enumerate(lines):
                # Skip header info lines and separator lines
                if line.startswith('-') or line.startswith('File') or line.startswith('Block'):
                    continue
                # Found a line with commas that's not a separator
                if ',' in line and not line.startswith('-'):
                    header_idx = i
                    break

            if header_idx is None:
                return rows

            # Parse header
            headers = [h.strip() for h in lines[header_idx].split(',')]

            # Parse data rows (skip separator line after header)
            for line in lines[header_idx + 1:]:
                # Skip separator lines
                if line.startswith('-') or not line.strip():
                    continue

                values = line.split(',')
                if len(values) >= len(headers):
                    row = {}
                    for j, header in enumerate(headers):
                        if j < len(values):
                            row[header] = values[j].strip()
                    rows.append(row)

        except Exception as e:
            self.logger.error(f"Error reading CSV file {ascii_file}: {e}")

        return rows

    def _parse_power_status(self, ascii_file: Path) -> Dict[str, Any]:
        """Parse PowerStatus block from bin2asc CSV output.

        CSV format:
            TOW [s],WNc [w],PowerSource,VinVoltage [V],...
            342000.000,2393,Vin,12.50,...

        Returns:
            Dictionary with power metrics (voltage, source)
        """
        power_data = {}

        try:
            rows = self._read_csv_file(ascii_file)
            if not rows:
                return power_data

            # Get the last row (most recent reading)
            last_row = rows[-1]

            # Extract voltage from "VinVoltage [V]" or "Vin Voltage [V]" column
            voltage = None
            for key in ["VinVoltage [V]", "Vin Voltage [V]"]:
                if key in last_row:
                    try:
                        voltage = float(last_row[key])
                        break
                    except (ValueError, TypeError):
                        pass

            if voltage is not None:
                power_data["power"] = {
                    "voltage": voltage,
                    "unit": "V",
                    "status": self._check_voltage_status(voltage),
                }

            # Extract power source
            for key in ["PowerSource", "Power Source"]:
                if key in last_row:
                    power_data["power_source"] = last_row[key]
                    break

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
        """Parse ReceiverStatus2 block from bin2asc CSV output.

        CSV format:
            TOW [s],WNc [w],CPULoad [%],...,UpTime [s],...,Temperature_degC [°C],...
            342000.000,2393,37,...,673011,...,26.00,...

        Returns:
            Dictionary with receiver metrics (CPU, uptime, temperature)
        """
        receiver_data = {}

        try:
            rows = self._read_csv_file(ascii_file)
            if not rows:
                return receiver_data

            # Get the last row (most recent reading)
            last_row = rows[-1]

            # Extract CPU load
            if "CPULoad [%]" in last_row:
                try:
                    cpu_load = int(last_row["CPULoad [%]"])
                    receiver_data["cpu_load"] = {
                        "percent": cpu_load,
                        "status": self._check_cpu_status(cpu_load),
                    }
                except (ValueError, TypeError):
                    pass

            # Extract temperature
            for key in ["Temperature_degC [°C]", "Temperature [°C]", "Temperature"]:
                if key in last_row:
                    try:
                        temperature = float(last_row[key])
                        receiver_data["temperature"] = {
                            "value": temperature,
                            "unit": "C",
                            "status": self._check_temperature_status(temperature),
                        }
                        break
                    except (ValueError, TypeError):
                        pass

            # Extract uptime
            if "UpTime [s]" in last_row:
                try:
                    receiver_data["uptime_seconds"] = int(last_row["UpTime [s]"])
                except (ValueError, TypeError):
                    pass

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

    def _parse_pvt_geodetic(self, ascii_file: Path) -> Dict[str, Any]:
        """Parse PVTGeodetic2 block from bin2asc CSV output.

        CSV columns include:
            TOW [s], WNc [w], Mode, Error, Latitude [deg], Longitude [deg],
            Height [m], Undulation [m], ..., NrSV, ..., HAccuracy [m], VAccuracy [m]

        Returns:
            Dictionary with position metrics matching TCP extractor format
        """
        import math

        position_data = {}

        try:
            rows = self._read_csv_file(ascii_file)
            if not rows:
                return position_data

            # Get the last row (most recent reading)
            last_row = rows[-1]

            # Extract latitude (may be in radians or degrees)
            lat = None
            for key in ["Latitude [rad]", "Latitude [deg]", "Latitude"]:
                if key in last_row:
                    try:
                        lat = float(last_row[key])
                        if math.isnan(lat):
                            lat = None
                            break
                        # Convert radians to degrees if needed
                        if "[rad]" in key or abs(lat) < math.pi:
                            lat = math.degrees(lat)
                        if abs(lat) > 90:
                            lat = None
                        break
                    except (ValueError, TypeError):
                        pass

            # Extract longitude (may be in radians or degrees)
            lon = None
            for key in ["Longitude [rad]", "Longitude [deg]", "Longitude"]:
                if key in last_row:
                    try:
                        lon = float(last_row[key])
                        if math.isnan(lon):
                            lon = None
                            break
                        # Convert radians to degrees if needed
                        if "[rad]" in key or abs(lon) < math.pi:
                            lon = math.degrees(lon)
                        if abs(lon) > 180:
                            lon = None
                        break
                    except (ValueError, TypeError):
                        pass

            # Extract height
            height = None
            for key in ["Height [m]", "Height"]:
                if key in last_row:
                    try:
                        height = float(last_row[key])
                        if math.isnan(height):
                            height = None
                        break
                    except (ValueError, TypeError):
                        pass

            # Extract horizontal accuracy
            h_accuracy = None
            for key in ["HAccuracy [m]", "HAccuracy"]:
                if key in last_row:
                    try:
                        val = float(last_row[key])
                        if not math.isnan(val) and val < 65535:
                            h_accuracy = round(val, 3)
                        break
                    except (ValueError, TypeError):
                        pass

            # Extract vertical accuracy
            v_accuracy = None
            for key in ["VAccuracy [m]", "VAccuracy"]:
                if key in last_row:
                    try:
                        val = float(last_row[key])
                        if not math.isnan(val) and val < 65535:
                            v_accuracy = round(val, 3)
                        break
                    except (ValueError, TypeError):
                        pass

            # Extract number of satellites
            nr_sv = None
            for key in ["NrSV", "NrSv"]:
                if key in last_row:
                    try:
                        nr_sv = int(last_row[key])
                        break
                    except (ValueError, TypeError):
                        pass

            # Extract fix mode
            fix_mode = None
            mode_names = {
                0: "no_fix",
                1: "standalone",
                2: "dgps",
                3: "fixed",
                4: "float",
                5: "sbas",
                6: "ppp"
            }
            if "Mode" in last_row:
                try:
                    mode_val = int(last_row["Mode"])
                    fix_mode = mode_names.get(mode_val, f"unknown_{mode_val}")
                except (ValueError, TypeError):
                    pass

            # Build position dict matching TCP extractor format
            if lat is not None and lon is not None:
                position_data["position"] = {
                    "latitude": round(lat, 8),
                    "longitude": round(lon, 8),
                    "height": round(height, 3) if height is not None else None,
                    "h_accuracy_m": h_accuracy,
                    "v_accuracy_m": v_accuracy,
                    "satellites_used": nr_sv,
                    "fix_mode": fix_mode,
                    "status": "ok" if fix_mode and fix_mode != "no_fix" else "warning"
                }

        except Exception as e:
            self.logger.error(f"Error parsing PVTGeodetic2: {e}")

        return position_data

    def _parse_channel_status(self, ascii_file: Path) -> Dict[str, Any]:
        """Parse ChannelStatus block from bin2asc CSV output.

        Aggregates satellite counts by constellation.

        CSV columns include:
            TOW [s], WNc [w], SVID, FreqNr, ...

        SVID format can be:
            - Numeric (from TCP): 1-37 GPS, 38-61 GLONASS, etc.
            - Alphanumeric (from bin2asc): G20, R08, E11, C14, etc.

        Returns:
            Dictionary with satellite counts matching TCP extractor format:
            {"satellites": {"total": N, "by_constellation": {...}, "status": "ok"}}
        """
        satellite_data = {}

        try:
            rows = self._read_csv_file(ascii_file)
            if not rows:
                return satellite_data

            # Count unique SVIDs by constellation
            # Use a set to avoid double-counting satellites across multiple rows
            svids_seen: set = set()
            constellation_counts: Dict[str, int] = {}

            for row in rows:
                svid_str = None
                for key in ["SVID", "Svid"]:
                    if key in row and row[key]:
                        svid_str = row[key].strip()
                        break

                if not svid_str or svid_str in svids_seen:
                    continue

                # Skip invalid entries like "Rxx"
                if "xx" in svid_str.lower():
                    continue

                svids_seen.add(svid_str)

                # Determine constellation from SVID format
                const_name = self._svid_str_to_constellation(svid_str)
                if const_name:
                    if const_name not in constellation_counts:
                        constellation_counts[const_name] = 0
                    constellation_counts[const_name] += 1

            total = sum(constellation_counts.values())

            satellite_data["satellites"] = {
                "total": total,
                "by_constellation": constellation_counts,
                "status": "ok" if total >= 4 else "warning"
            }

        except Exception as e:
            self.logger.error(f"Error parsing ChannelStatus: {e}")

        return satellite_data

    @staticmethod
    def _svid_str_to_constellation(svid_str: str) -> Optional[str]:
        """Convert SVID string to constellation name.

        Handles both formats:
            - Alphanumeric (bin2asc): G20, R08, E11, C14, S126, J01, I01
            - Numeric (TCP binary): 1-37 GPS, 38-61 GLONASS, etc.

        Args:
            svid_str: SVID string from ChannelStatus

        Returns:
            Constellation name or None if invalid
        """
        if not svid_str:
            return None

        # Try alphanumeric format first (G20, R08, E11, etc.)
        if svid_str[0].isalpha():
            prefix = svid_str[0].upper()
            const_map = {
                "G": "GPS",
                "R": "GLONASS",
                "E": "Galileo",
                "C": "BeiDou",
                "S": "SBAS",
                "J": "QZSS",
                "I": "IRNSS",
            }
            return const_map.get(prefix)

        # Try numeric format (from TCP binary extraction)
        try:
            svid = int(svid_str)
            if 1 <= svid <= 37:
                return "GPS"
            elif 38 <= svid <= 61:
                return "GLONASS"
            elif 71 <= svid <= 102:
                return "Galileo"
            elif 120 <= svid <= 140:
                return "SBAS"
            elif 141 <= svid <= 180:
                return "BeiDou"
            elif 181 <= svid <= 187:
                return "QZSS"
            elif 191 <= svid <= 197:
                return "IRNSS"
        except (ValueError, TypeError):
            pass

        return None

    @staticmethod
    def _svid_to_constellation(svid: int) -> str:
        """Convert Septentrio SVID to constellation name.

        SVID ranges from Septentrio SBF Reference Guide:
        - GPS: 1-37 (PRN 1-32 + reserved)
        - GLONASS: 38-61
        - Galileo: 71-102
        - SBAS: 120-140
        - BeiDou: 141-180
        - QZSS: 181-187
        - IRNSS/NavIC: 191-197

        Args:
            svid: Satellite Vehicle ID from ChannelStatus

        Returns:
            Constellation name string
        """
        if 1 <= svid <= 37:
            return "GPS"
        elif 38 <= svid <= 61:
            return "GLONASS"
        elif 71 <= svid <= 102:
            return "Galileo"
        elif 120 <= svid <= 140:
            return "SBAS"
        elif 141 <= svid <= 180:
            return "BeiDou"
        elif 181 <= svid <= 187:
            return "QZSS"
        elif 191 <= svid <= 197:
            return "IRNSS"
        else:
            return f"Unknown_{svid}"

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
