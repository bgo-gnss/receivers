"""Unit tests for TrimbleHTTPExtractor with mock API responses.

Tests parsing of real Trimble /prog/show? API response formats.
"""

import pytest
from unittest.mock import patch, MagicMock
from receivers.health.trimble_http_extractor import TrimbleHTTPExtractor


# Sample responses captured from real ARHO receiver (NetR9)
SAMPLE_VOLTAGES = """<Show Voltages>
port=0 B1 volts=8.36 cap=100%
port=1 ETH volts=0.00 cap=0%
port=2 P2 volts=15.06 cap=100%
<end of Show Voltages>"""

SAMPLE_TEMPERATURE = """Temperature temp=15.3"""

SAMPLE_TRACKING = """<Show TrackingStatus>
Prn=9   Sys=GPS Elv=24 Azm=328 IODE=67  URA=2 L1snr=41 L2snr=38
Prn=31  Sys=GPS Elv=54 Azm=205 IODE=95  URA=2 L1snr=48 L2snr=46 L2Csnr=47
Prn=7   Sys=GPS Elv=38 Azm=93  IODE=117 URA=2 L1snr=48 L2snr=47
Prn=11  Sys=GPS Elv=70 Azm=312 IODE=32  URA=2 L1snr=50 L2snr=48 L2Csnr=49
Prn=17  Sys=GLN Elv=-45 Azm=000 IODE=69  URA=4
Prn=1   Sys=GLN Elv=41 Azm=134 IODE=120 URA=0 L1snr=42 L2snr=41
Prn=2   Sys=GLN Elv=48 Azm=222 IODE=56  URA=4 L1snr=44 L2snr=40
<end of Show TrackingStatus>"""

SAMPLE_POSITION = """<Show Position>
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
<end of Show Position>"""

SAMPLE_SERIAL = """SerialNumber sn=5039K70766"""

SAMPLE_ANTENNA = """<Show Antenna>
name="TRM57971.00     NONE"
height=0.000
method=PhaseCenter
<end of Show Antenna>"""

SAMPLE_REFSTATION = """<RefStation
Name='ARHO'
>"""


class MockResponse:
    """Mock HTTP response."""
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


@pytest.fixture
def extractor():
    """Create extractor instance for testing."""
    return TrimbleHTTPExtractor(
        host="10.4.1.210",
        station_id="ARHO",
        port=8060,
        receiver_type="NetR9"
    )


class TestVoltagesParsing:
    """Test voltage response parsing."""

    def test_parse_voltages_real_format(self, extractor):
        """Test parsing of real voltage response format."""
        with patch.object(extractor, "_fetch_endpoint", return_value=SAMPLE_VOLTAGES):
            result = extractor._fetch_and_parse_voltages()

        assert result is not None
        assert result["voltage"] == 15.06  # Max voltage from ports
        assert result["unit"] == "V"
        assert result["status"] == "warning"  # 15.06V >= voltage_warning_high (15.0)
        assert len(result["ports"]) == 3
        assert result["ports"][0]["name"] == "B1"
        assert result["ports"][0]["voltage"] == 8.36
        assert result["ports"][2]["name"] == "P2"
        assert result["ports"][2]["voltage"] == 15.06

    def test_voltage_warning_threshold(self, extractor):
        """Test voltage warning status."""
        low_voltage = """port=0 B1 volts=11.5 cap=100%"""
        with patch.object(extractor, "_fetch_endpoint", return_value=low_voltage):
            result = extractor._fetch_and_parse_voltages()

        assert result is not None
        assert result["status"] == "warning"  # 11.5V between critical_low (11.0) and warning_low (11.8)

    def test_voltage_critical_threshold(self, extractor):
        """Test voltage critical status."""
        critical_voltage = """port=0 B1 volts=9.5 cap=50%"""
        with patch.object(extractor, "_fetch_endpoint", return_value=critical_voltage):
            result = extractor._fetch_and_parse_voltages()

        assert result is not None
        assert result["status"] == "critical"


