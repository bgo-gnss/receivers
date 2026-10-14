"""Tests for the archived 1 Hz -> 15 s daily backfill (stream-splice)."""

import gzip
from datetime import date
from pathlib import Path

import pytest

from receivers.streaming.downsample import DownsampleResult
from receivers.streaming.splice_backfill import (
    SpliceDay,
    build_day,
    classify_day,
    count_epochs_with_sats,
    plan_station,
    raw15_present,
)

DAY = date(2026, 7, 1)  # DOY 182
BNC = {"pgm": "BNC 2.12.7", "version": "3.01"}


def _hours(root: Path, n=24, sta="SEY9", day=DAY):
    d = root / "2026" / "jul" / sta / "1Hz_1hr" / "rinex"
    d.mkdir(parents=True, exist_ok=True)
    for h in range(n):
        (d / f"{sta}182{chr(97 + h)}.26D.Z").write_bytes(b"h")
    return d


def _daily(root: Path, sta="SEY9"):
    d = root / "2026" / "jul" / sta / "15s_24hr" / "rinex"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sta}1820.26D.Z"
    f.write_bytes(b"daily")
    return f


def _reader(daily_hdr, src_hdr=BNC):
    def read(path: Path):
        return src_hdr if "1Hz_1hr" in str(path) else daily_hdr

    return read


class TestClassify:
    def test_missing(self, tmp_path):
        _hours(tmp_path)
        p = classify_day(tmp_path, "SEY9", DAY, header_reader=_reader({}))
        assert p.reason == "missing" and p.build

    def test_truncated(self, tmp_path):
        """SEY9 182 of 2026: BNC daily ending 07:59 with 24 hours archived."""
        _hours(tmp_path)
        _daily(tmp_path)
        hdr = dict(BNC, last_obs="2026 7 1 7 59 45.0000000")
        p = classify_day(tmp_path, "SEY9", DAY, header_reader=_reader(hdr))
        assert p.reason == "truncated" and p.build
        assert p.daily is not None and p.last_source_hour == 23

    def test_honest_partial_is_not_truncated(self, tmp_path):
        """The hours themselves stop at 14:00 (SEY9 2021-281): nothing to add."""
        _hours(tmp_path, n=15)
        _daily(tmp_path)
        hdr = {"pgm": "BNC 2.12.7", "version": "2.11", "last_obs": "2026 7 1 14 32 0"}
        p = classify_day(tmp_path, "SEY9", DAY, header_reader=_reader(hdr))
        assert p.reason == "complete" and not p.build

    def test_complete_r3_is_converted_unless_disabled(self, tmp_path):
        _hours(tmp_path)
        _daily(tmp_path)
        hdr = dict(BNC, last_obs="2026 7 1 23 59 45.0")
        r = _reader(hdr)
        assert (
            classify_day(tmp_path, "SEY9", DAY, header_reader=r).reason == "stream_r3"
        )
        p = classify_day(tmp_path, "SEY9", DAY, convert_r3=False, header_reader=r)
        assert p.reason == "complete"

    @pytest.mark.parametrize("pgm", ["sbf2rin-16.2.0", "teqc  2019Feb25", None])
    def test_non_bnc_daily_is_authoritative(self, tmp_path, pgm):
        _hours(tmp_path)
        _daily(tmp_path)
        hdr = {"pgm": pgm} if pgm else {}
        p = classify_day(tmp_path, "SEY9", DAY, header_reader=_reader(hdr))
        assert p.reason == "authoritative" and not p.build

    def test_raw_present_means_rerinex(self, tmp_path):
        _hours(tmp_path)
        raw = tmp_path / "2026" / "jul" / "SEY9" / "15s_24hr" / "raw"
        raw.mkdir(parents=True)
        (raw / "SEY9202607010000a.sbf.gz").write_bytes(b"r")
        p = classify_day(tmp_path, "SEY9", DAY, header_reader=_reader({}))
        assert p.reason == "rerinex" and not p.build

    def test_downloaded_hours_are_not_spliced(self, tmp_path):
        _hours(tmp_path)
        r = _reader({}, src_hdr={"pgm": "sbf2rin-16.2.0"})
        p = classify_day(tmp_path, "SEY9", DAY, header_reader=r)
        assert p.reason == "non_stream_sources" and not p.build

    def test_raw15_present_short_name(self, tmp_path):
        raw = tmp_path / "2026" / "jul" / "SEY9" / "15s_24hr" / "raw"
        raw.mkdir(parents=True)
        (raw / "SEY91820.26_.gz").write_bytes(b"r")
        assert raw15_present(tmp_path, "SEY9", DAY)
        assert not raw15_present(tmp_path, "SEY9", date(2026, 7, 2))


