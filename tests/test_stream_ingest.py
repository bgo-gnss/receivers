"""Tests for stream-capture archive ingestion (mocked Hatanaka tools)."""

from pathlib import Path

from receivers.streaming.downsample import _swap_obs_to_hatanaka
from receivers.streaming.ingest import (
    StreamIngestor,
    parse_bnc_rinex_name,
)


class FakeRunner:
    def __init__(self, fail_tool=None):
        self.calls = []
        self.fail_tool = fail_tool

    def __call__(self, cmd, stdout_path=None):
        self.calls.append([str(c) for c in cmd])
        tool = Path(cmd[0]).name.upper()
        if self.fail_tool and self.fail_tool.upper() in tool:
            return 1
        if stdout_path is not None:
            Path(stdout_path).write_bytes(b"X" * 2000)
        if tool == "RNX2CRX":
            obs = Path(cmd[-1])
            obs.with_name(_swap_obs_to_hatanaka(obs.name)).write_bytes(b"hat")
        return 0


class TestParse:
    def test_valid_hourly(self):
        f = parse_bnc_rinex_name("GONH162a.26O")
        assert f is not None
        assert (f.station, f.doy, f.hour, f.year) == ("GONH", 162, 0, 2026)
        assert f.datetime.month == 6 and f.datetime.day == 11  # doy162/2026 = Jun 11
        assert f.datetime.hour == 0
        # canonical archive name is short + lowercase (fleet/SBF convention)
        assert f.hatanaka_name == "GONH162a.26D.Z"

    def test_hour_letters(self):
        assert parse_bnc_rinex_name("GONH162b.26O").hour == 1
        assert parse_bnc_rinex_name("GONH162x.26O").hour == 23

    def test_rinex3_long_name(self):
        # BNC RINEX 3 output: SSSSMRCCC_S_YYYYDDDHHMM_01H_MO.rnx
        f = parse_bnc_rinex_name("GONH00ISL_S_20261670700_01H_MO.rnx")
        assert f is not None
        assert (f.station, f.doy, f.hour, f.year) == ("GONH", 167, 7, 2026)
        # normalizes to the SAME short, lowercase archive name as RINEX 2 would
        assert f.hatanaka_name == "GONH167h.26D.Z"
        assert f.short_obs_name == "GONH167h.26o"

    def test_rinex3_long_name_with_sample_field(self):
        f = parse_bnc_rinex_name("GONH00ISL_R_20261670700_01H_01S_MO.rnx")
        assert f is not None and f.hour == 7 and f.doy == 167

    def test_archive_path(self):
        f = parse_bnc_rinex_name("GONH162a.26O")
        p = f.archive_path("/data")
        assert p == Path("/data/2026/jun/GONH/1Hz_1hr/rinex/GONH162a.26D.Z")

    def test_invalid_returns_none(self):
        assert parse_bnc_rinex_name("README.txt") is None
        assert parse_bnc_rinex_name("GONH162a.26N") is None  # nav, not obs
        assert parse_bnc_rinex_name("GONH00ISL_S_20261670700_01H_MN.rnx") is None  # nav


