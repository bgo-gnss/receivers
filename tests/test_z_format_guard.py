"""Regression tests for the .Z = LZW compress invariant.

Two layers under test:

1. The producer: ``RawToRinexConverter._apply_compression`` with
   ``CompressionFormat.Z`` must emit genuine compress(1) LZW output
   (magic ``1f 9d``) and must FAIL LOUDLY (never fall back to gzip) when
   the compress binary is missing. The gzip-as-.Z era (cutover DOY 172
   2026 → 2026-07-06) silently contaminated ~98k archive files.

2. The chokepoints: ``format_guard`` must refuse gzip-magic .Z files at
   the archive-sync delta and the re-rinex push.
"""

import gzip
import logging
import shutil
import subprocess

import pytest

from receivers.archive.format_guard import (
    GZIP_MAGIC,
    LZW_MAGIC,
    bad_z_format,
    split_bad_z,
)
from receivers.rinex.converter_base import (
    CompressionFormat,
    ConversionError,
    RawToRinexConverter,
)

HAS_COMPRESS = shutil.which("compress") is not None


class _StubConverter(RawToRinexConverter):
    @property
    def converter_name(self):
        return "stub"

    @property
    def supported_extensions(self):
        return [".t02"]

    def _get_required_tools(self):
        return []

    def _run_conversion(self, *a, **k):  # pragma: no cover - never called
        raise NotImplementedError


def _make_converter(fmt: CompressionFormat) -> RawToRinexConverter:
    inst = _StubConverter.__new__(_StubConverter)
    inst.logger = logging.getLogger("test.zformat")
    inst.apply_hatanaka = False
    inst.compression_format = fmt
    return inst


# ---------------------------------------------------------------- producer


@pytest.mark.skipif(not HAS_COMPRESS, reason="compress(1) not installed")
def test_z_output_is_real_lzw(tmp_path):
    conv = _make_converter(CompressionFormat.Z)
    f = tmp_path / "TEST1860.26D"
    payload = b"RINEX observation payload " * 64
    f.write_bytes(payload)

    out = conv._apply_compression(f)

    assert out.name == "TEST1860.26D.Z"
    assert out.read_bytes()[:2] == LZW_MAGIC, "must be compress(1) LZW, not gzip"
    assert not f.exists(), "compress consumes the plain file"
    # round-trip: decompressed bytes identical to the original payload
    back = subprocess.run(["zcat", str(out)], capture_output=True, check=True)
    assert back.stdout == payload


@pytest.mark.skipif(not HAS_COMPRESS, reason="compress(1) not installed")
def test_z_output_never_gzip(tmp_path):
    conv = _make_converter(CompressionFormat.Z)
    f = tmp_path / "TEST1870.26D"
    f.write_bytes(b"x" * 4096)
    out = conv._apply_compression(f)
    assert out.read_bytes()[:2] != GZIP_MAGIC


def test_missing_compress_fails_loudly_no_gzip_fallback(tmp_path, monkeypatch):
    """compress absent -> ConversionError; NEVER a silent gzip-.Z file."""
    conv = _make_converter(CompressionFormat.Z)
    f = tmp_path / "TEST1880.26D"
    f.write_bytes(b"y" * 512)

    import receivers.rinex.converter_base as m

    def _no_compress(cmd, *a, **k):
        raise FileNotFoundError("compress")

    monkeypatch.setattr(m.subprocess, "run", _no_compress, raising=True)
    with pytest.raises(ConversionError, match="compress"):
        conv._apply_compression(f)
    assert not (tmp_path / "TEST1880.26D.Z").exists()


def test_gz_output_still_gzip(tmp_path):
    conv = _make_converter(CompressionFormat.GZ)
    f = tmp_path / "TEST1890.26D"
    f.write_bytes(b"z" * 512)
    out = conv._apply_compression(f)
    assert out.name.endswith(".gz")
    assert out.read_bytes()[:2] == GZIP_MAGIC


# -------------------------------------------------------------- chokepoint


