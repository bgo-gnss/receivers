"""Tests for the compression-invariant content_sha256 primitive.

Cornerstone invariant: the same bytes hash to ONE value whether stored plain,
gzip, bzip2 or xz; different bytes hash differently. That round-trip is what
proves the primitive does its job (back-zip verify, dedup, integrity, #34).
"""

import bz2
import gzip
import hashlib
import lzma

import pytest

from receivers.utils.content_hash import CorruptArchiveFileError, content_sha256

PAYLOAD = (
    b"SEPTENTRIO PolaRx5 SBF block stream \x00\x01\x02 ... 15s_24hr raw payload" * 500
)
OTHER = b"a different receiver payload entirely" * 500


def _write(p, data):
    p.write_bytes(data)
    return p


class TestCompressionInvariant:
    def test_plain_matches_raw_sha256(self, tmp_path):
        f = _write(tmp_path / "plain.bin", PAYLOAD)
        assert content_sha256(f) == hashlib.sha256(PAYLOAD).hexdigest()

    def test_gzip_equals_plain(self, tmp_path):
        plain = _write(tmp_path / "x.T02", PAYLOAD)
        gz = tmp_path / "x.T02.gz"
        with gzip.GzipFile(gz, "wb") as fh:
            fh.write(PAYLOAD)
        assert content_sha256(gz) == content_sha256(plain)

    def test_all_formats_one_hash(self, tmp_path):
        plain = _write(tmp_path / "p.bin", PAYLOAD)
        gz = tmp_path / "p.gz"
        bz = tmp_path / "p.bz2"
        xz = tmp_path / "p.xz"
        with gzip.GzipFile(gz, "wb") as fh:
            fh.write(PAYLOAD)
        with bz2.BZ2File(bz, "wb") as fh:
            fh.write(PAYLOAD)
        with lzma.LZMAFile(xz, "wb") as fh:
            fh.write(PAYLOAD)
        hashes = {content_sha256(p) for p in (plain, gz, bz, xz)}
        assert len(hashes) == 1

    def test_different_content_different_hash(self, tmp_path):
        a = _write(tmp_path / "a.bin", PAYLOAD)
        b_gz = tmp_path / "b.gz"
        with gzip.GzipFile(b_gz, "wb") as fh:
            fh.write(OTHER)
        assert content_sha256(a) != content_sha256(b_gz)

    def test_gzip_recompression_is_stable(self, tmp_path):
        # Two independent gzips of the same bytes (different mtime/level) still
        # share a content hash — the back-zip-verify guarantee.
        g1 = tmp_path / "1.gz"
        g2 = tmp_path / "2.gz"
        with gzip.GzipFile(g1, "wb", compresslevel=1, mtime=0) as fh:
            fh.write(PAYLOAD)
        with gzip.GzipFile(g2, "wb", compresslevel=9, mtime=12345) as fh:
            fh.write(PAYLOAD)
        assert content_sha256(g1) == content_sha256(g2)


class TestMagicBytesOverExtension:
    def test_gzip_content_named_uncompressed(self, tmp_path):
        # A gzip stream that happens to be named ".T02" (no .gz) must still be
        # decompressed — detection is by magic bytes, not extension.
        mislabeled = tmp_path / "ALHV0410.26.T02"
        with gzip.GzipFile(mislabeled, "wb") as fh:
            fh.write(PAYLOAD)
        plain = _write(tmp_path / "twin.bin", PAYLOAD)
        assert content_sha256(mislabeled) == content_sha256(plain)


class TestCorruptInput:
    def test_truncated_gzip_raises(self, tmp_path):
        gz = tmp_path / "bad.gz"
        with gzip.GzipFile(gz, "wb") as fh:
            fh.write(PAYLOAD)
        # Lop off the trailing bytes (CRC/length trailer) -> corrupt stream.
        data = gz.read_bytes()
        gz.write_bytes(data[: len(data) // 2])
        with pytest.raises(CorruptArchiveFileError):
            content_sha256(gz)

    def test_not_actually_gzip_with_gz_magic(self, tmp_path):
        # Starts with the gzip magic but is garbage afterwards.
        f = tmp_path / "fake.gz"
        f.write_bytes(b"\x1f\x8b" + b"\x00" * 32)
        with pytest.raises(CorruptArchiveFileError):
            content_sha256(f)


class TestUnsupportedFormats:
    def test_dot_z_raises_not_implemented(self, tmp_path):
        # .Z (Unix compress) magic 0x1f 0x9d — deferred to the RINEX track.
        f = tmp_path / "ELDC0410.26d.Z"
        f.write_bytes(b"\x1f\x9d" + b"\x00" * 32)
        with pytest.raises(NotImplementedError):
            content_sha256(f)


class TestEmptyAndPlain:
    def test_empty_file_hashes_as_empty(self, tmp_path):
        f = _write(tmp_path / "empty", b"")
        assert content_sha256(f) == hashlib.sha256(b"").hexdigest()