class TestTemperatureParsing:
    """Test temperature response parsing."""

    def test_parse_temperature_real_format(self, extractor):
        """Test parsing of real temperature response format."""
        with patch.object(extractor, "_fetch_endpoint", return_value=SAMPLE_TEMPERATURE):
            result = extractor._fetch_and_parse_temperature()

        assert result is not None
        assert result["value"] == 15.3
        assert result["unit"] == "C"
        assert result["status"] == "ok"

    def test_temperature_warning(self, extractor):
        """Test temperature warning status."""
        warm = """Temperature temp=55.0"""
        with patch.object(extractor, "_fetch_endpoint", return_value=warm):
            result = extractor._fetch_and_parse_temperature()

        assert result is not None
        assert result["status"] == "warning"  # 55.0°C between warning_high (50.0) and critical_high (60.0)

    def test_temperature_critical(self, extractor):
        """Test temperature critical status."""
        hot = """Temperature temp=75.0"""
        with patch.object(extractor, "_fetch_endpoint", return_value=hot):
            result = extractor._fetch_and_parse_temperature()

        assert result is not None
        assert result["status"] == "critical"


class TestTrackingParsing:
    """Test satellite tracking response parsing."""

    def test_parse_tracking_real_format(self, extractor):
        """Test parsing of real tracking status response."""
        with patch.object(extractor, "_fetch_endpoint", return_value=SAMPLE_TRACKING):
            result = extractor._fetch_and_parse_tracking()

        assert result is not None
        # 6 satellites with positive elevation (GLN Prn=17 has Elv=-45)
        assert result["total"] == 6
        assert result["visible"] == 7  # Total parsed lines
        assert result["status"] == "warning"  # 6 < sat_warning (8)
        assert result["by_constellation"]["GPS"] == 4
        assert result["by_constellation"]["GLONASS"] == 2
        assert len(result["satellites"]) == 6

    def test_tracking_with_snr(self, extractor):
        """Test that SNR values are parsed."""
        with patch.object(extractor, "_fetch_endpoint", return_value=SAMPLE_TRACKING):
            result = extractor._fetch_and_parse_tracking()

        # Check that L1 SNR values are extracted
        gps_sat = next(s for s in result["satellites"] if s["prn"] == 9)
        assert gps_sat["l1_snr"] == 41

    def test_tracking_warning(self, extractor):
        """Test tracking warning status (few satellites)."""
        few_sats = """Prn=9 Sys=GPS Elv=24 Azm=328 IODE=67 URA=2 L1snr=41
Prn=31 Sys=GPS Elv=54 Azm=205 IODE=95 URA=2 L1snr=48
Prn=7 Sys=GPS Elv=38 Azm=93 IODE=117 URA=2 L1snr=48"""
        with patch.object(extractor, "_fetch_endpoint", return_value=few_sats):
            result = extractor._fetch_and_parse_tracking()

        assert result is not None
        assert result["total"] == 3
        assert result["status"] == "critical"  # 3 < sat_critical (4)

    def test_tracking_critical(self, extractor):
        """Test tracking critical status (very few satellites)."""
        one_sat = """Prn=9 Sys=GPS Elv=24 Azm=328 IODE=67 URA=2 L1snr=41"""
        with patch.object(extractor, "_fetch_endpoint", return_value=one_sat):
            result = extractor._fetch_and_parse_tracking()

        assert result is not None
        assert result["total"] == 1
        assert result["status"] == "critical"  # 1 < sat_critical (4)


