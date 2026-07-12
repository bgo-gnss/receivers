"""General date-token vocabulary — resolver + its archive/TOS providers.

Covers :mod:`receivers.utils.date_vocab` (literal + token + ±N offset resolution,
missing-provider errors) and the two backing providers:
``engine.archive_date_bounds`` (first/last) and
``tos_access.device_session_bounds`` (device_start/device_end).
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from receivers.dissemination.engine import _date_from_rinex_name, archive_date_bounds
from receivers.dissemination.tos_access import device_session_bounds
from receivers.utils.date_vocab import (
    DateContext,
    DateVocabError,
    resolve_date,
    tokens,
)

_TODAY = date(2026, 7, 12)


def _ctx(**over):
    base = dict(
        role="start",
        today=_TODAY,
        station="NYLA",
        device_serial=None,
        archive_bounds=lambda: (date(2006, 7, 27), date(2026, 7, 11)),
        device_bounds=lambda sn: (
            (date(2019, 4, 6), date(2022, 7, 21)) if sn == "3071033" else (None, None)
        ),
    )
    base.update(over)
    return DateContext(**base)


class TestResolveLiteral:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2026-01-15", date(2026, 1, 15)),
            ("20260115", date(2026, 1, 15)),
            ("2026-01-01+14", date(2026, 1, 15)),  # literal + offset
            ("20260201-1", date(2026, 1, 31)),  # literal - offset, month rollover
        ],
    )
    def test_literals_and_offsets(self, value, expected):
        assert resolve_date(value, _ctx()) == expected

    def test_hyphens_in_date_are_not_an_offset(self):
        # A bare ISO date must not have its own hyphens read as a ±N offset.
        assert resolve_date("2026-07-03", _ctx()) == date(2026, 7, 3)


class TestResolveTokens:
    def test_today_yesterday(self):
        assert resolve_date("today", _ctx()) == _TODAY
        assert resolve_date("yesterday", _ctx()) == date(2026, 7, 11)

    def test_first_last_from_archive(self):
        assert resolve_date("first", _ctx()) == date(2006, 7, 27)
        assert resolve_date("last", _ctx()) == date(2026, 7, 11)

    def test_token_offsets(self):
        assert resolve_date("first+7", _ctx()) == date(2006, 8, 3)
        assert resolve_date("last-1", _ctx()) == date(2026, 7, 10)
        assert resolve_date("today-30", _ctx()) == date(2026, 6, 12)

    def test_device_start_end(self):
        ctx = _ctx(device_serial="3071033")
        assert resolve_date("device_start", ctx) == date(2019, 4, 6)
        assert resolve_date("device_end", ctx) == date(2022, 7, 21)

    def test_device_end_open_session_falls_back_to_archive_latest(self):
        # Open device session (end None) → the latest archived day, not a crash.
        ctx = _ctx(device_bounds=lambda sn: (date(2023, 2, 9), None))
        ctx.device_serial = "3075127"
        assert resolve_date("device_end", ctx) == date(2026, 7, 11)

    def test_case_insensitive(self):
        assert resolve_date("FIRST", _ctx()) == date(2006, 7, 27)


class TestResolveErrors:
    def test_unknown_token(self):
        with pytest.raises(DateVocabError):
            resolve_date("bogus", _ctx())

    def test_first_without_archive_context(self):
        with pytest.raises(DateVocabError, match="archive"):
            resolve_date("first", _ctx(station=None, archive_bounds=None))

    def test_first_with_empty_archive(self):
        with pytest.raises(DateVocabError, match="no archived files"):
            resolve_date("first", _ctx(archive_bounds=lambda: (None, None)))

    def test_device_without_serial(self):
        with pytest.raises(DateVocabError, match="--device"):
            resolve_date("device_start", _ctx(device_serial=None))

    def test_device_serial_not_in_tos(self):
        with pytest.raises(DateVocabError, match="no TOS device session"):
            resolve_date("device_start", _ctx(device_serial="0000"))

    def test_empty_value(self):
        with pytest.raises(DateVocabError):
            resolve_date("   ", _ctx())

    def test_tokens_listed(self):
        assert {
            "first",
            "last",
            "device_start",
            "device_end",
            "today",
            "yesterday",
        } <= set(tokens())


class TestArchiveDateBounds:
    def _seed(self, root, station, entries):
        for year, mon, doy, ext in entries:
            d = root / f"{year}" / mon / station / "15s_24hr" / "rinex"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{station}{doy:03d}0.{ext}").write_bytes(b"x")

    def test_earliest_latest_across_years(self, tmp_path):
        self._seed(
            tmp_path,
            "NYLA",
            [
                (2006, "jul", 208, "06d.Z"),
                (2019, "apr", 96, "19d.Z"),
                (2026, "jul", 192, "26d.Z"),
            ],
        )
        lo, hi = archive_date_bounds(tmp_path, ("15s_24hr",), "NYLA")
        assert lo == date(2006, 7, 27)
        assert hi == date(2026, 7, 11)

    def test_long_name_epoch(self, tmp_path):
        d = tmp_path / "2026" / "may" / "FIM2" / "15s_24hr" / "rinex"
        d.mkdir(parents=True)
        (d / "FIM200ISL_R_20261280000_01D_15S_MO.crx.gz").write_bytes(b"x")
        lo, hi = archive_date_bounds(tmp_path, ("15s_24hr",), "FIM2")
        assert lo == hi == date(2026, 5, 8)

    def test_missing_station_is_none(self, tmp_path):
        assert archive_date_bounds(tmp_path, ("15s_24hr",), "ZZZZ") == (None, None)

    def test_missing_root_is_none(self, tmp_path):
        assert archive_date_bounds(tmp_path / "nope", ("15s_24hr",), "NYLA") == (
            None,
            None,
        )

    @pytest.mark.parametrize(
        "name,year,expected",
        [
            ("RHOF0270.06d.Z", 2006, date(2006, 1, 27)),
            ("RHOF00ISL_R_20261280000_01D_15S_MO.crx.gz", 2099, date(2026, 5, 8)),
            ("README.txt", 2020, None),
            ("RHOF9990.06d", 2006, None),  # doy 999 out of range
        ],
    )
    def test_date_from_rinex_name(self, name, year, expected):
        assert _date_from_rinex_name(name, year) == expected


class TestDeviceSessionBounds:
    _HIST = [
        {
            "gnss_receiver": {"serial_number": "4539258413"},
            "time_from": datetime(2006, 7, 27),
            "time_to": datetime(2019, 4, 6),
        },
        {
            "gnss_receiver": {"serial_number": "4539258413"},
            "time_from": datetime(2019, 4, 6),
            "time_to": datetime(2022, 7, 22),
        },
        {
            "gnss_receiver": {"serial_number": "3075127"},
            "time_from": datetime(2023, 2, 9),
            "time_to": None,
        },
    ]

    def test_closed_session_is_inclusive_end(self):
        # Half-open [from, to) → last owned day is to − 1 (handover belongs to next).
        assert device_session_bounds(self._HIST, "4539258413") == (
            date(2006, 7, 27),
            date(2022, 7, 21),
        )

    def test_open_session_end_is_none(self):
        assert device_session_bounds(self._HIST, "3075127") == (date(2023, 2, 9), None)

    def test_missing_serial(self):
        assert device_session_bounds(self._HIST, "9999") == (None, None)

    def test_iso_string_dates(self):
        hist = [
            {
                "gnss_receiver": {"serial_number": "X1"},
                "time_from": "2020-01-01T00:00:00",
                "time_to": "2021-01-01T00:00:00Z",
            }
        ]
        assert device_session_bounds(hist, "X1") == (
            date(2020, 1, 1),
            date(2020, 12, 31),
        )


class TestCliResolveIntegration:
    """The epos-disseminate CLI resolver wires station → archive walk and the
    --device serial → TOS bounds through one real DateContext."""

    def _target(self, root):
        from types import SimpleNamespace

        return SimpleNamespace(source_root=str(root), sessions=("15s_24hr",))

    def _args(self, **over):
        from types import SimpleNamespace

        base = dict(station="NYLA", device=None)
        base.update(over)
        return SimpleNamespace(**base)

    def test_first_and_last_walk_the_archive(self, tmp_path):
        from receivers.cli.epos_disseminate import _resolve_cli_date

        for year, mon, doy in [(2006, "jul", 208), (2026, "jul", 192)]:
            d = tmp_path / f"{year}" / mon / "NYLA" / "15s_24hr" / "rinex"
            d.mkdir(parents=True)
            (d / f"NYLA{doy:03d}0.{year % 100:02d}d.Z").write_bytes(b"x")
        tgt = self._target(tmp_path)
        args = self._args()
        assert _resolve_cli_date("first", "start", args, tgt) == date(2006, 7, 27)
        assert _resolve_cli_date("last", "end", args, tgt) == date(2026, 7, 11)
        # Offset on a token flows through the CLI resolver too.
        assert _resolve_cli_date("first+1", "start", args, tgt) == date(2006, 7, 28)

    def test_device_tokens_use_tos_bounds(self, tmp_path, monkeypatch):
        import receivers.cli.epos_disseminate as cli

        class _FakeCache:
            def get_metadata(self, station):
                return {
                    "device_history": [
                        {
                            "gnss_receiver": {"serial_number": "3071033"},
                            "time_from": datetime(2019, 4, 6),
                            "time_to": datetime(2022, 7, 22),
                        }
                    ]
                }

        monkeypatch.setattr(
            "receivers.dissemination.tos_access.TOSSesionCache", _FakeCache
        )
        tgt = self._target(tmp_path)
        args = self._args(device="3071033")
        assert cli._resolve_cli_date("device_start", "start", args, tgt) == date(
            2019, 4, 6
        )
        assert cli._resolve_cli_date("device_end", "end", args, tgt) == date(
            2022, 7, 21
        )

    def test_literal_still_works(self, tmp_path):
        from receivers.cli.epos_disseminate import _resolve_cli_date

        args = self._args()
        tgt = self._target(tmp_path)
        assert _resolve_cli_date("2026-05-08", "date", args, tgt) == date(2026, 5, 8)
