"""Tests for archive_catalog reindex (receivers.archive.reindex).

Reindex re-hashes a local staging-mirror file and upserts its catalog row so the
stored content_sha256 matches archive bytes edited out-of-band (the
``rinex --fix-headers --push`` case). These tests seed rows in a local
gps_health and assert updated / inserted / unchanged / only_existing behaviour.
"""

import gzip

import pytest

from receivers.archive.reindex import reindex_files
from receivers.utils.content_hash import content_sha256

REL = "2026/jun/ELDC/15s_24hr/raw/ELDC_a.sbf.gz"
LOC = "test_reindex"


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
        cur.execute("DELETE FROM archive_catalog WHERE storage_location = %s", (LOC,))
    conn.commit()
    conn.close()


def _seed_gz(root, rel, payload):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(path, "wb") as fh:
        fh.write(payload)
    return path, content_sha256(path)


def _seed_row(conn, rel, digest):
    from receivers.archive.catalog import upsert_catalog_row

    upsert_catalog_row(
        conn,
        storage_location=LOC,
        station="ELDC",
        session_type="15s_24hr",
        file_category="raw",
        file_date=None,
        archive_path=f"~/gpsdata/{rel}",
        filename=rel.split("/")[-1],
        file_size=1,
        content_sha256=digest,
    )
    conn.commit()


def _catalog_sha(conn, key):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content_sha256 FROM archive_catalog "
            "WHERE storage_location=%s AND canonical_key=%s",
            (LOC, key),
        )
        row = cur.fetchone()
    return row[0] if row else None


def test_updated_refreshes_stale_hash(db_conn, tmp_path):
    # Seed a row with a wrong (stale) hash, then reindex the real file.
    _seed_row(db_conn, REL, "0" * 64)
    _, real = _seed_gz(tmp_path, REL, b"corrected header payload" * 50)

    stats = reindex_files(
        db_conn, [str(tmp_path / REL)], root=str(tmp_path),
        storage_location=LOC, dest_prefix="~/gpsdata",
    )
    assert (stats.updated, stats.inserted, stats.unchanged) == (1, 0, 0)
    assert _catalog_sha(db_conn, "eldc_a.sbf") == real


def test_inserted_when_no_prior_row(db_conn, tmp_path):
    _, real = _seed_gz(tmp_path, REL, b"brand new file" * 50)
    stats = reindex_files(
        db_conn, [str(tmp_path / REL)], root=str(tmp_path),
        storage_location=LOC, dest_prefix="~/gpsdata",
    )
    assert (stats.updated, stats.inserted, stats.unchanged) == (0, 1, 0)
    assert _catalog_sha(db_conn, "eldc_a.sbf") == real


def test_only_existing_skips_insert(db_conn, tmp_path):
    _seed_gz(tmp_path, REL, b"unseen file" * 50)
    stats = reindex_files(
        db_conn, [str(tmp_path / REL)], root=str(tmp_path),
        storage_location=LOC, dest_prefix="~/gpsdata", only_existing=True,
    )
    assert (stats.updated, stats.inserted, stats.skipped_new) == (0, 0, 1)
    assert _catalog_sha(db_conn, "eldc_a.sbf") is None  # nothing written


def test_unchanged_when_hash_matches(db_conn, tmp_path):
    _, real = _seed_gz(tmp_path, REL, b"same content" * 50)
    _seed_row(db_conn, REL, real)
    stats = reindex_files(
        db_conn, [str(tmp_path / REL)], root=str(tmp_path),
        storage_location=LOC, dest_prefix="~/gpsdata",
    )
    assert (stats.updated, stats.inserted, stats.unchanged) == (0, 0, 1)


def test_dry_run_writes_nothing(db_conn, tmp_path):
    _seed_row(db_conn, REL, "0" * 64)
    _seed_gz(tmp_path, REL, b"would-fix payload" * 50)
    stats = reindex_files(
        db_conn, [str(tmp_path / REL)], root=str(tmp_path),
        storage_location=LOC, dest_prefix="~/gpsdata", dry_run=True,
    )
    assert stats.updated == 1
    assert _catalog_sha(db_conn, "eldc_a.sbf") == "0" * 64  # unchanged on disk


def test_unparsable_path_skipped(db_conn, tmp_path):
    # A file not in archive layout can't be parsed to an identity.
    stray = tmp_path / "stray.txt"
    stray.write_text("nope")
    stats = reindex_files(
        db_conn, [str(stray)], root=str(tmp_path),
        storage_location=LOC, dest_prefix="~/gpsdata",
    )
    assert stats.skipped == 1
    assert stats.touched == 0


# ---- _push_reindex glue (the `rinex --fix-headers --push --reindex` entry) ---
class TestPushReindexGlue:
    """The flag entry point wires args + staged paths into reindex_files."""

    def test_no_flag_prints_hint_only(self, capsys):
        import types

        from receivers.cli.main import _push_reindex

        args = types.SimpleNamespace(reindex=False, catalog_host=None)
        # No DB touched; must not raise and must point at the reindex verb.
        _push_reindex(
            args, ["/whatever"], root="/tmp/x",
            storage_location="imo_archive", dest_prefix="~/gpsdata",
        )
        out = capsys.readouterr().out
        assert "archive-reindex" in out
        assert "archive-sync --status" not in out

    def test_flag_reindexes_staged_file(self, db_conn, tmp_path, capsys):
        import types

        from receivers.cli.main import _push_reindex

        _seed_row(db_conn, REL, "0" * 64)          # stale row
        _seed_gz(tmp_path, REL, b"pushed corrected payload" * 50)
        args = types.SimpleNamespace(reindex=True, catalog_host=None)  # localhost
        _push_reindex(
            args, [str(tmp_path / REL)], root=str(tmp_path),
            storage_location=LOC, dest_prefix="~/gpsdata",
        )
        out = capsys.readouterr().out
        assert "reindexed archive_catalog" in out
        assert "1 updated" in out
        # Row now holds the real hash, not the stale zeros.
        assert _catalog_sha(db_conn, "eldc_a.sbf") != "0" * 64

    def test_flag_missing_storage_location_skips(self, capsys):
        import types

        from receivers.cli.main import _push_reindex

        args = types.SimpleNamespace(reindex=True, catalog_host=None)
        _push_reindex(
            args, ["/whatever"], root="/tmp/x",
            storage_location=None, dest_prefix=None,
        )
        assert "no archive storage_location" in capsys.readouterr().out