class TestPositionParsing:
    """Test position response parsing."""

    def test_parse_position_real_format(self, extractor):
        """Test parsing of real position response."""
        with patch.object(extractor, "_fetch_endpoint", return_value=SAMPLE_POSITION):
            result = extractor._fetch_and_parse_position()

        assert result is not None
        assert abs(result["latitude"] - 66.1930960854) < 0.0001
        assert abs(result["longitude"] - (-17.1090319429)) < 0.0001
        assert abs(result["height"] - 128.192) < 0.01
        assert result["fix_type"] == "WGS84,3D,Autonomous"
        assert result["pdop"] == 1.8
        assert result["hdop"] == 0.8
        assert result["vdop"] == 1.6
        assert result["tdop"] == 0.9
        assert result["satellites_used"] == 12


class TestSystemInfoParsing:
    """Test system info parsing."""

    def test_parse_serial_number(self, extractor):
        """Test serial number parsing."""
        def mock_fetch(endpoint):
            if endpoint == "serial":
                return SAMPLE_SERIAL
            return None

        with patch.object(extractor, "_fetch_endpoint", side_effect=mock_fetch):
            result = extractor._fetch_system_info()

        assert result is not None
        assert result["serial_number"] == "5039K70766"

    def test_parse_antenna_info(self, extractor):
        """Test antenna info parsing."""
        def mock_fetch(endpoint):
            if endpoint == "antenna":
                return SAMPLE_ANTENNA
            return None

        with patch.object(extractor, "_fetch_endpoint", side_effect=mock_fetch):
            result = extractor._fetch_system_info()

        assert result is not None
        assert result["antenna_type"] == "TRM57971.00     NONE"
        assert result["antenna_height"] == 0.0

    def test_parse_refstation_name(self, extractor):
        """Test reference station name parsing."""
        def mock_fetch(endpoint):
            if endpoint == "refstation":
                return SAMPLE_REFSTATION
            return None

        with patch.object(extractor, "_fetch_endpoint", side_effect=mock_fetch):
            result = extractor._fetch_system_info()

        assert result is not None
        assert result["station_name"] == "ARHO"


class TestFullExtraction:
    """Test full health extraction with mocked endpoints."""

    def test_full_extraction_healthy(self, extractor):
        """Test full extraction returns proper structure."""
        def mock_fetch(endpoint):
            responses = {
                "voltages": SAMPLE_VOLTAGES,
                "temperature": SAMPLE_TEMPERATURE,
                "tracking": SAMPLE_TRACKING,
                "position": SAMPLE_POSITION,
                "serial": SAMPLE_SERIAL,
                "antenna": SAMPLE_ANTENNA,
                "refstation": SAMPLE_REFSTATION,
            }
            return responses.get(endpoint)

        def mock_connection():
            return {"status": "ok", "port": 8060, "accessible": True}

        with patch.object(extractor, "_fetch_endpoint", side_effect=mock_fetch):
            with patch.object(extractor, "_test_connection", return_value=mock_connection()):
                result = extractor.extract_health_data()

        # Check overall structure
        assert result["station_id"] == "ARHO"
        assert result["receiver_type"] == "NetR9"
        assert result["schema_version"] == "1.0"
        assert result["overall_status"] == "warning"  # 15.06V >= voltage_warning_high (15.0)

        # Check metrics exist
        assert "power" in result["metrics"]
        assert "temperature" in result["metrics"]
        assert "satellites" in result["metrics"]
        assert "position" in result["metrics"]
        assert "system" in result["metrics"]

        # Check unavailable metrics marked correctly
        assert result["metrics"]["cpu_load"]["available"] is False
        assert result["metrics"]["disk"]["available"] is False

        # Check network marked as unavailable
        assert result["network"]["ntrip_client"]["available"] is False

        # Check extraction metadata
        assert result["extraction_metadata"]["data_source"] == "trimble_http_api"

    def test_full_extraction_with_connection_failure(self, extractor):
        """Test extraction handles connection failure gracefully."""
        def mock_connection():
            return {"status": "critical", "port": 8060, "accessible": False, "error": "Timeout"}

        with patch.object(extractor, "_test_connection", return_value=mock_connection()):
            with patch.object(extractor, "_fetch_endpoint", return_value=None):
                result = extractor.extract_health_data()

        assert result["overall_status"] == "critical"
        assert result["connection"]["http_port"]["status"] == "critical"


