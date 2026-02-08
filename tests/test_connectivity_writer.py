"""Tests for ConnectivityWriter."""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call

from receivers.health.connectivity_writer import ConnectivityWriter


def _make_health_data(
    ping_accessible=True,
    response_time_ms=15.2,
    packet_loss=0.0,
    ftp_open=True,
    ftp_port=2160,
    http_open=True,
    http_port=8060,
    control_open=True,
    control_port=28784,
    protocol_type="ftp",
    timestamp="2025-10-01T12:00:00Z",
):
    """Build a health_data dictionary for testing."""
    return {
        "timestamp": timestamp,
        "connection": {
            "router_ping": {
                "accessible": ping_accessible,
                "response_time_ms": response_time_ms,
                "packet_loss": packet_loss,
            },
            "protocol": {
                "type": protocol_type,
            },
        },
        "metrics": {
            "ports": {
                "ftp": {
                    "port": ftp_port,
                    "open": ftp_open,
                    "status": "open" if ftp_open else "refused",
                },
                "http": {
                    "port": http_port,
                    "open": http_open,
                    "status": "open" if http_open else "timeout",
                },
                "control": {
                    "port": control_port,
                    "open": control_open,
                    "status": "open" if control_open else "refused",
                },
            },
        },
    }


class TestConnectivityWriter:
    """Test ConnectivityWriter class."""

    def test_initialization(self):
        """Test writer initialization."""
        writer = ConnectivityWriter()
        assert writer.logger is not None

    def test_initialization_with_logger(self):
        """Test writer initialization with custom logger."""
        logger = MagicMock()
        writer = ConnectivityWriter(logger)
        assert writer.logger is logger


