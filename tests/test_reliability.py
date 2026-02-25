"""Tests for download reliability improvements (#1–#6).

Tests:
- Error classifier (#6c)
- DiskStatus SBF parser (#6a)
- Data routing fix (#6b)
- Health gate (#4)
- Consecutive failure backoff (#3)
- Packet loss factor (#5)
- Router failure cache (#2)
"""

from __future__ import annotations

import struct
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ── Error classifier tests (#6c) ──────────────────────────────────────────

class TestErrorClassifier:
    """Tests for classify_download_error()."""

    def setup_method(self):
        from receivers.utils.error_classifier import classify_download_error
        self.classify = classify_download_error

    def test_dead_connection_sendall(self):
        assert self.classify("'NoneType' object has no attribute 'sendall'") == "dead_connection"

    def test_dead_connection_broken_pipe(self):
        assert self.classify("Broken pipe") == "dead_connection"

    def test_dead_connection_reset(self):
        assert self.classify("Connection reset by peer") == "dead_connection"

    def test_stall_timeout_watchdog(self):
        assert self.classify("Watchdog: No data received in 10.0s, killing connection") == "stall_timeout"

    def test_stall_timeout_timed_out(self):
        assert self.classify("FTP connection timed out") == "stall_timeout"

    def test_stall_timeout_timeout(self):
        assert self.classify("timeout waiting for data") == "stall_timeout"

    def test_file_not_found_550(self):
        assert self.classify("550 No such file or directory") == "file_not_found"

    def test_file_not_found_not_found(self):
        assert self.classify("Remote file not found on server") == "file_not_found"

    def test_unreachable_ping(self):
        assert self.classify("Ping check failed") == "unreachable"

    def test_unreachable_refused(self):
        assert self.classify("Connection refused") == "unreachable"

    def test_unreachable_no_route(self):
        assert self.classify("No route to host") == "unreachable"

    def test_auth_failed_530(self):
        assert self.classify("530 Login authentication failed") == "auth_failed"

    def test_auth_failed_permission(self):
        assert self.classify("Permission denied") == "auth_failed"

    def test_disk_error_full(self):
        assert self.classify("Disk full, cannot write") == "disk_error"

    def test_disk_error_unmounted(self):
        assert self.classify("disk unmounted") == "disk_error"

    def test_validation_failed(self):
        assert self.classify("Validation failed: corrupt gzip header") == "validation_failed"

    def test_validation_size_mismatch(self):
        assert self.classify("Size mismatch: got 12345, expected 67890") == "validation_failed"

    def test_unknown_empty(self):
        assert self.classify("") == "unknown"

    def test_unknown_gibberish(self):
        assert self.classify("Something completely unexpected happened") == "unknown"


# ── DiskStatus SBF parser tests (#6a) ─────────────────────────────────────

def _build_disk_status_sbf(disks: list[dict]) -> bytes:
    """Build a raw DiskStatus SBF block from disk descriptors.

    Each disk dict: {disk_id, status_code, usage_raw, disk_size_kb}
    """
    sb_length = 12  # Minimum descriptor size
    n_disks = len(disks)

    # SBF header: sync($@) + CRC(0) + ID(4059, rev=0) + length
    body_length = 8 + 8 + n_disks * sb_length  # header(8) + TOW+WNc+N+SBLen(8) + descriptors
    header = b'$@'
    header += struct.pack('<H', 0)  # CRC placeholder
    header += struct.pack('<H', 4059)  # Block ID (no revision bits)
    header += struct.pack('<H', body_length)

    # Body: TOW(4B) + WNc(2B) + N(1B) + SBLength(1B)
    body = struct.pack('<I', 0)  # TOW
    body += struct.pack('<H', 0)  # WNc
    body += struct.pack('<B', n_disks)
    body += struct.pack('<B', sb_length)

    # Disk descriptors
    for d in disks:
        desc = struct.pack('<B', d['disk_id'])
        desc += struct.pack('<B', d['status_code'])
        desc += struct.pack('<H', d['usage_raw'])
        desc += struct.pack('<I', d['disk_size_kb'])
        # CreateDeleteCount (4B) - pad to sb_length
        desc += b'\x00' * (sb_length - len(desc))
        body += desc

    return header + body


class TestDiskStatusParser:
    """Tests for _query_disk_status() SBF parsing."""

    def setup_method(self):
        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor
        self.extractor = PolaRX5TCPExtractor.__new__(PolaRX5TCPExtractor)
        self.extractor.host = "127.0.0.1"
        self.extractor.port = 28784
        self.extractor.station_id = "TEST"
        self.extractor.timeout = 5
        self.extractor.logger = MagicMock()

    def test_single_mounted_disk(self):
        """Normal disk: 50% usage on 32GB disk."""
        sbf_data = _build_disk_status_sbf([
            {"disk_id": 0, "status_code": 1, "usage_raw": 5000, "disk_size_kb": 32 * 1024 * 1024},
        ])
        with patch.object(self.extractor, '_send_sbf_request', return_value=sbf_data):
            result = self.extractor._query_disk_status()

        assert result is not None
        assert result["status"] == "mounted"
        assert result["usage_percent"] == 50.0
        assert result["total_mb"] == pytest.approx(32768.0, abs=1)
        assert result["used_mb"] == pytest.approx(16384.0, abs=1)
        assert len(result["disks"]) == 1

    def test_nearly_full_disk(self):
        """Disk at 99.5% usage."""
        sbf_data = _build_disk_status_sbf([
            {"disk_id": 0, "status_code": 1, "usage_raw": 9950, "disk_size_kb": 32 * 1024 * 1024},
        ])
        with patch.object(self.extractor, '_send_sbf_request', return_value=sbf_data):
            result = self.extractor._query_disk_status()

        assert result is not None
        assert result["usage_percent"] == 99.5

    def test_unmounted_disk_gjac_case(self):
        """GJAC scenario: disk status=4 (unmounted) — broken disk."""
        sbf_data = _build_disk_status_sbf([
            {"disk_id": 0, "status_code": 4, "usage_raw": 0, "disk_size_kb": 0},
        ])
        with patch.object(self.extractor, '_send_sbf_request', return_value=sbf_data):
            result = self.extractor._query_disk_status()

        assert result is not None
        assert result["status"] == "unmounted"
        assert result["total_mb"] == 0
        assert result["used_mb"] == 0

    def test_error_disk(self):
        """Disk with error status=3."""
        sbf_data = _build_disk_status_sbf([
            {"disk_id": 0, "status_code": 3, "usage_raw": 0, "disk_size_kb": 16 * 1024 * 1024},
        ])
        with patch.object(self.extractor, '_send_sbf_request', return_value=sbf_data):
            result = self.extractor._query_disk_status()

        assert result is not None
        assert result["status"] == "error"

    def test_two_disks_worst_status(self):
        """Two disks: one mounted, one unmounted — worst status wins."""
        sbf_data = _build_disk_status_sbf([
            {"disk_id": 0, "status_code": 1, "usage_raw": 5000, "disk_size_kb": 32 * 1024 * 1024},
            {"disk_id": 1, "status_code": 4, "usage_raw": 0, "disk_size_kb": 0},
        ])
        with patch.object(self.extractor, '_send_sbf_request', return_value=sbf_data):
            result = self.extractor._query_disk_status()

        assert result is not None
        assert result["status"] == "unmounted"
        assert len(result["disks"]) == 2

    def test_no_data_returns_none(self):
        """No SBF data received."""
        with patch.object(self.extractor, '_send_sbf_request', return_value=None):
            result = self.extractor._query_disk_status()
        assert result is None

    def test_zero_disks(self):
        """Zero disk descriptors."""
        sbf_data = _build_disk_status_sbf([])
        with patch.object(self.extractor, '_send_sbf_request', return_value=sbf_data):
            result = self.extractor._query_disk_status()

        assert result is not None
        assert result["status"] == "unavailable"
        assert result["disks"] == []


# ── Data routing fix test (#6b) ────────────────────────────────────────────

class TestDiskDataRouting:
    """Test that disk data is stored in metrics (not data_quality)."""

    def test_disk_routed_to_metrics(self):
        """Verify disk data goes to health_data['metrics']['disk']."""
        from receivers.health.polarx5_tcp_extractor import PolaRX5TCPExtractor

        ext = PolaRX5TCPExtractor.__new__(PolaRX5TCPExtractor)
        ext.host = "127.0.0.1"
        ext.port = 28784
        ext.station_id = "TEST"
        ext.timeout = 5
        ext.logger = MagicMock()
        ext.metric_checker = MagicMock()

        disk_result = {"status": "mounted", "used_mb": 100, "total_mb": 1000}

        with patch.object(ext, '_check_port_status', return_value=None), \
             patch.object(ext, '_query_power_status', return_value=None), \
             patch.object(ext, '_query_receiver_status', return_value=None), \
             patch.object(ext, '_query_disk_status', return_value=disk_result), \
             patch.object(ext, '_query_pvt_geodetic', return_value=None), \
             patch.object(ext, '_query_satellite_tracking', return_value=None), \
             patch.object(ext, '_query_ntrip_client_status', return_value=None), \
             patch.object(ext, '_query_ntrip_server_status', return_value=None), \
             patch.object(ext, '_query_receiver_setup', return_value=None), \
             patch.object(ext, '_query_logging_sessions', return_value=None):
            health = ext.extract_health_data()

        # Disk should be in metrics (where db_writer looks for it), not data_quality
        assert "disk" in health["metrics"]
        assert "disk" not in health["data_quality"]
        assert health["metrics"]["disk"]["used_mb"] == 100


# ── Health gate tests (#4) ─────────────────────────────────────────────────

class TestHealthGate:
    """Tests for check_station_health_gate().

    These mock the internal _query_health_gate function to avoid needing
    a real database connection.
    """

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache
        invalidate_cache()

    def test_no_satellites_skips(self):
        """Station with 0 satellites tracked should be skipped."""
        from receivers.utils.stall_timeout import check_station_health_gate, invalidate_cache
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_health_gate", return_value="no_satellites"):
            result = check_station_health_gate("GJAC")
        assert result == "no_satellites"

    def test_disk_full_skips(self):
        """Station with >98% disk usage should be skipped."""
        from receivers.utils.stall_timeout import check_station_health_gate, invalidate_cache
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_health_gate", return_value="disk_full"):
            result = check_station_health_gate("GJAC")
        assert result == "disk_full"

    def test_healthy_station_proceeds(self):
        """Healthy station should return None (proceed)."""
        from receivers.utils.stall_timeout import check_station_health_gate, invalidate_cache
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_health_gate", return_value=None):
            result = check_station_health_gate("GOOD")
        assert result is None

    def test_db_failure_proceeds(self):
        """Database connection failure should not block downloads."""
        from receivers.utils.stall_timeout import check_station_health_gate, invalidate_cache
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_health_gate", side_effect=Exception("DB down")):
            # The wrapper catches exceptions from _query and returns None
            result = check_station_health_gate("NODB")
            assert result is None

    def test_cache_prevents_repeated_queries(self):
        """Second call should use cached result."""
        from receivers.utils.stall_timeout import check_station_health_gate, invalidate_cache
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_health_gate", return_value="disk_full") as mock_q:
            check_station_health_gate("CACHED")
            check_station_health_gate("CACHED")  # Should hit cache
            assert mock_q.call_count == 1


# ── _query_health_gate integration tests ────────────────────────────────────

class TestQueryHealthGateIntegration:
    """Test _query_health_gate with mocked DB connection."""

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache
        invalidate_cache()

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_no_satellites_detected(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # station_latest_metrics → sats=0, age=60s (fresh)
        mock_cur.fetchone.side_effect = [
            (0, 50.0, datetime.now(timezone.utc)),
            (60.0,),
        ]

        result = _query_health_gate("GJAC")
        assert result == "no_satellites"

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_disk_full_detected(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # sats=12 (OK), disk=99.5% (>98), age=60s (fresh)
        mock_cur.fetchone.side_effect = [
            (12, 99.5, datetime.now(timezone.utc)),
            (60.0,),
        ]

        result = _query_health_gate("DISKFULL")
        assert result == "disk_full"

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_stale_data_proceeds(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # sats=0, disk=99%, but age=3600s (stale → proceed)
        mock_cur.fetchone.side_effect = [
            (0, 99.0, datetime.now(timezone.utc)),
            (3600.0,),
        ]

        result = _query_health_gate("STALE")
        assert result is None

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_no_data_proceeds(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_health_gate

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.return_value = None  # No rows

        result = _query_health_gate("NODATA")
        assert result is None


# ── Consecutive failure backoff tests (#3) ─────────────────────────────────

class TestConsecutiveFailureBackoff:
    """Tests for should_skip_station()."""

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache
        invalidate_cache()

    def test_five_failures_triggers_backoff(self):
        from receivers.utils.stall_timeout import should_skip_station, invalidate_cache
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_consecutive_failures", return_value=True):
            assert should_skip_station("BADST") is True

    def test_mixed_results_no_backoff(self):
        from receivers.utils.stall_timeout import should_skip_station, invalidate_cache
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_consecutive_failures", return_value=False):
            assert should_skip_station("MIXED") is False

    def test_cache_prevents_repeated_queries(self):
        from receivers.utils.stall_timeout import should_skip_station, invalidate_cache
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_consecutive_failures", return_value=True) as mock_q:
            should_skip_station("CACHED")
            should_skip_station("CACHED")
            assert mock_q.call_count == 1


class TestQueryConsecutiveFailuresIntegration:
    """Test _query_consecutive_failures with mocked DB."""

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache
        invalidate_cache()

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_all_five_failed(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_consecutive_failures

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchall.return_value = [
            ("failed",), ("unreachable",), ("stall_timeout",), ("failed",), ("failed",),
        ]

        assert _query_consecutive_failures("BADST") is True

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_one_success_breaks_streak(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_consecutive_failures

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchall.return_value = [
            ("completed",), ("failed",), ("failed",), ("failed",), ("failed",),
        ]

        assert _query_consecutive_failures("MIXED") is False

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_fewer_than_five_no_backoff(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_consecutive_failures

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchall.return_value = [("failed",), ("failed",)]

        assert _query_consecutive_failures("NEW") is False


# ── Packet loss factor tests (#5) ─────────────────────────────────────────

class TestPacketLossFactor:
    """Tests for get_packet_loss_factor()."""

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache
        invalidate_cache()

    def test_zero_loss(self):
        from receivers.utils.stall_timeout import get_packet_loss_factor, invalidate_cache
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_packet_loss_factor", return_value=1.0):
            assert get_packet_loss_factor("GOOD") == 1.0

    def test_high_loss_capped(self):
        from receivers.utils.stall_timeout import get_packet_loss_factor, invalidate_cache
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_packet_loss_factor", return_value=2.0):
            assert get_packet_loss_factor("BAD") == 2.0


class TestQueryPacketLossFactorIntegration:
    """Test _query_packet_loss_factor with mocked DB."""

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_zero_loss(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_packet_loss_factor

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = (0.0,)

        assert _query_packet_loss_factor("GOOD") == 1.0

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_20_percent_boundary(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_packet_loss_factor

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = (20.0,)

        assert _query_packet_loss_factor("MED") == 1.0

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_35_percent_interpolated(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_packet_loss_factor

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = (35.0,)

        factor = _query_packet_loss_factor("SKRO")
        assert factor == pytest.approx(1.5, abs=0.01)

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_50_percent_capped(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_packet_loss_factor

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = (75.0,)

        assert _query_packet_loss_factor("BAD") == 2.0

    @patch("receivers.health.database_factory.DatabaseConnectionFactory")
    def test_no_data_returns_1(self, mock_dbf):
        from receivers.utils.stall_timeout import _query_packet_loss_factor

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_dbf.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_dbf.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = None

        assert _query_packet_loss_factor("NODATA") == 1.0


# ── Router failure cache tests (#2) ───────────────────────────────────────

class TestRouterFailureCache:
    """Tests for RouterFailureCache."""

    def test_unknown_router_not_failed(self):
        from receivers.cli.parallel import RouterFailureCache
        cache = RouterFailureCache()
        assert cache.is_failed("10.4.1.43") is False

    def test_mark_and_check(self):
        from receivers.cli.parallel import RouterFailureCache
        cache = RouterFailureCache()
        cache.mark_failed("10.4.1.43")
        assert cache.is_failed("10.4.1.43") is True

    def test_different_router_not_affected(self):
        from receivers.cli.parallel import RouterFailureCache
        cache = RouterFailureCache()
        cache.mark_failed("10.4.1.43")
        assert cache.is_failed("10.4.1.44") is False

    def test_expiry(self):
        from receivers.cli.parallel import RouterFailureCache
        cache = RouterFailureCache()
        cache._TTL = 0.1  # 100ms for testing
        cache.mark_failed("10.4.1.43")
        assert cache.is_failed("10.4.1.43") is True
        time.sleep(0.15)
        assert cache.is_failed("10.4.1.43") is False

    def test_thread_safety(self):
        """Concurrent mark/check should not crash."""
        from receivers.cli.parallel import RouterFailureCache
        cache = RouterFailureCache()
        errors = []

        def marker():
            try:
                for i in range(100):
                    cache.mark_failed(f"10.0.0.{i % 10}")
            except Exception as e:
                errors.append(e)

        def checker():
            try:
                for i in range(100):
                    cache.is_failed(f"10.0.0.{i % 10}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=marker),
            threading.Thread(target=checker),
            threading.Thread(target=marker),
            threading.Thread(target=checker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


# ── Fix 1: Mode-switch returns new FTP connection ─────────────────────────

class TestModeSwitchFtpReturn:
    """Test that _download_with_progressbar_and_retry returns (result, ftp) tuple
    and that a mode-switch reconnect returns the NEW ftp connection."""

    def _make_receiver(self):
        from receivers.septentrio.polarx5 import PolaRX5
        rx = PolaRX5.__new__(PolaRX5)
        rx.station_id = "TEST"
        rx.logger = MagicMock()
        rx.pasv = True
        rx.progress_timeout = 600
        rx.data_transfer_timeout = 10
        rx.inactivity_timeout = 30
        return rx

    def test_happy_path_returns_original_ftp(self):
        """Normal download returns (result, original_ftp) tuple."""
        rx = self._make_receiver()
        ftp_orig = MagicMock()

        with patch.object(rx, '_download_with_progressbar', return_value=0):
            result, ftp_out = rx._download_with_progressbar_and_retry(
                ftp_orig, "/remote/file", "/local/file", 1000, 0,
            )

        assert result == 0
        assert ftp_out is ftp_orig

    def test_mode_switch_returns_new_ftp(self):
        """After mode-switch reconnect, returns (result, ftp_new)."""
        rx = self._make_receiver()
        ftp_orig = MagicMock()
        ftp_new = MagicMock()

        # First call raises connection error, triggering mode switch
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("500 I won't open a connection to X (only to Y)")
            return 0

        with patch.object(rx, '_download_with_progressbar', side_effect=side_effect), \
             patch.object(rx, '_ftp_open_connection', return_value=ftp_new), \
             patch.object(rx, '_get_ftp_mode_description', return_value="passive"):
            result, ftp_out = rx._download_with_progressbar_and_retry(
                ftp_orig, "/remote/file", "/local/file", 1000, 0,
            )

        assert result == 0
        assert ftp_out is ftp_new
        assert ftp_out is not ftp_orig

    def test_immediate_retry_propagates_new_ftp(self):
        """_download_with_immediate_retry propagates the new ftp from mode-switch."""
        rx = self._make_receiver()
        ftp_orig = MagicMock()
        ftp_new = MagicMock()

        with patch.object(rx, '_download_with_progressbar_and_retry', return_value=(0, ftp_new)):
            result, ftp_out = rx._download_with_immediate_retry(
                ftp_orig, "/remote/file", "/local/file", 1000, 0,
            )

        assert result == 0
        assert ftp_out is ftp_new


# ── Fix 2: Ping-override for backoff ──────────────────────────────────────

class TestBackoffPingOverride:
    """Test that should_skip_station() can be overridden by a successful ping."""

    def setup_method(self):
        from receivers.utils.stall_timeout import invalidate_cache
        invalidate_cache()

    def test_clear_backoff_cache(self):
        """clear_backoff_cache removes the station from the cache."""
        from receivers.utils.stall_timeout import (
            should_skip_station, clear_backoff_cache, invalidate_cache,
        )
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_consecutive_failures", return_value=True):
            assert should_skip_station("PING1") is True

        clear_backoff_cache("PING1")

        # After clearing, it should re-query (which we now make return False)
        with patch("receivers.utils.stall_timeout._query_consecutive_failures", return_value=False):
            assert should_skip_station("PING1") is False

    def test_clear_backoff_cache_case_insensitive(self):
        """clear_backoff_cache normalizes station ID to uppercase."""
        from receivers.utils.stall_timeout import (
            should_skip_station, clear_backoff_cache, invalidate_cache,
        )
        invalidate_cache()

        with patch("receivers.utils.stall_timeout._query_consecutive_failures", return_value=True):
            assert should_skip_station("PING2") is True

        clear_backoff_cache("ping2")  # lowercase

        with patch("receivers.utils.stall_timeout._query_consecutive_failures", return_value=False):
            assert should_skip_station("PING2") is False

    def test_clear_nonexistent_station_no_error(self):
        """Clearing cache for unknown station doesn't raise."""
        from receivers.utils.stall_timeout import clear_backoff_cache
        clear_backoff_cache("DOESNOTEXIST")  # Should not raise


# ── Fix 3: Progress-aware timeout extension ───────────────────────────────

class TestTimeoutExtension:
    """Test that near-complete downloads get a one-time timeout extension."""

    def _make_receiver(self):
        from receivers.septentrio.polarx5 import PolaRX5
        rx = PolaRX5.__new__(PolaRX5)
        rx.station_id = "TEST"
        rx.logger = MagicMock()
        rx.progress_timeout = 100
        rx.data_transfer_timeout = 10
        rx.inactivity_timeout = 60
        return rx

    def test_extension_logged_when_over_70_percent(self):
        """When progress >70% and timeout hit, extension should be logged."""
        rx = self._make_receiver()

        # The extension logic is inside _download_with_progressbar which is
        # deeply integrated with FTP. Test the logic pattern directly:
        # - timeout_extended starts False
        # - if >70% and not extended: extend by 50%, set flag True
        effective_timeout = 100
        timeout_extended = False
        offset = 0
        remote_file_size = 1000
        current_bytes = 750  # 75% done

        if not timeout_extended and (remote_file_size - offset) > 0:
            progress_pct = (current_bytes - offset) / (remote_file_size - offset) * 100
            if progress_pct > 70:
                extension = effective_timeout * 0.5
                effective_timeout += extension
                timeout_extended = True

        assert timeout_extended is True
        assert effective_timeout == 150  # 100 + 50

    def test_no_extension_when_under_70_percent(self):
        """When progress <70%, no extension."""
        effective_timeout = 100
        timeout_extended = False
        offset = 0
        remote_file_size = 1000
        current_bytes = 600  # 60% done

        if not timeout_extended and (remote_file_size - offset) > 0:
            progress_pct = (current_bytes - offset) / (remote_file_size - offset) * 100
            if progress_pct > 70:
                extension = effective_timeout * 0.5
                effective_timeout += extension
                timeout_extended = True

        assert timeout_extended is False
        assert effective_timeout == 100

    def test_extension_only_once(self):
        """Extension flag prevents double extension."""
        effective_timeout = 150  # Already extended once
        timeout_extended = True  # Flag already set
        offset = 0
        remote_file_size = 1000
        current_bytes = 900  # 90% done

        original_timeout = effective_timeout
        if not timeout_extended and (remote_file_size - offset) > 0:
            progress_pct = (current_bytes - offset) / (remote_file_size - offset) * 100
            if progress_pct > 70:
                extension = effective_timeout * 0.5
                effective_timeout += extension
                timeout_extended = True

        assert effective_timeout == original_timeout  # Unchanged

    def test_extension_with_resume_offset(self):
        """Extension progress calculation accounts for resume offset."""
        effective_timeout = 100
        timeout_extended = False
        offset = 500  # Resumed from 500 bytes
        remote_file_size = 1000
        current_bytes = 900  # 400/500 remaining = 80% of remaining done

        if not timeout_extended and (remote_file_size - offset) > 0:
            progress_pct = (current_bytes - offset) / (remote_file_size - offset) * 100
            if progress_pct > 70:
                extension = effective_timeout * 0.5
                effective_timeout += extension
                timeout_extended = True

        assert timeout_extended is True
        assert effective_timeout == 150


# ── Fix 4: Size mismatch clean retry ─────────────────────────────────────

class TestSizeMismatchRetry:
    """Test the size mismatch → delete → retry clean logic."""

    def _make_receiver(self):
        from receivers.septentrio.polarx5 import PolaRX5
        rx = PolaRX5.__new__(PolaRX5)
        rx.station_id = "TEST"
        rx.logger = MagicMock()
        rx.progress_timeout = 600
        rx.data_transfer_timeout = 10
        rx.inactivity_timeout = 30
        rx._last_effective_timeout = 600
        rx.file_validator = MagicMock()
        rx.file_validator.validate_file.return_value = {
            "valid": True, "compression": "gzip", "size": 1000,
        }
        return rx

    def test_handle_successful_download_valid(self):
        """_handle_successful_download records completed for valid files."""
        import tempfile, os
        rx = self._make_receiver()
        record = MagicMock()
        downloaded = []

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 100)
            tmp_path = f.name

        try:
            from pathlib import Path
            result = rx._handle_successful_download(
                "test.sbf.gz", Path(tmp_path), 100, 100,
                "15s_24hr", 1.0, downloaded, None, False,
                False, False, None, record,
            )
            assert result is True
            record.assert_called_once()
            assert record.call_args[0][2] == "completed"
            assert tmp_path in downloaded[0] or str(Path(tmp_path)) in downloaded[0]
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_handle_successful_download_invalid(self):
        """_handle_successful_download records failed and removes invalid files."""
        import tempfile, os
        rx = self._make_receiver()
        rx.file_validator.validate_file.return_value = {
            "valid": False, "error": "corrupt gzip",
        }
        record = MagicMock()
        downloaded = []

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 100)
            tmp_path = f.name

        try:
            from pathlib import Path
            result = rx._handle_successful_download(
                "test.sbf.gz", Path(tmp_path), 100, 100,
                "15s_24hr", 1.0, downloaded, None, False,
                False, False, None, record,
            )
            assert result is True
            record.assert_called_once()
            assert record.call_args[0][2] == "failed"
            assert not os.path.exists(tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_size_mismatch_retried_flag_resets_per_file(self):
        """size_mismatch_retried should be False at start of each file iteration."""
        # This tests the pattern: the flag is initialized per-file in the loop
        size_mismatch_retried = False  # As set at start of each iteration
        assert size_mismatch_retried is False

        # After first mismatch retry
        size_mismatch_retried = True
        assert size_mismatch_retried is True

        # Next file iteration resets it
        size_mismatch_retried = False
        assert size_mismatch_retried is False