class TestStatusCalculation:
    """Test overall status calculation."""

    def test_all_ok_is_healthy(self, extractor):
        """Test that all ok statuses result in healthy."""
        statuses = ["ok", "ok", "ok"]
        result = extractor._calculate_overall_status(statuses)
        assert result == "healthy"

    def test_any_critical_is_critical(self, extractor):
        """Test that any critical status makes overall critical."""
        statuses = ["ok", "critical", "ok"]
        result = extractor._calculate_overall_status(statuses)
        assert result == "critical"

    def test_warning_without_critical(self, extractor):
        """Test warning status without critical."""
        statuses = ["ok", "warning", "ok"]
        result = extractor._calculate_overall_status(statuses)
        assert result == "warning"

    def test_empty_statuses(self, extractor):
        """Test empty status list."""
        result = extractor._calculate_overall_status([])
        assert result == "unknown"

    def test_status_counting(self, extractor):
        """Test status counting."""
        statuses = ["ok", "ok", "warning", "critical", "unknown"]
        counts = extractor._count_statuses(statuses)
        assert counts["healthy"] == 2
        assert counts["warning"] == 1
        assert counts["critical"] == 1
        assert counts["unknown"] == 1


# --- NetRS Fixtures and Tests ---

@pytest.fixture
def netrs_extractor():
    """Create NetRS extractor instance for testing."""
    return TrimbleHTTPExtractor(
        host="10.4.1.50",
        station_id="BLEI",
        port=8060,
        receiver_type="NetRS",
    )


def _mock_response(text, status_code=200):
    """Create a mock requests.Response object."""
    resp = MagicMock()
    resp.text = text
    resp.status_code = status_code
    return resp


class TestNetRSVoltages:
    """Test NetRS voltage fallback chain."""

    def test_netrs_voltage_input_format(self, netrs_extractor):
        """Test parsing NetRS 'Voltage input=1 volts=12.34' format."""
        # /prog/show?Voltages returns ERROR → triggers fallback to input-specific endpoints
        def mock_get(url, **kwargs):
            if "input=1" in url:
                return _mock_response("Voltage input=1 volts=12.34")
            if "input=2" in url:
                return _mock_response("", status_code=404)
            return _mock_response("ERROR", status_code=200)

        with patch.object(netrs_extractor, "_fetch_endpoint", return_value="ERROR"):
            with patch("requests.get", side_effect=mock_get):
                result = netrs_extractor._fetch_and_parse_voltages()

        assert result is not None
        assert result["voltage"] == 12.34
        assert result["unit"] == "V"
        assert len(result["ports"]) == 1
        assert result["ports"][0]["name"] == "Primary"

    def test_netrs_voltage_dual_inputs(self, netrs_extractor):
        """Test both voltage inputs returning data."""
        def mock_get(url, **kwargs):
            if "input=1" in url:
                return _mock_response("Voltage input=1 volts=12.34")
            if "input=2" in url:
                return _mock_response("Voltage input=2 volts=13.50")
            return _mock_response("ERROR")

        with patch.object(netrs_extractor, "_fetch_endpoint", return_value="ERROR"):
            with patch("requests.get", side_effect=mock_get):
                result = netrs_extractor._fetch_and_parse_voltages()

        assert result is not None
        assert result["voltage"] == 13.50  # Max of both inputs
        assert len(result["ports"]) == 2
        assert result["ports"][0]["name"] == "Primary"
        assert result["ports"][0]["voltage"] == 12.34
        assert result["ports"][1]["name"] == "Secondary"
        assert result["ports"][1]["voltage"] == 13.50

    def test_netrs_voltage_single_input(self, netrs_extractor):
        """Test only one voltage input responding."""
        def mock_get(url, **kwargs):
            if "input=1" in url:
                return _mock_response("no data", status_code=404)
            if "input=2" in url:
                return _mock_response("Voltage input=2 volts=14.20")
            return _mock_response("ERROR")

        with patch.object(netrs_extractor, "_fetch_endpoint", return_value="ERROR"):
            with patch("requests.get", side_effect=mock_get):
                result = netrs_extractor._fetch_and_parse_voltages()

        assert result is not None
        assert result["voltage"] == 14.20
        assert len(result["ports"]) == 1
        assert result["ports"][0]["name"] == "Secondary"

    def test_netrs_voltage_fallback_on_error(self, netrs_extractor):
        """Test /prog/show?Voltages returns ERROR, triggers fallback."""
        # First call: _fetch_endpoint("voltages") returns "ERROR"
        # Then fallback calls requests.get for input-specific endpoints
        def mock_get(url, **kwargs):
            if "input=1" in url:
                return _mock_response("Voltage input=1 volts=11.80")
            if "input=2" in url:
                return _mock_response("Voltage input=2 volts=12.10")
            return _mock_response("ERROR")

        with patch.object(netrs_extractor, "_fetch_endpoint", return_value="ERROR"):
            with patch("requests.get", side_effect=mock_get):
                result = netrs_extractor._fetch_and_parse_voltages()

        assert result is not None
        assert result["voltage"] == 12.10
        assert result["status"] == "ok"

    def test_netrs_voltage_all_fail_returns_none(self, netrs_extractor):
        """Test all voltage endpoints failing returns None."""
        from requests.exceptions import Timeout

        def mock_get(url, **kwargs):
            raise Timeout("Connection timed out")

        with patch.object(netrs_extractor, "_fetch_endpoint", return_value="ERROR"):
            with patch("requests.get", side_effect=mock_get):
                result = netrs_extractor._fetch_and_parse_voltages()

        assert result is None


