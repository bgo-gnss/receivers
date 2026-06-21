"""Tests for the compression-invariant content_sha256 primitive.

Cornerstone invariant: the same bytes hash to ONE value whether stored plain,
gzip, bzip2 or xz; different bytes hash differently. That round-trip is what
proves the primitive does its job (back-zip verify, dedup, integrity, #34).
"""

import base64
import bz2
import gzip
import hashlib
import lzma

import pytest

from receivers.utils.content_hash import CorruptArchiveFileError, content_sha256

# A real ``.Z`` (Unix LZW compress) blob, embedded so the test needs only
# ``gzip -dc`` to decode (universally present) — not the ``compress`` *encoder*
# (often absent in CI). Produced once with: compress -c plain.bin > plain.Z
# where ``plain.bin`` is the payload below; PLAIN_SHA is its sha256.
DOT_Z_B64 = (
    "H52QU4pAoVLECRUpSZ6AQBKGThg3YdaEAYHQSREsIFyQcaEFBJwwedi8CUMGBIAAAkCwSeOmjIKAAws"
    "eTLiw4cOIEytezLix48eQI0ueTLmy5UuBBA0iVMjQIUSJFJNYxKiRo0eQIkmaRKmSpUuYSWcytfk0p9Sd"
    "VX1iDbqVqNejMZXSbHoTqk6qPa8C1Tq0q1GwMpfWdIoz6lSeVn9mFcq16FekgeeSLXwXsdq9jN3+hSx3L"
    "GG7Z/EmXsu38VvAnQfXNXs4rd7Fbf0+jitWdVnDaPMqZtvXMdywgunerux6d2nNs4FL/sw692jMsX2jri"
    "2ccmjLr3mb3kw7+GTQrXWTziz7d2TPq3GLvgy792nO1L83X5/9ePnp3pmrx26cvHT4+aVHnHjQucedcug"
    "Nd11x40X3XnfLCbggge1tl9x5tlkX3nMVImdeatWB5xx72nmIX4QKbkiiff9BmKCGI9bn34MIZigiff05"
    "eCCGIc7HX4MGXgiifPsxWKCFH8an34AclngfgCjCiCOQSJ744o0/HmkilFf6aGSHT7poo5cUOtlijT0WW"
    "SaLNPJIJJMrzrjjkEtO2CSbcyopoYoy6iikninGmGOQSQYY6JRahonmm3bG6WehUWL5pZlvAQ=="
)
DOT_Z_PLAIN_SHA = "cc5e22fe40fe9978600890d8b1c55187ea637029a4e6111154088d460f030134"
DOT_Z_PLAIN = b"SEPTENTRIO Hatanaka RINEX .d.Z payload \x00\x01\x02 line\n" * 40

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


class TestDotZCompress:
    """.Z (Unix LZW compress) — decoded via the gzip binary, hashed decompressed."""

    def test_dot_z_hashes_decompressed_content(self, tmp_path):
        # A real .Z file hashes to the sha256 of its *decompressed* content —
        # same primitive as every other format, just a different reader.
        f = tmp_path / "ELDC0410.26d.Z"
        f.write_bytes(base64.b64decode(DOT_Z_B64))
        assert content_sha256(f) == DOT_Z_PLAIN_SHA

    def test_dot_z_equals_plain_twin(self, tmp_path):
        # The compression-invariant guarantee extends to .Z: a .d.Z RINEX hashes
        # identically to its decompressed .d twin.
        z = tmp_path / "x.d.Z"
        z.write_bytes(base64.b64decode(DOT_Z_B64))
        plain = _write(tmp_path / "x.d", DOT_Z_PLAIN)
        assert content_sha256(z) == content_sha256(plain)

    def test_truncated_dot_z_never_passes_as_intact(self, tmp_path):
        # LZW .Z has no CRC/length trailer, so gzip may decode a truncated .Z to
        # partial output with a zero exit. Either way a truncated file must NOT
        # yield the intact hash: it raises (gzip hard error) or it hashes
        # differently — the mismatch is what the verify pass catches.
        truncated = tmp_path / "trunc.d.Z"
        truncated.write_bytes(base64.b64decode(DOT_Z_B64)[:120])
        try:
            assert content_sha256(truncated) != DOT_Z_PLAIN_SHA
        except CorruptArchiveFileError:
            pass  # acceptable: gzip flagged it outright


class TestUnsupportedFormats:
    def test_zstd_raises_not_implemented(self, tmp_path):
        # zstd magic 0x28 0xb5 0x2f 0xfd — not used in the GNSS archive.
        f = tmp_path / "weird.zst"
        f.write_bytes(b"\x28\xb5\x2f\xfd" + b"\x00" * 32)
        with pytest.raises(NotImplementedError):
            content_sha256(f)


class TestEmptyAndPlain:
    def test_empty_file_hashes_as_empty(self, tmp_path):
        f = _write(tmp_path / "empty", b"")
        assert content_sha256(f) == hashlib.sha256(b"").hexdigest()
