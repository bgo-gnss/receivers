"""Tests for `classify_download_exception` in the Trimble HTTP download path.

A download failure must only be promoted to `file_tracking.status='missing'`
when the receiver explicitly reported the file is not on disk (HTTP 404).
Connection failures, timeouts, and 5xx errors leave the file's state
unknown and must NOT be marked missing — otherwise a transient outage
during the midnight window would lock the file out of the morning_recovery
retry pass.

Driving incident: GJFV (2026-05-14) — the station's router was offline
00:00–01:08 UTC. The midnight HTTP download timed out 4× and wrote a
`status='missing'` row, even though the receiver did have the file. Once
the row was written, morning_recovery skipped the station, and the file
was lost for the day.
"""

from unittest.mock import Mock

import pytest
import requests

from receivers.trimble.http_download_client import classify_download_exception

# ─── classify_download_exception ───────────────────────────────────────────


def test_404_classified_as_not_found():
    """HTTP 404 from the receiver = file verified absent."""
    mock_response = Mock(status_code=404)
    exc = requests.exceptions.HTTPError(response=mock_response)
    assert classify_download_exception(exc) == "not_found"


def test_500_classified_as_transport_error():
    """5xx is server-side; receiver state is unknown — do NOT mark missing."""
    mock_response = Mock(status_code=500)
    exc = requests.exceptions.HTTPError(response=mock_response)
    assert classify_download_exception(exc) == "transport_error"


def test_503_classified_as_transport_error():
    """503 Service Unavailable — receiver state unknown."""
    mock_response = Mock(status_code=503)
    exc = requests.exceptions.HTTPError(response=mock_response)
    assert classify_download_exception(exc) == "transport_error"


def test_403_classified_as_transport_error():
    """403 Forbidden is an auth issue, not file absence."""
    mock_response = Mock(status_code=403)
    exc = requests.exceptions.HTTPError(response=mock_response)
    assert classify_download_exception(exc) == "transport_error"


def test_connect_timeout_classified_as_transport_error():
    """The GJFV case: receiver's router went offline → connect timeout."""
    exc = requests.exceptions.ConnectTimeout("Connection to 1.2.3.4:7000 timed out")
    assert classify_download_exception(exc) == "transport_error"


def test_read_timeout_classified_as_transport_error():
    """Receiver accepted the connection but didn't respond in time."""
    exc = requests.exceptions.ReadTimeout("HTTPConnectionPool: Read timed out")
    assert classify_download_exception(exc) == "transport_error"


def test_connection_error_classified_as_transport_error():
    """Connection refused / DNS failure / network unreachable."""
    exc = requests.exceptions.ConnectionError("Connection refused")
    assert classify_download_exception(exc) == "transport_error"


def test_generic_timeout_error_classified_as_transport_error():
    """Bare-Python TimeoutError (e.g. our progress-stall sentinel)."""
    exc = TimeoutError("Download stalled")
    assert classify_download_exception(exc) == "transport_error"


def test_generic_exception_classified_as_transport_error():
    """Unknown exception with no response attribute → safe default."""
    exc = RuntimeError("Some other failure")
    assert classify_download_exception(exc) == "transport_error"


def test_http_error_without_response_classified_as_transport_error():
    """Defensive: HTTPError instance with no response attribute."""
    exc = requests.exceptions.HTTPError("HTTP error, no response object")
    assert classify_download_exception(exc) == "transport_error"


def test_http_error_with_response_no_status_code():
    """Defensive: response object without status_code attribute."""
    exc = requests.exceptions.HTTPError(response=object())
    assert classify_download_exception(exc) == "transport_error"


@pytest.mark.parametrize(
    "status_code,expected",
    [
        (200, "transport_error"),  # raise_for_status wouldn't fire, but defensive
        (301, "transport_error"),  # redirect — shouldn't reach here either
        (400, "transport_error"),  # bad request, not absence
        (401, "transport_error"),  # auth, not absence
        (404, "not_found"),  # the only "verified absent" signal
        (410, "transport_error"),  # gone — receiver semantics ambiguous
        (500, "transport_error"),
        (502, "transport_error"),
        (504, "transport_error"),
    ],
)
def test_status_code_table(status_code, expected):
    """Exhaustive table to lock the policy: only 404 = not_found."""
    mock_response = Mock(status_code=status_code)
    exc = requests.exceptions.HTTPError(response=mock_response)
    assert classify_download_exception(exc) == expected