class TestNetRSUptime:
    """Test NetRS uptime Activity page parsing."""

    def test_netrs_uptime_activity_page(self, netrs_extractor):
        """Test parsing uptime from Activity CGI page."""
        html = (
            '<html><body><b>Run Time:</b><br />'
            'System has been running for 159 days 1 hours 31 minutes'
            '</body></html>'
        )

        with patch("requests.get", return_value=_mock_response(html)):
            result = netrs_extractor._fetch_uptime_from_activity_page()

        assert result is not None
        assert result["days"] == 159
        assert result["hours"] == 1
        assert result["minutes"] == 31
        assert result["seconds"] == (159 * 86400) + (1 * 3600) + (31 * 60)
        assert result["source"] == "activity_page"
        assert result["formatted"] == "159d 1h 31m"

    def test_netrs_uptime_merge_xml_preferred(self, netrs_extractor):
        """When merge.xml provides uptime, Activity page is not called."""
        merge_xml = (
            "<uptime><day>10</day><hour>5</hour>"
            "<min>30</min><sec>15</sec></uptime>"
        )
        result = netrs_extractor._parse_uptime_from_merge_xml(merge_xml)

        assert result is not None
        assert result["days"] == 10
        assert result["hours"] == 5
        assert result["source"] == "merge_xml"


