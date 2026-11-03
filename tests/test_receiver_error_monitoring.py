"""ReceiverStatus2 error words reach the health metrics, the status and the DB.

ENTC (PolaRx5) lost its antenna feed to an overcurrent on 2026-11-02. The
receiver reported it — RxError bit 5 (ANTENNA) — but nothing read RxError or
ExtError, and ``block_receiver_status.rx_error`` / ``ext_error`` had never been
written for any station. The outage surfaced only as "0 satellites", 35 h late.

The fixture below is that real block, captured from ENTC after a reset.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from receivers.health.db_writer import HealthDatabaseWriter
from receivers.health.polarx5_tcp_extractor import (
    PolaRX5TCPExtractor,
    receiver_error_metric,
)

# ReceiverStatus2 (4014) from ENTC, 2026-11-03: RxError=32 (ANTENNA),
# RxState=640 (ACTIVEANTENNA clear), AGC gain ~60 dB on every front end.
ENTC_RECEIVER_STATUS2 = bytes.fromhex(
    "24400f35ae2f3800ffffffffffff10008b00000080020000200000000604017e"
    "003c5b00013e5900053c5d00063d6200033a6200043a5d00"
)


def _healthy(block: bytes) -> bytes:
    """The same block with RxError cleared (the decode does not check CRC)."""
    return block[:24] + (0).to_bytes(4, "little") + block[28:]


# --- the decode ------------------------------------------------------------


def test_receiver_error_metric_antenna_is_critical():
    m = receiver_error_metric(32, 0, rx_status=640)
    assert m["flags"] == ["ANTENNA"]
    assert m["status"] == "critical"
    assert m["active_antenna"] is False


def test_receiver_error_metric_clear_is_ok():
    m = receiver_error_metric(0, 0, rx_status=2802)
    assert (m["flags"], m["status"], m["active_antenna"]) == ([], "ok", True)


def test_other_and_undocumented_bits_warn_by_position():
    assert receiver_error_metric(1 << 9, 0)["flags"] == ["CPUOVERLOAD"]
    m = receiver_error_metric(1 << 20, 1 << 3)
    assert m["flags"] == ["BIT20", "SETUPERROR"]
    assert m["status"] == "warning"


def _extractor() -> PolaRX5TCPExtractor:
    return PolaRX5TCPExtractor("192.0.2.1", "ENTC", timeout=1)


def test_query_receiver_status_decodes_the_captured_entc_block():
    ex = _extractor()
    with patch.object(ex, "_send_sbf_request", return_value=ENTC_RECEIVER_STATUS2):
        r = ex._query_receiver_status()
    assert (r["rx_status"], r["rx_error"], r["ext_error"]) == (640, 32, 0)
    assert r["receiver_errors"]["status"] == "critical"
    assert r["uptime_seconds"] == 139


def test_extract_health_data_carries_the_error_words_into_metrics():
    """The wiring, not just the helper: extract_health_data must copy them."""
    ex = _extractor()
    quiet = {
        name: MagicMock(return_value=None)
        for name in dir(ex)
        if name.startswith("_query_") and name != "_query_receiver_status"
    }
    with (
        patch.object(ex, "_send_sbf_request", return_value=ENTC_RECEIVER_STATUS2),
        patch.object(ex, "_check_port_status", return_value={}),
        patch.multiple(ex, **quiet),
    ):
        metrics = ex.extract_health_data()["metrics"]
    assert metrics["rx_error"] == 32 and metrics["ext_error"] == 0
    assert metrics["receiver_errors"]["flags"] == ["ANTENNA"]
    assert metrics["receiver_errors"]["status"] == "critical"


def test_healthy_block_reports_ok():
    ex = _extractor()
    with patch.object(
        ex, "_send_sbf_request", return_value=_healthy(ENTC_RECEIVER_STATUS2)
    ):
        r = ex._query_receiver_status()
    assert r["rx_error"] == 0 and r["receiver_errors"]["status"] == "ok"


# --- the database ----------------------------------------------------------


class _Cursor:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


class _Conn:
    def __init__(self):
        self.calls = []

    @contextmanager
    def cursor(self):
        yield _Cursor(self.calls)


def _writer() -> HealthDatabaseWriter:
    w = HealthDatabaseWriter.__new__(HealthDatabaseWriter)
    w._conn = _Conn()
    return w


def test_write_receiver_status_stores_rx_error_and_ext_error():
    w = _writer()
    ex = _extractor()
    with patch.object(ex, "_send_sbf_request", return_value=ENTC_RECEIVER_STATUS2):
        metrics = ex._query_receiver_status()
    w._write_receiver_status("ENTC", "2026-11-03T12:00:00Z", metrics)
    ((sql, params),) = w._conn.calls
    assert "rx_error" in sql and "ext_error" in sql
    assert params[-3:] == (640, 32, 0)  # rx_status, rx_error, ext_error


def test_status_details_names_the_flag():
    w = _writer()
    metrics = {"receiver_errors": receiver_error_metric(32, 0, 640)}
    details = w._build_status_details(metrics, "critical")
    assert "Receiver error: ANTENNA" in details