class TestExtractTimestamp:
    """Test timestamp extraction from health data."""

    def test_extract_iso_timestamp(self):
        """Test extracting ISO8601 timestamp."""
        writer = ConnectivityWriter()
        health_data = {"timestamp": "2025-10-01T12:00:00Z"}
        ts = writer._extract_timestamp(health_data)
        assert ts.year == 2025
        assert ts.month == 10
        assert ts.day == 1
        assert ts.hour == 12
        assert ts.tzinfo is not None

    def test_extract_datetime_timestamp(self):
        """Test extracting datetime object."""
        writer = ConnectivityWriter()
        expected = datetime(2025, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        health_data = {"timestamp": expected}
        ts = writer._extract_timestamp(health_data)
        assert ts == expected

    def test_extract_naive_datetime(self):
        """Test extracting naive datetime gets UTC timezone added."""
        writer = ConnectivityWriter()
        naive = datetime(2025, 10, 1, 12, 0, 0)
        health_data = {"timestamp": naive}
        ts = writer._extract_timestamp(health_data)
        assert ts.tzinfo == timezone.utc

    def test_no_timestamp_uses_now(self):
        """Test fallback to current time when no timestamp."""
        writer = ConnectivityWriter()
        health_data = {}
        ts = writer._extract_timestamp(health_data)
        assert ts.tzinfo is not None
        # Should be very recent
        diff = abs((datetime.now(timezone.utc) - ts).total_seconds())
        assert diff < 5

    def test_invalid_timestamp_uses_now(self):
        """Test fallback when timestamp is unparseable."""
        writer = ConnectivityWriter()
        health_data = {"timestamp": "not-a-date"}
        ts = writer._extract_timestamp(health_data)
        assert ts.tzinfo is not None


class TestWritePingStatus:
    """Test ping status writing logic."""

    def test_online_with_ping_and_ports(self):
        """Test station is online when ping works and ports are open."""
        writer = ConnectivityWriter()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        health_data = _make_health_data(ping_accessible=True, ftp_open=True)
        ts = datetime(2025, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

        writer._write_ping_status(mock_conn, "ELDC", health_data, ts)

        # Verify INSERT was called
        mock_cursor.execute.assert_called_once()
        sql, params = mock_cursor.execute.call_args[0]
        assert "block_ping_status" in sql
        assert params[0] == "ELDC"  # station_id
        assert params[1] == ts  # timestamp (not NOW())
        assert params[2] is True  # is_online
        assert params[5] is None  # no error

    def test_offline_when_ping_fails(self):
        """Test station is offline when ICMP ping fails."""
        writer = ConnectivityWriter()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        health_data = _make_health_data(ping_accessible=False)
        ts = datetime(2025, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

        writer._write_ping_status(mock_conn, "ELDC", health_data, ts)

        sql, params = mock_cursor.execute.call_args[0]
        assert params[2] is False  # is_online
        assert "ping failed" in params[5]  # error message

    def test_online_when_ping_works_but_ports_closed(self):
        """Test station is online when ping works even if all ports closed.

        is_online reflects network reachability (ping), not service availability.
        Port status is tracked separately in block_port_status.
        """
        writer = ConnectivityWriter()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        health_data = _make_health_data(
            ping_accessible=True,
            ftp_open=False,
            http_open=False,
            control_open=False,
        )
        ts = datetime(2025, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

        writer._write_ping_status(mock_conn, "ELDC", health_data, ts)

        sql, params = mock_cursor.execute.call_args[0]
        assert params[2] is True  # is_online (ping works)
        assert params[5] is None  # no error message

    def test_uses_explicit_timestamp_not_now(self):
        """Test that explicit timestamp is used instead of NOW().

        This verifies the fix for the 20.6-hour Last Checked bug.
        """
        writer = ConnectivityWriter()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        health_data = _make_health_data()
        ts = datetime(2025, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

        writer._write_ping_status(mock_conn, "ELDC", health_data, ts)

        sql, params = mock_cursor.execute.call_args[0]
        # Verify timestamp is the explicit value, not "NOW()"
        assert "NOW()" not in sql
        assert params[1] == ts


class TestWritePortStatus:
    """Test port status writing logic."""

    def test_ftp_download_port(self):
        """Test FTP download port for Septentrio receivers."""
        writer = ConnectivityWriter()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        health_data = _make_health_data(protocol_type="ftp", ftp_port=2160)
        ts = datetime(2025, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

        writer._write_port_status(mock_conn, "ELDC", health_data, ts)

        sql, params = mock_cursor.execute.call_args[0]
        assert "block_port_status" in sql
        assert params[0] == "ELDC"
        assert params[1] == ts
        assert params[2] == 2160  # download_port (FTP)

    def test_http_download_port(self):
        """Test HTTP download port for Trimble receivers."""
        writer = ConnectivityWriter()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        health_data = _make_health_data(protocol_type="http", http_port=80)
        ts = datetime(2025, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

        writer._write_port_status(mock_conn, "MANA", health_data, ts)

        sql, params = mock_cursor.execute.call_args[0]
        assert params[2] == 80  # download_port (HTTP)


class TestWriteConnectivityStatus:
    """Test full connectivity status writing."""

    @patch("receivers.health.connectivity_writer.DatabaseConnectionFactory")
    def test_writes_both_tables(self, mock_factory):
        """Test that both ping and port status are written."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_factory.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_factory.connection.return_value.__exit__ = MagicMock(return_value=False)

        writer = ConnectivityWriter()
        health_data = _make_health_data()
        result = writer.write_connectivity_status("ELDC", health_data)

        assert result is True
        # Should have 2 execute calls (ping + port)
        assert mock_cursor.execute.call_count == 2

    @patch("receivers.health.connectivity_writer.DatabaseConnectionFactory")
    def test_returns_false_on_db_error(self, mock_factory):
        """Test that False is returned on database errors."""
        mock_factory.connection.side_effect = Exception("connection failed")

        writer = ConnectivityWriter()
        health_data = _make_health_data()
        result = writer.write_connectivity_status("ELDC", health_data)

        assert result is False

    @patch("receivers.health.connectivity_writer.DatabaseConnectionFactory")
    def test_write_ping_only(self, mock_factory):
        """Test writing only ping status for error cases."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_factory.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_factory.connection.return_value.__exit__ = MagicMock(return_value=False)

        writer = ConnectivityWriter()
        health_data = {
            "connection": {
                "tcp": {"status": "failed"},
                "error": "connection timeout",
            },
        }
        result = writer.write_ping_only("ELDC", health_data)

        assert result is True
        # Should have 1 execute call (ping only)
        assert mock_cursor.execute.call_count == 1
        sql = mock_cursor.execute.call_args[0][0]
        assert "block_ping_status" in sql


class TestConstants:
    """Test constants module."""

    def test_colors_exist(self):
        """Test color constants are accessible."""
        from receivers.health.constants import Colors

        assert Colors.GREEN == "#73BF69"
        assert Colors.RED == "#F2495C"
        assert Colors.YELLOW == "#FADE2A"

    def test_satellite_thresholds(self):
        """Test satellite threshold constants."""
        from receivers.health.constants import SatelliteThresholds

        assert SatelliteThresholds.GREEN_MIN == 16
        assert SatelliteThresholds.YELLOW_MIN == 8

    def test_checked_thresholds(self):
        """Test checked time thresholds."""
        from receivers.health.constants import CheckedThresholds

        assert CheckedThresholds.GREEN_MAX == 7200  # 2 hours
        assert CheckedThresholds.YELLOW_MAX == 86400  # 24 hours
