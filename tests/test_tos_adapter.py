"""Tests for receivers.cfg.tos_adapter's station-lifecycle helpers.

station_date_end / has_open_receiver / current_lifecycle were added
2026-10-10 as the receivers-side half of Step 1 of the station_status/
TOS-inference architecture review (tostools' date_end passthrough is the
other half, in the sibling tostools repo). These are pure functions over
the station dict shape TOSClient.get_complete_station_metadata() returns
(device_history sessions + top-level attributes like date_end) -- no TOS
calls, no mocking needed.
"""

from __future__ import annotations

from receivers.cfg import tos_adapter


def _session(
    *,
    time_to=None,
    gnss_receiver=None,
):
    session = {"time_from": "2020-01-01T00:00:00", "time_to": time_to}
    if gnss_receiver is not None:
        session["gnss_receiver"] = gnss_receiver
    return session


def _station(*, date_end=None, sessions=None):
    station = {"device_history": sessions or []}
    if date_end is not None:
        station["date_end"] = date_end
    return station


# ---------------------------------------------------------------------------
# has_open_receiver
# ---------------------------------------------------------------------------


def test_has_open_receiver_true_when_current_session_has_one():
    station = _station(
        sessions=[
            _session(
                gnss_receiver={"model": "TRIMBLE NETRS", "serial_number": "4729135857"}
            )
        ]
    )
    assert tos_adapter.has_open_receiver(station) is True


def test_has_open_receiver_false_when_no_open_session():
    """Every session closed (time_to set) -- nothing current to poll."""
    station = _station(
        sessions=[
            _session(
                time_to="2024-01-01T00:00:00",
                gnss_receiver={"model": "X", "serial_number": "Y"},
            )
        ]
    )
    assert tos_adapter.has_open_receiver(station) is False


def test_has_open_receiver_false_when_open_session_has_no_receiver():
    """Open session exists (e.g. antenna/monument only) but no receiver
    attached -- a legitimate TOS state for a bare install stage."""
    station = _station(sessions=[_session(gnss_receiver=None)])
    assert tos_adapter.has_open_receiver(station) is False


def test_has_open_receiver_false_for_empty_device_history():
    assert tos_adapter.has_open_receiver(_station()) is False


# ---------------------------------------------------------------------------
# station_date_end
# ---------------------------------------------------------------------------


def test_station_date_end_present():
    station = _station(date_end="2024-06-01 00:00")
    assert tos_adapter.station_date_end(station) == "2024-06-01 00:00"


def test_station_date_end_none_when_absent():
    """Absence means 'TOS currently considers the station open/live' --
    NOT 'unknown'. A live station simply never has this key."""
    assert tos_adapter.station_date_end(_station()) is None


def test_station_date_end_none_when_empty_string():
    assert tos_adapter.station_date_end(_station(date_end="")) is None


# ---------------------------------------------------------------------------
# current_lifecycle
# ---------------------------------------------------------------------------


def test_current_lifecycle_discontinued_when_date_end_set():
    station = _station(date_end="2024-06-01", sessions=[])
    assert tos_adapter.current_lifecycle(station) == ("discontinued", False)


def test_current_lifecycle_active_when_open_receiver_and_no_date_end():
    station = _station(
        sessions=[_session(gnss_receiver={"model": "X", "serial_number": "Y"})]
    )
    assert tos_adapter.current_lifecycle(station) == ("active", False)


def test_current_lifecycle_inactive_when_no_open_receiver_and_no_date_end():
    station = _station(sessions=[])
    assert tos_adapter.current_lifecycle(station) == ("inactive", False)


def test_current_lifecycle_contradiction_date_end_with_open_receiver():
    """TOS hygiene violation: station entity closed (date_end set) but an
    open gnss_receiver join still exists. discontinued wins for the
    verdict, but contradiction=True so the caller flags it for review
    instead of silently picking a side."""
    station = _station(
        date_end="2024-06-01",
        sessions=[_session(gnss_receiver={"model": "X", "serial_number": "Y"})],
    )
    assert tos_adapter.current_lifecycle(station) == ("discontinued", True)


def test_current_lifecycle_no_contradiction_date_end_without_open_receiver():
    """The normal, non-contradictory discontinued case: closed entity,
    no live receiver either."""
    station = _station(date_end="2024-06-01", sessions=[])
    lifecycle, contradiction = tos_adapter.current_lifecycle(station)
    assert lifecycle == "discontinued"
    assert contradiction is False