class TestIngest:
    def _make(self, rt_dir, *names):
        rt_dir.mkdir(parents=True, exist_ok=True)
        for n in names:
            (rt_dir / n).write_bytes(b"rinex-obs")

    def test_missing_dir_empty_result(self, tmp_path):
        ing = StreamIngestor(archive_base=tmp_path / "arch")
        res = ing.ingest_dir("GONH", tmp_path / "nope")
        assert res.ingested == [] and res.failed == []

    def test_skips_current_hour(self, tmp_path):
        rt = tmp_path / "RT" / "GONH"
        self._make(rt, "GONH162a.26O", "GONH162b.26O")
        now = parse_bnc_rinex_name("GONH162b.26O").datetime  # hour 1 is "current"
        tracked = []
        ing = StreamIngestor(
            archive_base=tmp_path / "arch",
            runner=FakeRunner(),
            tracker=lambda *a: tracked.append(a),
        )
        res = ing.ingest_dir("GONH", rt, now=now)
        assert res.skipped_current == ["GONH162b.26O"]
        assert res.ingested == ["GONH162a.26O"]
        # archived to the dated path (short, lowercase)
        dest = tmp_path / "arch/2026/jun/GONH/1Hz_1hr/rinex/GONH162a.26D.Z"
        assert dest.exists()
        # tracker recorded the ingested file
        assert len(tracked) == 1
        assert tracked[0][0] == "GONH" and tracked[0][1] == dest
        assert tracked[0][3] == "1Hz_1hr"

    def test_ingests_rinex3_long_name(self, tmp_path):
        """RINEX 3 long-named BNC output archives under the short, lowercase name,
        and the short-name intermediates are cleaned up (long source left for BNC)."""
        rt = tmp_path / "RT" / "GONH"
        self._make(rt, "GONH00ISL_S_20261670700_01H_MO.rnx")  # doy167 hr7 = Jun16
        now = parse_bnc_rinex_name(
            "GONH00ISL_S_20261672300_01H_MO.rnx"
        ).datetime  # hr23 current
        tracked = []
        ing = StreamIngestor(
            archive_base=tmp_path / "arch",
            runner=FakeRunner(),
            tracker=lambda *a: tracked.append(a),
        )
        res = ing.ingest_dir("GONH", rt, now=now)
        assert res.ingested == ["GONH00ISL_S_20261670700_01H_MO.rnx"]
        dest = tmp_path / "arch/2026/jun/GONH/1Hz_1hr/rinex/GONH167h.26D.Z"
        assert dest.exists()
        assert len(tracked) == 1 and tracked[0][1] == dest
        # short-name working copies cleaned; long source left in place for BNC
        assert not (rt / "GONH167h.26o").exists()
        assert not (rt / "GONH167h.26d").exists()
        assert (rt / "GONH00ISL_S_20261670700_01H_MO.rnx").exists()

    def test_prefers_rinex3_when_both_present(self, tmp_path):
        """Mid RINEX2->RINEX3 switch: both a short .YYO and a long .rnx exist for
        the same hour (both normalize to one archive name). The .rnx (RINEX 3)
        must win so the archive isn't silently overwritten with the RINEX 2 file."""
        rt = tmp_path / "RT" / "GONH"
        self._make(
            rt, "GONH167h.26O", "GONH00ISL_S_20261670700_01H_MO.rnx"
        )  # both hour 7
        now = parse_bnc_rinex_name("GONH167x.26O").datetime  # hour 23 = current
        ing = StreamIngestor(archive_base=tmp_path / "arch", runner=FakeRunner())
        res = ing.ingest_dir("GONH", rt, now=now)
        assert res.ingested == ["GONH00ISL_S_20261670700_01H_MO.rnx"]  # R3 only
        assert (tmp_path / "arch/2026/jun/GONH/1Hz_1hr/rinex/GONH167h.26D.Z").exists()

    def test_ignores_unparseable(self, tmp_path):
        rt = tmp_path / "RT" / "GONH"
        self._make(rt, "GONH162a.26O", "notes.txt")
        now = parse_bnc_rinex_name("GONH162x.26O").datetime  # nothing current
        ing = StreamIngestor(archive_base=tmp_path / "arch", runner=FakeRunner())
        res = ing.ingest_dir("GONH", rt, now=now)
        assert res.ingested == ["GONH162a.26O"]

    def test_tool_failure_recorded(self, tmp_path):
        rt = tmp_path / "RT" / "GONH"
        self._make(rt, "GONH162a.26O")
        now = parse_bnc_rinex_name("GONH162x.26O").datetime
        ing = StreamIngestor(
            archive_base=tmp_path / "arch", runner=FakeRunner(fail_tool="RNX2CRX")
        )
        res = ing.ingest_dir("GONH", rt, now=now)
        assert res.failed == ["GONH162a.26O"]
        assert res.ingested == []


