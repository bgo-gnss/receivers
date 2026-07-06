"""Tests for receivers.archive.audit — archive lint + regen-candidate scan."""

import gzip
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from receivers.archive.audit import (
    _obs_date,
    audit_station_session,
)

HAS_COMPRESS = shutil.which("compress") is not None

LZW_MAGIC = b"\x1f\x9d"


def _lzw(payload: bytes, tmp: Path) -> bytes:
    plain = tmp / "payload.plain"
    plain.write_bytes(payload)
    subprocess.run(["compress", "-f", str(plain)], check=True, capture_output=True)
    return (tmp / "payload.plain.Z").read_bytes()


def _mk(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_obs_date_parsing():
    assert _obs_date("RHOF2400.15D.Z") == date(2015, 8, 28)
    assert _obs_date("RHOF2440.24o.Z") == date(2024, 8, 31)
    assert _obs_date("RHOF001a.26D.Z") == date(2026, 1, 1)  # hourly, hour a
    assert _obs_date("RHOF0010.26D.Z") == date(2026, 1, 1)
    assert _obs_date("garbage.txt") is None


@pytest.fixture
def archive(tmp_path):
    """Fake archive: one clean file, four problem classes, one raw-only date."""
    root = tmp_path / "arch"
    lzw = LZW_MAGIC + b"\x90fake-lzw-stream"
    # clean product (valid name + LZW magic), 2015-08-27 = DOY 239
    _mk(root, "2015/aug/RHOF/15s_24hr/rinex/RHOF2390.15D.Z", lzw)
    # bad names
    _mk(root, "2015/aug/RHOF/15s_24hr/rinex/RHOF2400.15o.Z", lzw)
    _mk(root, "2015/aug/RHOF/15s_24hr/rinex/RHOF2410.15d", b"plain hatanaka")
    # gzip-as-.Z (valid name, wrong magic)
    gz = root / "2015/aug/RHOF/15s_24hr/rinex/RHOF2420.15D.Z"
    gz.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(gz, "wb") as fh:
        fh.write(b"content fine, format wrong")
    # legit subdir must be ignored
    _mk(root, "2015/aug/RHOF/15s_24hr/rinex/superseded_rt_x/RHOF0000.15D.Z", b"x")
    # raw present for DOY 243 (2015-08-31), no rinex product at all
    _mk(root, "2015/aug/RHOF/15s_24hr/raw/RHOF201508310000a.T02", b"raw")
    # raw for the clean date — must NOT be flagged missing
    _mk(root, "2015/aug/RHOF/15s_24hr/raw/RHOF201508270000a.T02", b"raw")
    return root


def test_audit_flags_all_classes(archive):
    rep = audit_station_session(archive, "RHOF", "15s_24hr")
    counts = rep.counts()
    assert counts == {"bad-name": 2, "bad-magic": 1, "missing": 1}
    assert rep.scanned == 4  # subdir contents not scanned
    assert rep.clean == 1

    by_issue = {f.issue: f for f in rep.findings}
    # junk: only the bad names (gzip-.Z content is fine; missing is raw-side)
    assert sorted(Path(p).name for p in rep.junk_paths) == [
        "RHOF2400.15o.Z",
        "RHOF2410.15d",
    ]
    assert by_issue["bad-magic"].junk is False
    # regen: the two bad-name dates + the missing date; NOT the gzip date
    assert rep.regen_dates == [date(2015, 8, 28), date(2015, 8, 29), date(2015, 8, 31)]


def test_audit_years_filter(archive):
    rep = audit_station_session(archive, "RHOF", "15s_24hr", years={1999})
    assert rep.scanned == 0 and not rep.findings


def test_audit_no_missing_flag(archive):
    rep = audit_station_session(archive, "RHOF", "15s_24hr", check_missing=False)
    assert "missing" not in rep.counts()


@pytest.mark.skipif(not HAS_COMPRESS, reason="compress(1) not installed")
def test_audit_deep_flags_undecompressible(tmp_path):
    root = tmp_path / "arch"
    good = _lzw(b"COMPACT RINEX content " * 50, tmp_path)
    # valid name, LZW magic, corrupt code stream → --deep flags it. (NB: a
    # merely TRUNCATED LZW stream decompresses cleanly — LZW has no
    # checksum/length trailer — so --deep catches garbage and truncated
    # GZIP files, not truncated LZW.)
    _mk(
        root,
        "2016/jan/RHOF/15s_24hr/rinex/RHOF0010.16D.Z",
        LZW_MAGIC + b"\x90" + b"\xff" * 200,
    )
    _mk(root, "2016/jan/RHOF/15s_24hr/rinex/RHOF0020.16D.Z", good)

    rep = audit_station_session(
        root, "RHOF", "15s_24hr", deep=True, check_missing=False
    )
    assert rep.counts().get("unreadable") == 1
    assert rep.clean == 1
    assert [f.file_date for f in rep.findings] == [date(2016, 1, 1)]


@pytest.mark.skipif(not HAS_COMPRESS, reason="compress(1) not installed")
def test_audit_check_version_flags_rinex2(tmp_path):
    root = tmp_path / "arch"
    (tmp_path / "a").mkdir()
    r2 = _lzw(
        b"1.0                 COMPACT RINEX FORMAT"
        + b" " * 20
        + b"CRINEX VERS   / TYPE\n",
        tmp_path / "a",
    )
    (tmp_path / "b").mkdir()
    r3 = _lzw(
        b"3.0                 COMPACT RINEX FORMAT"
        + b" " * 20
        + b"CRINEX VERS   / TYPE\n",
        tmp_path / "b",
    )
    _mk(root, "2013/jan/RHOF/15s_24hr/rinex/RHOF0010.13D.Z", r2)
    _mk(root, "2013/jan/RHOF/15s_24hr/rinex/RHOF0020.13D.Z", r3)

    rep = audit_station_session(
        root, "RHOF", "15s_24hr", check_version=True, check_missing=False
    )
    assert rep.counts() == {"old-version": 1}
    f = rep.findings[0]
    assert f.regen is True and f.junk is False
    assert f.file_date == date(2013, 1, 1)
    assert rep.clean == 1
