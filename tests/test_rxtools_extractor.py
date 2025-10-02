"""Tests for RxTools health data extractor."""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from receivers.health.rxtools_extractor import RxToolsExtractor, RxToolsNotFoundError


class TestRxToolsExtractor:
    """Test RxToolsExtractor class."""

    def test_initialization(self):
        """Test extractor initialization."""
        extractor = RxToolsExtractor(station_id="ELDC")
        assert extractor.station_id == "ELDC"
        assert extractor._bin2asc_path is None

    @patch("shutil.which")
    def test_check_rxtools_available_found(self, mock_which):
        """Test RxTools availability check when found."""
        mock_which.return_value = "/usr/local/bin/bin2asc"

        extractor = RxToolsExtractor(station_id="ELDC")
        assert extractor.check_rxtools_available() is True
        assert extractor._bin2asc_path == "/usr/local/bin/bin2asc"

    @patch("shutil.which")
    def test_check_rxtools_available_not_found(self, mock_which):
        """Test RxTools availability check when not found."""
        mock_which.return_value = None

        extractor = RxToolsExtractor(station_id="ELDC")
        assert extractor.check_rxtools_available() is False
        assert extractor._bin2asc_path is None

    @patch("shutil.which")
    def test_extract_health_rxtools_not_available(self, mock_which):
        """Test extraction raises error when RxTools not available."""
        mock_which.return_value = None

        extractor = RxToolsExtractor(station_id="ELDC")
        sbf_file = Path("/tmp/test.sbf")

        with pytest.raises(RxToolsNotFoundError) as exc_info:
            extractor.extract_health_from_sbf(sbf_file)

        assert "bin2asc not found" in str(exc_info.value)

    @patch("shutil.which")
    def test_extract_health_file_not_found(self, mock_which):
        """Test extraction raises error when SBF file doesn't exist."""
        mock_which.return_value = "/usr/local/bin/bin2asc"

        extractor = RxToolsExtractor(station_id="ELDC")
        sbf_file = Path("/nonexistent/test.sbf")

        with pytest.raises(FileNotFoundError) as exc_info:
            extractor.extract_health_from_sbf(sbf_file)

        assert "SBF file not found" in str(exc_info.value)

    def test_check_voltage_status(self):
        """Test voltage status checking."""
        assert RxToolsExtractor._check_voltage_status(12.5) == "ok"
        assert RxToolsExtractor._check_voltage_status(11.3) == "warning"
        assert RxToolsExtractor._check_voltage_status(10.8) == "critical"

    def test_check_disk_status(self):
        """Test disk usage status checking."""
        assert RxToolsExtractor._check_disk_status(50.0) == "ok"
        assert RxToolsExtractor._check_disk_status(85.0) == "warning"
        assert RxToolsExtractor._check_disk_status(95.0) == "critical"

    def test_check_cpu_status(self):
        """Test CPU load status checking."""
        assert RxToolsExtractor._check_cpu_status(50) == "ok"
        assert RxToolsExtractor._check_cpu_status(80) == "warning"
        assert RxToolsExtractor._check_cpu_status(95) == "critical"

    def test_check_temperature_status(self):
        """Test temperature status checking."""
        assert RxToolsExtractor._check_temperature_status(45.0) == "ok"
        assert RxToolsExtractor._check_temperature_status(65.0) == "warning"
        assert RxToolsExtractor._check_temperature_status(75.0) == "critical"

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_parse_power_status(self, mock_run, mock_which, tmp_path):
        """Test PowerStatus parsing."""
        mock_which.return_value = "/usr/local/bin/bin2asc"

        # Create test ASCII file
        power_file = tmp_path / "test_PowerStatus.txt"
        power_file.write_text(
            """PowerStatus:
ExtSupply: 12.3 V
PowerSource: External
"""
        )

        extractor = RxToolsExtractor(station_id="ELDC")
        result = extractor._parse_power_status(power_file)

        assert "power" in result
        assert result["power"]["voltage"] == 12.3
        assert result["power"]["unit"] == "V"
        assert result["power"]["status"] == "ok"
        assert result["power_source"] == "External"

    @patch("shutil.which")
    def test_parse_disk_status(self, mock_which, tmp_path):
        """Test DiskStatus parsing."""
        mock_which.return_value = "/usr/local/bin/bin2asc"

        # Create test ASCII file
        disk_file = tmp_path / "test_DiskStatus.txt"
        disk_file.write_text(
            """DiskStatus:
DiskUsage: 45678/102400 MB
"""
        )

        extractor = RxToolsExtractor(station_id="ELDC")
        result = extractor._parse_disk_status(disk_file)

        assert "disk_usage" in result
        assert result["disk_usage"]["used_mb"] == 45678
        assert result["disk_usage"]["total_mb"] == 102400
        assert result["disk_usage"]["usage_percent"] == 44.6
        assert result["disk_usage"]["status"] == "ok"

    @patch("shutil.which")
    def test_parse_receiver_status(self, mock_which, tmp_path):
        """Test ReceiverStatus parsing."""
        mock_which.return_value = "/usr/local/bin/bin2asc"

        # Create test ASCII file
        receiver_file = tmp_path / "test_ReceiverStatus.txt"
        receiver_file.write_text(
            """ReceiverStatus:
CPULoad: 35%
Temperature: 45.2 C
UpTime: 123456 s
"""
        )

        extractor = RxToolsExtractor(station_id="ELDC")
        result = extractor._parse_receiver_status(receiver_file)

        assert "cpu_load" in result
        assert result["cpu_load"]["percent"] == 35
        assert result["cpu_load"]["status"] == "ok"

        assert "temperature" in result
        assert result["temperature"]["value"] == 45.2
        assert result["temperature"]["unit"] == "C"
        assert result["temperature"]["status"] == "ok"

        assert result["uptime_seconds"] == 123456

    @patch("shutil.which")
    def test_parse_wifi_status(self, mock_which, tmp_path):
        """Test WiFiAPStatus parsing."""
        mock_which.return_value = "/usr/local/bin/bin2asc"

        # Create test ASCII file
        wifi_file = tmp_path / "test_WiFiAPStatus.txt"
        wifi_file.write_text(
            """WiFiAPStatus:
ConnectedClients: 3
"""
        )

        extractor = RxToolsExtractor(station_id="ELDC")
        result = extractor._parse_wifi_status(wifi_file)

        assert "wifi" in result
        assert result["wifi"]["connected_clients"] == 3
        assert result["wifi"]["status"] == "ok"

    @patch("shutil.which")
    def test_parse_log_status(self, mock_which, tmp_path):
        """Test LogStatus parsing."""
        mock_which.return_value = "/usr/local/bin/bin2asc"

        # Create test ASCII file
        log_file = tmp_path / "test_LogStatus.txt"
        log_file.write_text(
            """LogStatus:
ActiveSessions: 2
"""
        )

        extractor = RxToolsExtractor(station_id="ELDC")
        result = extractor._parse_log_status(log_file)

        assert "logging" in result
        assert result["logging"]["active_sessions"] == 2
        assert result["logging"]["status"] == "ok"

    @patch("shutil.which")
    def test_parse_ntrip_server_status(self, mock_which, tmp_path):
        """Test NTRIPServerStatus parsing."""
        mock_which.return_value = "/usr/local/bin/bin2asc"

        # Create test ASCII file
        ntrip_file = tmp_path / "test_NTRIPServerStatus.txt"
        ntrip_file.write_text(
            """NTRIPServerStatus:
Clients: 5
"""
        )

        extractor = RxToolsExtractor(station_id="ELDC")
        result = extractor._parse_ntrip_server_status(ntrip_file)

        assert "ntrip_server" in result
        assert result["ntrip_server"]["clients"] == 5
        assert result["ntrip_server"]["status"] == "ok"

    @patch("shutil.which")
    def test_parse_ntrip_client_status_connected(self, mock_which, tmp_path):
        """Test NTRIPClientStatus parsing when connected."""
        mock_which.return_value = "/usr/local/bin/bin2asc"

        # Create test ASCII file
        ntrip_file = tmp_path / "test_NTRIPClientStatus.txt"
        ntrip_file.write_text(
            """NTRIPClientStatus:
Connected: Yes
"""
        )

        extractor = RxToolsExtractor(station_id="ELDC")
        result = extractor._parse_ntrip_client_status(ntrip_file)

        assert "ntrip_client" in result
        assert result["ntrip_client"]["connected"] is True
        assert result["ntrip_client"]["status"] == "ok"

    @patch("shutil.which")
    def test_parse_ntrip_client_status_disconnected(self, mock_which, tmp_path):
        """Test NTRIPClientStatus parsing when disconnected."""
        mock_which.return_value = "/usr/local/bin/bin2asc"

        # Create test ASCII file
        ntrip_file = tmp_path / "test_NTRIPClientStatus.txt"
        ntrip_file.write_text(
            """NTRIPClientStatus:
Connected: No
"""
        )

        extractor = RxToolsExtractor(station_id="ELDC")
        result = extractor._parse_ntrip_client_status(ntrip_file)

        assert "ntrip_client" in result
        assert result["ntrip_client"]["connected"] is False
        assert result["ntrip_client"]["status"] == "warning"

    @patch("shutil.which")
    def test_parse_receiver_setup(self, mock_which, tmp_path):
        """Test ReceiverSetup parsing."""
        mock_which.return_value = "/usr/local/bin/bin2asc"

        # Create test ASCII file
        setup_file = tmp_path / "test_ReceiverSetup.txt"
        setup_file.write_text(
            """ReceiverSetup:
FirmwareVersion: 5.4.0
ReceiverType: PolaRx5TR
"""
        )

        extractor = RxToolsExtractor(station_id="ELDC")
        result = extractor._parse_receiver_setup(setup_file)

        assert result["firmware_version"] == "5.4.0"
        assert result["receiver_model"] == "PolaRx5TR"
