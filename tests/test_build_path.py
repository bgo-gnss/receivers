"""Regression test for BaseReceiver.build_path with a zero-length range.

`download STATION -s D -e D --session 1Hz_1hr` makes start == end for an hourly
session, so the local datelist comes out empty. Previously build_path then called
gtimes.datepathlist(datelist=[]) which compared `None <= end_time` and raised
`TypeError: '<=' not supported between 'NoneType' and 'datetime.datetime'`,
crashing the download for every NetR9/G10 station (PolaRX5 filenames happened to
dodge it). build_path must instead return [] for an empty range.
"""

from __future__ import annotations

from datetime import datetime

from receivers.base.receiver import BaseReceiver


class _StubReceiver(BaseReceiver):
    """Concrete BaseReceiver with stubbed abstract methods (no real I/O)."""

    def get_connection_status(self):
        return {}

    def download_data(self, *args, **kwargs):
        return {}

    def get_health_status(self):
        return {}

    def get_station_info(self):
        return {}

    def get_file_extension(self):
        return ".sbf"

    def get_session_letter(self, session):
        return "a"


def _bare_receiver():
    # __new__ bypasses the config-loading __init__; build_path needs no config
    # state for a placeholder-free template.
    rx = _StubReceiver.__new__(_StubReceiver)
    rx.station_id = "TEST"
    return rx


_TMPL = "data/%Y/%m/file%j%H.sbf"  # no {station}/{session} placeholders


def test_build_path_zero_length_hourly_range_returns_empty():
    rx = _bare_receiver()
    same = datetime(2026, 6, 7)
    # start == end for an hourly session must not raise — just no periods.
    assert (
        rx.build_path(None, _TMPL, "1Hz_1hr", "1H", start_time=same, end_time=same)
        == []
    )


def test_build_path_zero_length_daily_range_returns_empty():
    rx = _bare_receiver()
    same = datetime(2026, 6, 7)
    assert (
        rx.build_path(None, _TMPL, "15s_24hr", "1D", start_time=same, end_time=same)
        == []
    )


def test_build_path_normal_hourly_range_still_yields_entries():
    rx = _bare_receiver()
    start = datetime(2026, 6, 7, 0)
    end = datetime(2026, 6, 7, 2)  # 00:00 and 01:00 (end exclusive) → 2 periods
    result = rx.build_path(None, _TMPL, "1Hz_1hr", "1H", start_time=start, end_time=end)
    assert len(result) == 2
