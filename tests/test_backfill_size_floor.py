"""archive-index-backfill must not catalogue stub files.

The walker hashed EVERY file with no minimum-size guard, so 0-byte and 3-byte
stub RINEX (``1f 9d 90`` — what ``compress`` emits for empty input) were
catalogued as legitimate archive members. Every one of the ~14k phantom
``archive_catalog`` rows carries the SHA-256 of the EMPTY STRING and no other
hash — because ``content_sha256`` is over DECOMPRESSED content and a stub
decompresses to nothing. That is the exact discriminator these tests pin.

Two guards, counted separately: a raw-size floor (cheap, never opens the
file) and the exact empty-content test (the real one). ``include_stubs``
disables both. No database: the connection layer and the catalog upsert are
stubbed the way ``test_archive_index_backfill.py`` does it.
"""

from __future__ import annotations

import gzip
import hashlib

import pytest

import receivers.archive.reindex as rx
from receivers.archive import DEFAULT_MIN_ARCHIVE_FILE_BYTES
from receivers.archive.reindex import BackfillStats, backfill_archive_catalog
from receivers.utils.content_hash import EMPTY_CONTENT_SHA256, content_sha256

EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
RINEX_DIR = "2015/jun/ARHO/15s_24hr/rinex"


class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a):
        pass

    def fetchall(self):
        return []  # nothing pre-existing → every file "needs" indexing


class _Conn:
    def cursor(self):
        return _Cur()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def upserts(monkeypatch):
    """Stub the DB; return the list of upserted (filename, content_sha256)."""
    monkeypatch.setattr(
        "receivers.db.connection.get_connection", lambda host_override=None: _Conn()
    )
    rows: list[tuple[str, str]] = []
    monkeypatch.setattr(
        rx,
        "upsert_catalog_row",
        lambda conn, **kw: rows.append((kw["filename"], kw["content_sha256"])),
    )
    return rows


def _write(root, name: str, data: bytes):
    p = root / RINEX_DIR / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _real_product(root, name="ARHO1550.15D.Z"):
    """A legitimately small but non-empty product (gzip of ~1 KB of content).

    Incompressible bytes on purpose: repetitive text would gzip to well under
    the floor and turn this fixture into a stub.
    """
    payload = b"".join(hashlib.sha256(bytes([i])).digest() for i in range(40))
    p = root / RINEX_DIR / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(p, "wb") as fh:
        fh.write(payload)
    assert p.stat().st_size >= DEFAULT_MIN_ARCHIVE_FILE_BYTES
    return p


def _run(root, files, **kw) -> BackfillStats:
    return backfill_archive_catalog(
        [None],
        [str(f) for f in files],
        root=str(root),
        storage_location="imo_archive",
        dest_prefix="~/gpsdata",
        **kw,
    )


# --------------------------------------------------------------------------
# the shared constant


class TestEmptyContentConstant:
    def test_is_the_sha256_of_zero_bytes(self):
        assert EMPTY_CONTENT_SHA256 == hashlib.sha256(b"").hexdigest() == EMPTY_SHA

    def test_every_stub_form_hashes_to_it(self, tmp_path):
        zero = _write(tmp_path, "zero.15D.Z", b"")
        compress_hdr = _write(tmp_path, "three.15D.Z", b"\x1f\x9d\x90")
        gz_empty = _write(tmp_path, "gz.15o.gz", gzip.compress(b""))
        for p in (zero, compress_hdr, gz_empty):
            assert content_sha256(p) == EMPTY_CONTENT_SHA256, p.name

    def test_a_real_product_does_not(self, tmp_path):
        assert content_sha256(_real_product(tmp_path)) != EMPTY_CONTENT_SHA256


# --------------------------------------------------------------------------
# the guards


class TestSizeFloor:
    def test_three_byte_stub_is_skipped_and_counted(self, tmp_path, upserts):
        stub = _write(tmp_path, "ARHO1550.15D.Z", b"\x1f\x9d\x90")
        stats = _run(tmp_path, [stub])
        assert upserts == []
        assert stats.hashed == 0
        assert stats.skipped_small == 1
        assert stats.skipped_empty == 0

    def test_zero_byte_stub_is_skipped_and_counted(self, tmp_path, upserts):
        stub = _write(tmp_path, "ARHO1570.15D.Z", b"")
        stats = _run(tmp_path, [stub])
        assert upserts == []
        assert stats.skipped_small == 1
        assert stats.skipped_empty == 0

    def test_floor_never_opens_the_file(self, tmp_path, upserts, monkeypatch):
        opened = []
        monkeypatch.setattr(
            rx, "content_sha256", lambda f: opened.append(f) or "c" * 64
        )
        stub = _write(tmp_path, "ARHO1600.15D.Z", b"\x1f\x9d\x90")
        _run(tmp_path, [stub])
        assert opened == []

    def test_default_floor_is_the_measured_constant(self):
        assert DEFAULT_MIN_ARCHIVE_FILE_BYTES == 128