def test_bad_z_format_detects_gzip(tmp_path):
    f = tmp_path / "STAT0010.26D.Z"
    with gzip.open(f, "wb") as fh:
        fh.write(b"data")
    reason = bad_z_format(f)
    assert reason is not None and "gzip" in reason


def test_bad_z_format_accepts_lzw(tmp_path):
    f = tmp_path / "STAT0020.26D.Z"
    f.write_bytes(LZW_MAGIC + b"\x90rest-of-lzw-stream")
    assert bad_z_format(f) is None


def test_bad_z_format_flags_empty_and_junk(tmp_path):
    empty = tmp_path / "STAT0030.26D.Z"
    empty.write_bytes(b"")
    assert bad_z_format(empty) is not None

    junk = tmp_path / "STAT0040.26D.Z"
    junk.write_bytes(b"AB")
    assert bad_z_format(junk) is not None


def test_bad_z_format_ignores_non_z(tmp_path):
    f = tmp_path / "STAT0050.26D.gz"
    with gzip.open(f, "wb") as fh:
        fh.write(b"data")
    assert bad_z_format(f) is None  # .gz with gzip bytes is correct


def test_split_bad_z_partitions_and_logs(tmp_path, caplog):
    good = tmp_path / "GOOD0010.26D.Z"
    good.write_bytes(LZW_MAGIC + b"ok")
    bad = tmp_path / "BAD00010.26D.Z"
    with gzip.open(bad, "wb") as fh:
        fh.write(b"data")
    other = tmp_path / "RAW00010.sbf.gz"
    other.write_bytes(GZIP_MAGIC + b"raw")

    logger = logging.getLogger("test.guard")
    with caplog.at_level(logging.ERROR, logger="test.guard"):
        ok, refused = split_bad_z(
            [good.name, bad.name, other.name], logger, root=str(tmp_path)
        )
    assert ok == [good.name, other.name]
    assert refused == [bad.name]
    assert any("refusing" in r.message for r in caplog.records)


# ---------------------------------------------------- strict Hatanaka (re-rinex)


class TestStrictHatanaka:
    """Re-rinex mode: Hatanaka failure must FAIL the file — never stage an
    uncompacted .o product (breaks the .D.Z convention + resume-skip)."""

    def _converter(self, strict: bool):
        conv = _make_converter(CompressionFormat.Z)
        conv.strict_hatanaka = strict
        conv.apply_hatanaka = True
        return conv

    def test_strict_failure_raises_and_cleans_up(self, tmp_path, monkeypatch):
        conv = self._converter(strict=True)
        f = tmp_path / "TEST2440.24o"
        f.write_bytes(b"RINEX OBS with corrupt lines")
        monkeypatch.setattr(conv, "get_tool_path", lambda t: "/usr/bin/rnx2crx")
        monkeypatch.setattr(
            conv,
            "_run_subprocess",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("exit code 1")),
        )
        with pytest.raises(ConversionError, match="Hatanaka compression failed"):
            conv._apply_hatanaka_compression(f)
        assert not f.exists(), "uncompacted intermediate must not linger"

    def test_strict_no_output_raises(self, tmp_path, monkeypatch):
        conv = self._converter(strict=True)
        f = tmp_path / "TEST2450.24o"
        f.write_bytes(b"RINEX OBS")
        monkeypatch.setattr(conv, "get_tool_path", lambda t: "/usr/bin/rnx2crx")
        monkeypatch.setattr(conv, "_run_subprocess", lambda *a, **k: None)  # no .24d
        with pytest.raises(ConversionError, match="no output"):
            conv._apply_hatanaka_compression(f)

    def test_default_keeps_degraded_fallback(self, tmp_path, monkeypatch):
        conv = self._converter(strict=False)
        f = tmp_path / "TEST2460.24o"
        f.write_bytes(b"RINEX OBS")
        monkeypatch.setattr(conv, "get_tool_path", lambda t: "/usr/bin/rnx2crx")
        monkeypatch.setattr(
            conv,
            "_run_subprocess",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("exit code 1")),
        )
        out = conv._apply_hatanaka_compression(f)
        assert out == f and f.exists(), "daily pipeline keeps degraded-but-present"