class TestAlreadyArchivedGuard:
    """Re-ingest suppression — the 2026-09-13 stream rewrite loop.

    BNC never deletes what it wrote, so the RT dir grows without bound (1,293
    files for GONH, back to DOY 205). ``ingest_dir`` skipped only the CURRENT
    hour, so every pass re-ran RNX2CRX + compress + move over every completed
    file and refreshed its archive mtime. Measured: 1,223 of GONH's 1,268
    archived RINEX carried that day's mtime, the hourly sweep consequently saw
    ~3,350 "changed" RINEX and pushed them to the single-core rawdata gateway
    every hour (~59,500 transfers in 17 sweeps), and re-upserted ~3,550
    archive_catalog rows an hour.
    """

    def _ingestor(self, tmp_path, runner):
        return StreamIngestor(
            archive_base=tmp_path / "archive",
            rnx2crx="RNX2CRX",
            compressor=["compress"],
            runner=runner,
        )

    def _rt_dir(self, tmp_path, names):
        rt = tmp_path / "rt" / "GONH"
        rt.mkdir(parents=True)
        for n in names:
            (rt / n).write_bytes(b"obs-data")
        return rt

    def test_second_pass_ingests_nothing(self, tmp_path):
        """The bug: the same completed file was re-converted on every pass."""
        runner = FakeRunner()
        ing = self._ingestor(tmp_path, runner)
        rt = self._rt_dir(tmp_path, ["GONH162a.26O", "GONH162b.26O"])

        first = ing.ingest_dir("GONH", rt)
        assert len(first.ingested) == 2, first
        calls_after_first = len(runner.calls)

        second = ing.ingest_dir("GONH", rt)
        assert second.ingested == []
        assert sorted(second.skipped_archived) == ["GONH162a.26O", "GONH162b.26O"]
        # The expensive part must not run again at all.
        assert len(runner.calls) == calls_after_first

    def test_newer_source_still_replaces_the_archive(self, tmp_path):
        """R2 -> R3: a long .rnx appearing LATER must still win.

        This is why the guard compares mtime rather than mere existence. A bare
        dest.exists() check would pin the archive to the RINEX 2 version.
        """
        import os

        runner = FakeRunner()
        ing = self._ingestor(tmp_path, runner)
        rt = self._rt_dir(tmp_path, ["GONH162a.26O"])
        ing.ingest_dir("GONH", rt)

        dest = (
            tmp_path
            / "archive"
            / "2026"
            / "jun"
            / "GONH"
            / "1Hz_1hr"
            / "rinex"
            / "GONH162a.26D.Z"
        )
        assert dest.exists()
        # Age the archive copy, then drop in the RINEX 3 file for the same hour.
        old = dest.stat().st_mtime - 3600
        os.utime(dest, (old, old))
        (rt / "GONH00ISL_S_20261620000_01H_MO.rnx").write_bytes(b"rinex3-data")

        again = ing.ingest_dir("GONH", rt)
        assert again.ingested == ["GONH00ISL_S_20261620000_01H_MO.rnx"], again
        assert again.skipped_archived == []

    def test_current_hour_still_skipped(self, tmp_path):
        from datetime import UTC, datetime

        runner = FakeRunner()
        ing = self._ingestor(tmp_path, runner)
        rt = self._rt_dir(tmp_path, ["GONH162a.26O"])
        now = datetime(2026, 6, 11, 0, 30, tzinfo=UTC)  # doy 162, hour 0
        res = ing.ingest_dir("GONH", rt, now=now)
        assert res.skipped_current == ["GONH162a.26O"]
        assert res.ingested == [] and res.skipped_archived == []

    def test_absent_destination_is_ingested(self, tmp_path):
        runner = FakeRunner()
        ing = self._ingestor(tmp_path, runner)
        rt = self._rt_dir(tmp_path, ["GONH162a.26O"])
        res = ing.ingest_dir("GONH", rt)
        assert res.ingested == ["GONH162a.26O"]

    def test_guard_fails_open_on_stat_error(self, tmp_path):
        """A permission/race problem must degrade to ingesting, never to dropping.

        Uses a stub rather than monkeypatching Path.stat — patching it globally
        breaks pytest's own path handling mid-run.
        """
        ing = self._ingestor(tmp_path, FakeRunner())

        class _Exploding:
            def exists(self):
                return True

            def stat(self):
                raise OSError("permission denied")

        assert ing._already_archived(_Exploding(), _Exploding()) is False