class TestExactGuard:
    def test_gzip_of_nothing_above_the_floor_is_caught_by_content(
        self, tmp_path, upserts
    ):
        # A gzip-of-empty is 20 B (+ a stored name, when the writer records
        # one — the 33 B syrf2440.21o.gz on the archive). Disable the floor so
        # ONLY the exact guard can catch it.
        name = "SYRF00ISL_R_20212440000_01D_15S_MO.crx.gz"
        p = _write(tmp_path, name, gzip.compress(b""))
        stats = _run(tmp_path, [p], min_size_bytes=0)
        assert content_sha256(p) == EMPTY_CONTENT_SHA256
        assert upserts == []
        assert stats.skipped_empty == 1
        assert stats.skipped_small == 0

    def test_three_byte_stub_with_floor_disabled_is_still_caught(
        self, tmp_path, upserts
    ):
        stub = _write(tmp_path, "ARHO1650.15D.Z", b"\x1f\x9d\x90")
        stats = _run(tmp_path, [stub], min_size_bytes=0)
        assert upserts == []
        assert stats.skipped_small == 0
        assert stats.skipped_empty == 1

    def test_exact_guard_costs_one_read_not_two(self, tmp_path, upserts, monkeypatch):
        zsha_calls = []
        monkeypatch.setattr(
            rx, "compressed_sha256", lambda f: zsha_calls.append(f) or "z" * 64
        )
        stub = _write(tmp_path, "ARHO1710.15D.Z", b"\x1f\x9d\x90")
        _run(tmp_path, [stub], min_size_bytes=0)
        assert zsha_calls == []  # the compressed hash is never computed for a stub


class TestNormalFilesAndEscapeHatch:
    def test_a_normal_file_is_indexed(self, tmp_path, upserts):
        p = _real_product(tmp_path)
        stats = _run(tmp_path, [p])
        assert stats.hashed == 1
        assert stats.skipped_stubs == 0
        assert upserts == [(p.name, content_sha256(p))]
        assert upserts[0][1] != EMPTY_CONTENT_SHA256

    def test_include_stubs_indexes_the_stub(self, tmp_path, upserts):
        stub = _write(tmp_path, "ARHO1550.15D.Z", b"\x1f\x9d\x90")
        zero = _write(tmp_path, "ARHO1570.15D.Z", b"")
        stats = _run(tmp_path, [stub, zero], include_stubs=True)
        assert stats.hashed == 2
        assert stats.skipped_small == 0 and stats.skipped_empty == 0
        assert [r[1] for r in upserts] == [EMPTY_CONTENT_SHA256] * 2

    def test_skip_reasons_are_counted_separately(self, tmp_path, upserts):
        small = _write(tmp_path, "ARHO1550.15D.Z", b"\x1f\x9d\x90")  # floor
        zero = _write(tmp_path, "ARHO1570.15D.Z", b"")  # floor
        big_empty = _write(  # 20 B: past a 16 B floor, empty content
            tmp_path, "ARHO00ISL_R_20151600000_01D_15S_MO.crx.gz", gzip.compress(b"")
        )
        real = _real_product(tmp_path, "ARHO1620.15D.Z")
        stats = _run(tmp_path, [small, zero, big_empty, real], min_size_bytes=16)
        assert stats.skipped_small == 2
        assert stats.skipped_empty == 1
        assert stats.skipped_stubs == 3
        assert stats.hashed == 1
        assert [r[0] for r in upserts] == [real.name]
        d = stats.to_dict()
        assert d["skipped_small"] == 2 and d["skipped_empty"] == 1

    def test_dry_run_applies_the_floor_only(self, tmp_path, upserts):
        stub = _write(tmp_path, "ARHO1550.15D.Z", b"\x1f\x9d\x90")
        real = _real_product(tmp_path, "ARHO1620.15D.Z")
        stats = _run(tmp_path, [stub, real], dry_run=True)
        assert stats.skipped_small == 1
        assert stats.skipped_empty == 0  # needs the decompress; not done in dry-run
        assert stats.hashed == 1
        assert upserts == []


class TestCliWiring:
    def test_backfill_parser_has_both_flags(self):
        import argparse

        from receivers.cli.archive_sync import create_archive_index_backfill_parser

        sub = argparse.ArgumentParser().add_subparsers()
        p = create_archive_index_backfill_parser(sub)
        ns = p.parse_args(["--dir", "/x"])
        assert ns.min_size_bytes == DEFAULT_MIN_ARCHIVE_FILE_BYTES
        assert ns.include_stubs is False
        ns = p.parse_args(["--dir", "/x", "--include-stubs", "--min-size-bytes", "0"])
        assert ns.include_stubs is True and ns.min_size_bytes == 0
