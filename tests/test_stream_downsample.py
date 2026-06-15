"""Tests for the 1Hz→15s RINEX downsampler (mocked external tools)."""

from pathlib import Path

from receivers.streaming.downsample import (
    RinexDownsampler,
    _obs_name_for_target,
    _swap_obs_to_hatanaka,
)


class FakeRunner:
    """Records commands and simulates each tool's file side-effects."""

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
        if tool == "CRX2RNX":
            crx = Path(cmd[-1])
            # Real CRX2RNX preserves the case of the trailing char (.D->.O, .d->.o).
            last = crx.suffix[-1]
            obs = crx.with_suffix(crx.suffix[:-1] + ("O" if last == "D" else "o"))
            obs.write_bytes(b"obs")
        elif tool == "RNX2CRX":
            obs = Path(cmd[-1])
            obs.with_name(_swap_obs_to_hatanaka(obs.name)).write_bytes(b"hatanaka")
        elif tool == "GFZRNX":
            # gfzrnx writes its -fout target directly (no stdout redirect).
            out = Path(cmd[cmd.index("-fout") + 1])
            out.write_bytes(b"X" * 2000)
        return 0

    @property
    def tools(self):
        return [Path(c[0]).name.upper() for c in self.calls]


def _sources(tmp_path, n=3):
    files = []
    for h in range(n):
        f = tmp_path / "src" / f"GONH162{h}.26d.Z"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"hatanaka-gz")
        files.append(f)
    return files


class TestNameHelpers:
    def test_obs_name_for_target(self):
        assert _obs_name_for_target("GONH1620.26D.Z") == "GONH1620.26O"
        assert _obs_name_for_target("gonh1620.26d.gz") == "gonh1620.26o"
        assert _obs_name_for_target("GONH1620.26D") == "GONH1620.26O"

    def test_swap_obs_to_hatanaka(self):
        assert _swap_obs_to_hatanaka("GONH1620.26O") == "GONH1620.26D"
        assert _swap_obs_to_hatanaka("gonh1620.26o") == "gonh1620.26d"


class TestDownsample:
    def test_skip_if_exists(self, tmp_path):
        out = tmp_path / "GONH1620.26D.Z"
        out.write_bytes(b"X" * 400_000)
        runner = FakeRunner()
        ds = RinexDownsampler(runner=runner)
        res = ds.downsample_day("GONH", _sources(tmp_path), out, tmp_path / "wd")
        assert res.status == "skipped_exists"
        assert runner.calls == []  # no tools invoked

    def test_no_source(self, tmp_path):
        out = tmp_path / "GONH1620.26D.Z"
        missing = [tmp_path / "nope1.d.Z", tmp_path / "nope2.d.Z"]
        res = RinexDownsampler(runner=FakeRunner()).downsample_day(
            "GONH", missing, out, tmp_path / "wd"
        )
        assert res.status == "no_source"
        assert res.source_count == 0

    def test_created_runs_full_pipeline(self, tmp_path):
        out = tmp_path / "arch" / "GONH1620.26D.Z"
        runner = FakeRunner()
        ds = RinexDownsampler(interval=15, runner=runner)
        res = ds.downsample_day("GONH", _sources(tmp_path, 3), out, tmp_path / "wd")
        assert res.status == "created"
        assert res.ok and res.source_count == 3
        assert out.exists()
        # 3 decompress + 3 crx2rnx + 1 gfzrnx + 1 rnx2crx + 1 compress
        assert runner.tools.count("CRX2RNX") == 3
        assert runner.tools.count("GFZRNX") == 1
        assert runner.tools.count("RNX2CRX") == 1
        # gfzrnx invoked with the decimation flags (RINEX2 out, 15s sampling)
        gfz_cmd = next(c for c in runner.calls if Path(c[0]).name == "gfzrnx")
        assert "-smp" in gfz_cmd and "15" in gfz_cmd
        assert "-vo" in gfz_cmd and "2" in gfz_cmd

    def test_uppercase_bnc_hatanaka_sources(self, tmp_path):
        """BNC writes uppercase-O RINEX2, so stream-ingested hourly files are
        ``.26D.Z`` and CRX2RNX yields ``.26O``. Regression: ``_to_obs`` hardcoded
        lowercase ``o``, handing the decimator a non-existent ``.26o`` path and
        breaking every stream downsample.
        """
        out = tmp_path / "GONH1620.26D.Z"
        files = []
        for h in range(3):
            f = tmp_path / "src" / f"GONH162{h}.26D.Z"  # uppercase D (BNC path)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"hatanaka-gz")
            files.append(f)
        runner = FakeRunner()
        res = RinexDownsampler(interval=15, runner=runner).downsample_day(
            "GONH", files, out, tmp_path / "wd"
        )
        assert res.status == "created", res.error
        assert res.source_count == 3
        # the decimator must receive the uppercase .26O obs files CRX2RNX actually
        # wrote, not the lowercase .26o the old code assumed.
        gfz_cmd = next(c for c in runner.calls if Path(c[0]).name == "gfzrnx")
        assert any(a.endswith(".26O") for a in gfz_cmd), gfz_cmd
        assert not any(a.endswith(".26o") for a in gfz_cmd), gfz_cmd

    def test_partial_sources_only_existing_used(self, tmp_path):
        out = tmp_path / "GONH1620.26D.Z"
        srcs = _sources(tmp_path, 2) + [
            tmp_path / "src" / "GONH1623.26d.Z"
        ]  # last missing
        runner = FakeRunner()
        res = RinexDownsampler(runner=runner).downsample_day(
            "GONH", srcs, out, tmp_path / "wd"
        )
        assert res.status == "created"
        assert res.source_count == 2
        assert runner.tools.count("CRX2RNX") == 2

    def test_decimation_failure_reported(self, tmp_path):
        out = tmp_path / "GONH1620.26D.Z"
        res = RinexDownsampler(runner=FakeRunner(fail_tool="gfzrnx")).downsample_day(
            "GONH", _sources(tmp_path), out, tmp_path / "wd"
        )
        assert res.status == "failed"
        assert "gfzrnx" in (res.error or "").lower()
        assert not out.exists()