class TestPlan:
    def test_live_window_and_inclusive_range(self, tmp_path):
        _hours(tmp_path)
        d = tmp_path / "2026" / "jul" / "SEY9" / "1Hz_1hr" / "rinex"
        (d / "SEY9183a.26D.Z").write_bytes(b"h")  # 07-02
        (d / "SEY9184a.26D.Z").write_bytes(b"h")  # 07-03
        r = _reader({})
        plans = plan_station(tmp_path, "SEY9", today=date(2026, 7, 3), header_reader=r)
        # 07-02 and 07-03 belong to the live cycle (today + days_back 1)
        assert [(p.day.day, p.reason) for p in plans] == [
            (1, "missing"),
            (2, "live_window"),
            (3, "live_window"),
        ]
        # -e is INCLUSIVE (unlike `rinex -e`)
        plans = plan_station(
            tmp_path,
            "SEY9",
            date(2026, 7, 1),
            date(2026, 7, 1),
            today=date(2026, 8, 1),
            header_reader=r,
        )
        assert [p.day for p in plans] == [DAY]


class FakeDownsampler:
    def __init__(self, status="created"):
        self.status = status
        self.calls = []

    def downsample_day(self, sta, sources, out, workdir, **kw):
        self.calls.append(kw)
        if self.status == "created":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"new")
        return DownsampleResult(sta, out, self.status, len(sources), error=None)


class TestBuild:
    def _plan(self, tmp_path, daily=None):
        return SpliceDay("SEY9", DAY, "truncated", [Path("x")], daily)

    def test_rebuild_always_forced_and_hook_passed(self, tmp_path):
        ds = FakeDownsampler()
        hook = object()
        o = build_day(
            self._plan(tmp_path),
            tmp_path,
            ds,
            header_hook=hook,
            epoch_counter=lambda p: 5,
        )
        assert o.ok and o.epochs == 5
        assert ds.calls[0]["skip_if_exists"] is False
        assert ds.calls[0]["header_hook"] is hook
        assert o.staged == tmp_path / "2026/jul/SEY9/15s_24hr/rinex/SEY91820.26D.Z"

    def test_refuses_zero_satellite_product(self, tmp_path):
        o = build_day(
            self._plan(tmp_path), tmp_path, FakeDownsampler(), epoch_counter=lambda p: 0
        )
        assert not o.ok and "satellite" in o.error
        assert not o.staged.exists()

    def test_refuses_replacement_with_fewer_epochs(self, tmp_path):
        old = _daily(tmp_path / "arch")
        counts = {old: 5674}
        o = build_day(
            self._plan(tmp_path, daily=old),
            tmp_path / "w",
            FakeDownsampler(),
            epoch_counter=lambda p: counts.get(p, 1896),
        )
        assert not o.ok and "fewer epochs" in o.error
        assert not o.staged.exists()

    def test_accepts_replacement_with_more_epochs(self, tmp_path):
        old = _daily(tmp_path / "arch")
        counts = {old: 1896}
        o = build_day(
            self._plan(tmp_path, daily=old),
            tmp_path / "w",
            FakeDownsampler(),
            epoch_counter=lambda p: counts.get(p, 5674),
        )
        assert o.ok and (o.epochs, o.old_epochs) == (5674, 1896)

    def test_failed_build_reported(self, tmp_path):
        o = build_day(
            self._plan(tmp_path), tmp_path, FakeDownsampler("failed"), epoch_counter=len
        )
        assert not o.ok


R2_BODY = """\
     2.11           OBSERVATION DATA    M (MIXED)           RINEX VERSION / TYPE
                                                            END OF HEADER
 26  7  1  0  0  0.0000000  0  2G01G02
 26  7  1  0  0 15.0000000  0  0
 26  7  1  0  0 30.0000000  0 12G01G02G03G04G05G06G07G08G09G10G11G12
"""
R3_BODY = """\
     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE
                                                            END OF HEADER
> 2026 07 01 00 00  0.0000000  0  3
> 2026 07 01 00 00 15.0000000  0  0
"""


@pytest.mark.parametrize("body,expected", [(R2_BODY, 2), (R3_BODY, 1)])
def test_count_epochs_with_sats(tmp_path, body, expected):
    """Epoch lines with zero satellites are not data (SKDA 2017-07-20)."""
    f = tmp_path / "x.26D.Z"
    f.write_bytes(gzip.compress(body.encode()))
    # `cat` stands in for CRX2RNX: the body is already plain RINEX.
    assert count_epochs_with_sats(f, crx2rnx="cat") == expected
