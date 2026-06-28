"""Tests for connection health checker."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from receivers.health.connection_checker import (
    ConnectionChecker,
    ConnectionStatus,
    HealthStatus,
)


class TestConnectionChecker:
    """Test ConnectionChecker class."""

    def test_initialization(self):
        """Test checker initialization."""
        checker = ConnectionChecker(host="192.168.1.100", station_id="TEST")
        assert checker.host == "192.168.1.100"
        assert checker.station_id == "TEST"

    def test_connection_status_to_dict(self):
        """Test ConnectionStatus conversion to dictionary."""
        status = ConnectionStatus(
            status=HealthStatus.OK,
            response_time_ms=123.45,
            accessible=True,
            details={"port": 80},
        )

        result = status.to_dict()
        assert result["status"] == "ok"
        assert result["response_time_ms"] == 123.45
        assert result["accessible"] is True
        assert result["port"] == 80

    @patch("subprocess.run")
    def test_check_ping_success(self, mock_run):
        """Test successful ping check."""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = """
PING 192.168.1.100 (192.168.1.100) 56(84) bytes of data.
64 bytes from 192.168.1.100: icmp_seq=1 ttl=64 time=2.5 ms

--- 192.168.1.100 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2003ms
rtt min/avg/max/mdev = 2.345/2.456/2.567/0.089 ms
"""
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        checker = ConnectionChecker(host="192.168.1.100", station_id="TEST")
        result = checker.check_ping()

        assert result.status == HealthStatus.OK
        assert result.accessible is True
        assert result.details["packet_loss"] == 0
        assert result.details["latency_ms"] == 2.456

    @patch("subprocess.run")
    def test_check_ping_failure(self, mock_run):
        """Test failed ping check."""
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Network is unreachable"
        mock_run.return_value = mock_result

        checker = ConnectionChecker(host="192.168.1.100", station_id="TEST")
        result = checker.check_ping()

        assert result.status == HealthStatus.CRITICAL
        assert result.accessible is False
        assert "Ping failed" in result.error_message

    def test_check_all_levels_icmp_blocked_but_port_open(self):
        """Ping fails but the data port is open (router blocks ICMP, e.g. ISAF):
        must NOT skip — probe the port, treat as reachable, run protocol check."""
        checker = ConnectionChecker(host="10.0.0.1", station_id="ISAF")
        ping_fail = ConnectionStatus(status=HealthStatus.CRITICAL, accessible=False)
        http_ok = ConnectionStatus(
            status=HealthStatus.OK, accessible=True, details={"port": 80}
        )
        ftp_ok = ConnectionStatus(
            status=HealthStatus.OK, accessible=True, details={"port": 21}
        )
        with (
            patch.object(checker, "check_ping", return_value=ping_fail),
            patch.object(checker, "check_http_port", return_value=http_ok) as m_http,
            patch.object(checker, "check_ftp", return_value=ftp_ok),
        ):
            results = checker.check_all_levels(
                http_port=80, protocol_type="ftp", protocol_port=21
            )
        assert results["http_port"].accessible is True
        assert not results["http_port"].details.get("skipped")
        assert results["protocol"].accessible is True
        # probed exactly once (the fallback), reused for Level 2 (no double-check)
        assert m_http.call_count == 1
        # ping reclassified as reachable so the verdict isn't falsely CRITICAL
        assert results["router_ping"].status == HealthStatus.OK
        assert results["router_ping"].accessible is True
        assert results["router_ping"].details.get("icmp_blocked") is True

    def test_check_all_levels_truly_down_still_skips(self):
        """Ping fails AND the data port is closed: fail_fast still short-circuits
        to CRITICAL without probing the protocol (truly-offline fast path)."""
        checker = ConnectionChecker(host="10.0.0.1", station_id="DOWN")
        ping_fail = ConnectionStatus(status=HealthStatus.CRITICAL, accessible=False)
        http_closed = ConnectionStatus(
            status=HealthStatus.CRITICAL, accessible=False, details={"port": 80}
        )
        with (
            patch.object(checker, "check_ping", return_value=ping_fail),
            patch.object(checker, "check_http_port", return_value=http_closed),
            patch.object(checker, "check_ftp") as m_ftp,
        ):
            results = checker.check_all_levels(
                http_port=80, protocol_type="ftp", protocol_port=21
            )
        assert results["http_port"].accessible is False
        assert results["protocol"].details.get("skipped") is True
        m_ftp.assert_not_called()

    @patch("socket.socket")
    def test_check_http_port_success(self, mock_socket):
        """Test successful HTTP port check (raw socket connect)."""
        mock_sock_instance = MagicMock()
        mock_socket.return_value = mock_sock_instance

        checker = ConnectionChecker(host="192.168.1.100", station_id="TEST")
        result = checker.check_http_port(port=80)

        assert result.status == HealthStatus.OK
        assert result.accessible is True
        assert result.details["port"] == 80

    @patch("socket.socket")
    def test_check_http_port_timeout(self, mock_socket):
        """Test HTTP port timeout (raw socket connect)."""
        mock_sock_instance = MagicMock()
        mock_sock_instance.connect.side_effect = TimeoutError("timed out")
        mock_socket.return_value = mock_sock_instance

        checker = ConnectionChecker(host="192.168.1.100", station_id="TEST")
        result = checker.check_http_port(port=80)

        assert result.status == HealthStatus.CRITICAL
        assert result.accessible is False
        assert "timeout" in result.error_message.lower()

    @patch("socket.socket")
    def test_check_ftp_success(self, mock_socket):
        """Test successful FTP connection."""
        mock_sock_instance = MagicMock()
        mock_sock_instance.recv.return_value = b"220 FTP Server ready"
        mock_socket.return_value = mock_sock_instance

        checker = ConnectionChecker(host="192.168.1.100", station_id="TEST")
        result = checker.check_ftp(port=21)

        assert result.status == HealthStatus.OK
        assert result.accessible is True
        assert result.details["type"] == "ftp"
        assert result.details["connected"] is True

    @patch("socket.socket")
    def test_check_ftp_refused(self, mock_socket):
        """Test FTP connection refused."""
        mock_sock_instance = MagicMock()
        mock_sock_instance.connect.side_effect = ConnectionRefusedError()
        mock_socket.return_value = mock_sock_instance

        checker = ConnectionChecker(host="192.168.1.100", station_id="TEST")
        result = checker.check_ftp(port=21)

        assert result.status == HealthStatus.CRITICAL
        assert result.accessible is False
        assert "refused" in result.error_message.lower()

    def test_get_default_port(self):
        """Test default port detection."""
        assert ConnectionChecker._get_default_port("ftp") == 21
        assert ConnectionChecker._get_default_port("http") == 80
        assert ConnectionChecker._get_default_port("tcp") == 80

    def test_get_overall_status_all_ok(self):
        """Test overall status when all checks pass."""
        results = {
            "router_ping": ConnectionStatus(status=HealthStatus.OK, accessible=True),
            "http_port": ConnectionStatus(status=HealthStatus.OK, accessible=True),
            "protocol": ConnectionStatus(status=HealthStatus.OK, accessible=True),
        }

        checker = ConnectionChecker(host="192.168.1.100", station_id="TEST")
        overall, message = checker.get_overall_status(results)

        assert overall == HealthStatus.OK
        assert "All connection levels OK" in message

    def test_get_overall_status_critical(self):
        """Test overall status when a check fails critically."""
        results = {
            "router_ping": ConnectionStatus(status=HealthStatus.OK, accessible=True),
            "http_port": ConnectionStatus(
                status=HealthStatus.CRITICAL, accessible=False
            ),
            "protocol": ConnectionStatus(status=HealthStatus.OK, accessible=True),
        }

        checker = ConnectionChecker(host="192.168.1.100", station_id="TEST")
        overall, message = checker.get_overall_status(results)

        assert overall == HealthStatus.CRITICAL
        assert "http_port" in message

    def test_get_overall_status_warning(self):
        """Test overall status with warning condition."""
        results = {
            "router_ping": ConnectionStatus(
                status=HealthStatus.WARNING, accessible=True
            ),
            "http_port": ConnectionStatus(status=HealthStatus.OK, accessible=True),
            "protocol": ConnectionStatus(status=HealthStatus.OK, accessible=True),
        }

        checker = ConnectionChecker(host="192.168.1.100", station_id="TEST")
        overall, message = checker.get_overall_status(results)

        assert overall == HealthStatus.WARNING
        assert "degraded" in message.lower()