class TestFullExtractionNetRS:
    """Test full extraction with NetRS receiver type."""

    def test_full_extraction_netrs(self, netrs_extractor):
        """Full extraction with NetRS, verify voltage fallback and disk unavailable."""
        def mock_fetch_endpoint(endpoint_name):
            responses = {
                "voltages": "ERROR",  # Triggers fallback to input-specific
                "temperature": SAMPLE_TEMPERATURE,
                "tracking": SAMPLE_TRACKING,
                "position": SAMPLE_POSITION,
                "serial": SAMPLE_SERIAL,
                "antenna": SAMPLE_ANTENNA,
                "refstation": SAMPLE_REFSTATION,
            }
            return responses.get(endpoint_name)

        def mock_get(url, **kwargs):
            # NetRS voltage fallback endpoints
            if "input=1" in url:
                return _mock_response("Voltage input=1 volts=12.50")
            if "input=2" in url:
                return _mock_response("Voltage input=2 volts=13.80")
            # Root page for merge.xml discovery (no CACHEDIR → merge.xml unavailable)
            return _mock_response("<html>NetRS</html>")

        def mock_connection():
            return {"status": "ok", "port": 8060, "accessible": True}

        with patch.object(netrs_extractor, "_fetch_endpoint", side_effect=mock_fetch_endpoint):
            with patch.object(netrs_extractor, "_test_connection", return_value=mock_connection()):
                with patch("requests.get", side_effect=mock_get):
                    result = netrs_extractor.extract_health_data()

        assert result["station_id"] == "BLEI"
        assert result["receiver_type"] == "NetRS"
        assert result["schema_version"] == "1.0"

        # Voltage came from fallback (input-specific endpoints)
        assert "power" in result["metrics"]
        assert result["metrics"]["power"]["voltage"] == 13.80
        assert len(result["metrics"]["power"]["ports"]) == 2

        # Disk should be unavailable (no merge.xml on NetRS without CACHEDIR)
        assert result["metrics"]["disk"]["available"] is False

        # Other metrics should be present
        assert "temperature" in result["metrics"]
        assert "satellites" in result["metrics"]
        assert "position" in result["metrics"]


# --- NetR5 Fixtures and Tests ---

@pytest.fixture
def netr5_extractor():
    """Create NetR5 extractor instance for testing."""
    return TrimbleHTTPExtractor(
        host="10.4.1.60",
        station_id="TEST",
        port=8060,
        receiver_type="NetR5",
    )


class TestNetR5Extraction:
    """Test NetR5 uses standard Trimble HTTP API (same as NetR9)."""

    def test_netr5_full_extraction(self, netr5_extractor):
        """Confirm NetR5 uses standard /prog/show?Voltages format."""
        def mock_fetch(endpoint):
            responses = {
                "voltages": SAMPLE_VOLTAGES,
                "temperature": SAMPLE_TEMPERATURE,
                "tracking": SAMPLE_TRACKING,
                "position": SAMPLE_POSITION,
                "serial": SAMPLE_SERIAL,
                "antenna": SAMPLE_ANTENNA,
                "refstation": SAMPLE_REFSTATION,
            }
            return responses.get(endpoint)

        def mock_connection():
            return {"status": "ok", "port": 8060, "accessible": True}

        with patch.object(netr5_extractor, "_fetch_endpoint", side_effect=mock_fetch):
            with patch.object(netr5_extractor, "_test_connection", return_value=mock_connection()):
                with patch.object(netr5_extractor, "_fetch_merge_xml", return_value=None):
                    result = netr5_extractor.extract_health_data()

        assert result["station_id"] == "TEST"
        assert result["receiver_type"] == "NetR5"
        # Overall status depends on thresholds; 15.06V exceeds voltage_warning_high=15.0
        assert result["overall_status"] in ("healthy", "warning")

        # Uses standard voltage format (same as NetR9)
        assert "power" in result["metrics"]
        assert result["metrics"]["power"]["voltage"] == 15.06
        assert len(result["metrics"]["power"]["ports"]) == 3

    def test_netr5_receiver_type_in_output(self, netr5_extractor):
        """Verify receiver_type field says NetR5 in output."""
        def mock_connection():
            return {"status": "ok", "port": 8060, "accessible": True}

        with patch.object(netr5_extractor, "_fetch_endpoint", return_value=None):
            with patch.object(netr5_extractor, "_test_connection", return_value=mock_connection()):
                with patch.object(netr5_extractor, "_fetch_merge_xml", return_value=None):
                    result = netr5_extractor.extract_health_data()

        assert result["receiver_type"] == "NetR5"
        assert result["extraction_metadata"]["data_source"] == "trimble_http_api"
