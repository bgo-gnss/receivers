"""Tests for the stream-capture pipeline orchestrator."""

from datetime import date
from pathlib import Path
from unittest.mock import Mock

from receivers.streaming.pipeline import (
    StreamPipeline,
    daily_15s_target,
    hourly_1hz_sources,
)

DAY = date(2026, 6, 11)  # doy 162


class TestPathHelpers:
    def test_daily_15s_target(self):
        # Lowercase d = fleet/SBF canonical, so the daily SBF download supersedes
        # the stream downsample in place (not a .26D.Z vs .26d.Z collision).
        p = daily_15s_target("/data", "GONH", DAY)
        assert p == Path("/data/2026/jun/GONH/15s_24hr/rinex/GONH1620.26d.Z")

    def test_hourly_sources_globs(self, tmp_path):
        rinex = tmp_path / "2026/jun/GONH/1Hz_1hr/rinex"
        rinex.mkdir(parents=True)
        for n in ("GONH162a.26D.Z", "GONH162b.26D.Z", "GONH162A.26d.gz"):
            (rinex / n).write_bytes(b"x")
        (rinex / "other.txt").write_bytes(b"x")
        found = [p.name for p in hourly_1hz_sources(tmp_path, "GONH", DAY)]
        assert found == ["GONH162A.26d.gz", "GONH162a.26D.Z", "GONH162b.26D.Z"]

    def test_hourly_sources_missing_dir(self, tmp_path):
        assert hourly_1hz_sources(tmp_path, "GONH", DAY) == []


def _pipeline(tmp_path, **over):
    kw = dict(
        supervisor=Mock(),
        ingestor=Mock(),
        downsampler=Mock(),
        gap_filler=Mock(),
        downloader=Mock(),
        rt_base=tmp_path / "RT",
        archive_base=tmp_path / "arch",
        workdir=tmp_path / "wd",
    )
    kw.update(over)
    return StreamPipeline(**kw)


class TestProcessStation:
    def test_sequences_stages_with_correct_args(self, tmp_path):
        # seed one 1Hz source so downsample receives it
        rinex = tmp_path / "arch/2026/jun/GONH/1Hz_1hr/rinex"
        rinex.mkdir(parents=True)
        (rinex / "GONH162a.26D.Z").write_bytes(b"x")

        p = _pipeline(tmp_path)
        res = p.process_station("GONH", DAY)

        p.ingestor.ingest_dir.assert_called_once()
        assert p.ingestor.ingest_dir.call_args.args[0] == "GONH"
        assert p.ingestor.ingest_dir.call_args.args[1] == tmp_path / "RT" / "GONH"

        ds_args = p.downsampler.downsample_day.call_args.args
        assert ds_args[0] == "GONH"
        assert [s.name for s in ds_args[1]] == ["GONH162a.26D.Z"]  # sources
        assert ds_args[2] == daily_15s_target(tmp_path / "arch", "GONH", DAY)

        gap_kwargs = p.gap_filler.check_and_fill.call_args.kwargs
        assert gap_kwargs["downloader"] is p.downloader

        assert res.station_id == "GONH"
        assert res.ingest is p.ingestor.ingest_dir.return_value
        assert res.downsample is p.downsampler.downsample_day.return_value
        assert res.gap is p.gap_filler.check_and_fill.return_value

    def test_gap_fill_skipped_when_no_1hz_on_disk(self, tmp_path):
        """A station that doesn't log 1 Hz on disk (gap_fill=False) must not
        invoke the gap-filler — otherwise it re-downloads the daily coarse SBF
        and mislabels it as 1 Hz hourly."""
        p = _pipeline(tmp_path)
        res = p.process_station("GONH", DAY, gap_fill=False)
        p.gap_filler.check_and_fill.assert_not_called()
        assert res.gap is None
        # ingest + downsample still run
        p.ingestor.ingest_dir.assert_called_once()
        p.downsampler.downsample_day.assert_called_once()


class TestRun:
    def test_supervises_then_processes_each(self, tmp_path):
        p = _pipeline(tmp_path)
        results = p.run(["GONH", "MOHA"], DAY)
        p.supervisor.supervise.assert_called_once()
        assert [r.station_id for r in results] == ["GONH", "MOHA"]

    def test_isolates_per_station_failure(self, tmp_path):
        p = _pipeline(tmp_path)
        p.ingestor.ingest_dir.side_effect = [RuntimeError("boom"), Mock()]
        results = p.run(["BAD", "GOOD"], DAY)
        # BAD raised and was skipped; GOOD still processed
        assert [r.station_id for r in results] == ["GOOD"]
