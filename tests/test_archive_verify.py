"""Tests for the archive_catalog verify pass (receivers.archive.verify).

Read-back is the load-bearing check: re-hash the actual archive file and compare
to the stored content_sha256. These tests seed catalog rows pointing at real
files under a tmp 'archive mount' and assert verified / corrupt / missing.
"""

import gzip

import pytest

from receivers.archive.verify import (
    _local_archive_path,
    _local_session,
    verify_archive_catalog,
)
from receivers.utils.content_hash import content_sha256


# ----------------------------------------------------------------- pure units
class TestPathHelpers:
    def test_local_session_maps_rinex_suffix(self):
        assert _local_session("15s_24hr", "raw") == "15s_24hr"
        assert _local_session("15s_24hr", "rinex") == "15s_24hr_rinex"

    def test_prefix_swap(self):
        p = _local_archive_path(
            "~/gpsdata/2026/jun/ELDC/15s_24hr/raw/ELDC_a.sbf.gz",
            dest_prefix="~/gpsdata",
            read_root="/mnt/rawgpsdata",
        )
        assert p == "/mnt/rawgpsdata/2026/jun/ELDC/15s_24hr/raw/ELDC_a.sbf.gz"

    def test_fallback_after_gpsdata_marker(self):
        # dest_prefix does not match → split after the last 'gpsdata/'.
        p = _local_archive_path(
            "/home/gpsops/gpsdata/2026/jun/X/raw/f.Z",
            dest_prefix="~/gpsdata",
            read_root="/mnt/rawgpsdata",
        )
        assert p == "/mnt/rawgpsdata/2026/jun/X/raw/f.Z"


# ------------------------------------------------------------ DB-backed verify
def _local_conn():
    try:
        from receivers.db.connection import get_connection

        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.archive_catalog')")
            if cur.fetchone()[0] is None:
                conn.close()
                return None
        return conn
    except Exception:
        return None


@pytest.fixture
def db_conn():
    conn = _local_conn()
    if conn is None:
        pytest.skip("local gps_health with archive_catalog not available")
    yield conn
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM archive_catalog WHERE storage_location = 'test_verify'"
        )
    conn.commit()
    conn.close()


def _seed_gz(tmp_root, rel, payload):
    """Write a gzip file under tmp_root/rel; return (abs_path, content_sha256)."""
    path = tmp_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(path, "wb") as fh:
        fh.write(payload)
    return path, content_sha256(path)


def _seed_catalog(conn, rel, digest, *, station="ELDC", category="raw"):
    from receivers.archive.catalog import upsert_catalog_row

    upsert_catalog_row(
        conn,
        storage_location="test_verify",
        station=station,
        session_type="15s_24hr",
        file_category=category,
        file_date=None,
        archive_path=f"~/gpsdata/{rel}",
        filename=rel.split("/")[-1],
        file_size=123,
        content_sha256=digest,
    )
    conn.commit()


class TestReadBackVerify:
    def test_intact_file_verifies_and_stamps(self, db_conn, tmp_path):
        rel = "2026/jun/ELDC/15s_24hr/raw/ELDC_a.sbf.gz"
        _, digest = _seed_gz(tmp_path, rel, b"intact raw payload" * 100)
        _seed_catalog(db_conn, rel, digest)

        stats = verify_archive_catalog(
            db_conn,
            storage_location="test_verify",
            read_root=str(tmp_path),
            dest_prefix="~/gpsdata",
        )
        assert stats.verified == 1
        assert stats.mismatched == 0
        # last_verified_at stamped
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT last_verified_at FROM archive_catalog "
                "WHERE storage_location='test_verify'"
            )
            assert cur.fetchone()[0] is not None

    def test_corrupt_archive_file_flags_mismatch(self, db_conn, tmp_path):
        rel = "2026/jun/ELDC/15s_24hr/raw/ELDC_b.sbf.gz"
        path, digest = _seed_gz(tmp_path, rel, b"original payload" * 100)
        _seed_catalog(db_conn, rel, digest)
        # Tamper the archive file AFTER cataloging its hash.
        with gzip.GzipFile(path, "wb") as fh:
            fh.write(b"TAMPERED payload" * 100)

        stats = verify_archive_catalog(
            db_conn,
            storage_location="test_verify",
            read_root=str(tmp_path),
            dest_prefix="~/gpsdata",
        )
        assert stats.mismatched == 1
        assert stats.verified == 0
        assert any("ARCHIVE CORRUPT" in f for f in stats.findings)

    def test_missing_archive_file(self, db_conn, tmp_path):
        rel = "2026/jun/ELDC/15s_24hr/raw/ELDC_c.sbf.gz"
        _, digest = _seed_gz(tmp_path, rel, b"payload" * 100)
        _seed_catalog(db_conn, rel, digest)
        (tmp_path / rel).unlink()  # gone from the archive mount

        stats = verify_archive_catalog(
            db_conn,
            storage_location="test_verify",
            read_root=str(tmp_path),
            dest_prefix="~/gpsdata",
        )
        assert stats.missing == 1
        assert stats.verified == 0

    def test_cross_check_only_without_read_root(self, db_conn, tmp_path):
        rel = "2026/jun/ELDC/15s_24hr/raw/ELDC_d.sbf.gz"
        _, digest = _seed_gz(tmp_path, rel, b"payload" * 100)
        _seed_catalog(db_conn, rel, digest)

        stats = verify_archive_catalog(db_conn, storage_location="test_verify")
        assert stats.read_back is False
        assert stats.checked == 1
        assert stats.verified == 0  # no read-back performed


class TestParallelReadBack:
    """workers>1 pre-hashes on a thread pool — outcomes must match serial."""

    def test_parallel_matches_serial(self, db_conn, tmp_path):
        rels = []
        for i, payload in enumerate(
            (b"intact one" * 50, b"intact two" * 50, b"intact three" * 50)
        ):
            rel = f"2026/jun/ELDC/15s_24hr/raw/ELDC_p{i}.sbf.gz"
            _, digest = _seed_gz(tmp_path, rel, payload)
            _seed_catalog(db_conn, rel, digest)
            rels.append(rel)
        # one corrupt + one missing
        rel_bad = "2026/jun/ELDC/15s_24hr/raw/ELDC_bad.sbf.gz"
        path, digest = _seed_gz(tmp_path, rel_bad, b"good" * 50)
        _seed_catalog(db_conn, rel_bad, digest)
        with gzip.GzipFile(path, "wb") as fh:
            fh.write(b"tampered" * 50)
        rel_missing = "2026/jun/ELDC/15s_24hr/raw/ELDC_gone.sbf.gz"
        _seed_catalog(db_conn, rel_missing, "f" * 64)

        stats = verify_archive_catalog(
            db_conn,
            storage_location="test_verify",
            read_root=str(tmp_path),
            dest_prefix="~/gpsdata",
            workers=3,
        )
        assert stats.checked == 5
        assert stats.verified == 3
        assert stats.mismatched == 1
        assert stats.missing == 1
