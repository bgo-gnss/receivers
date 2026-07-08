"""Tests for the one-time archive-index backfill (receivers.archive.reindex).

``backfill_archive_catalog`` bulk-indexes already-on-disk archive files into
``archive_catalog`` on every catalog host, hashing BOTH content_sha256 and
compressed_sha256. It is resumable (skips files already fully hashed) and
pausable (``limit`` caps files hashed per run). These tests exercise the real
local gps_health where available (both-hash population, resume, limit, dry-run,
unparsable) and mock the connection layer for the hash-once/fan-out and
connect-failure behaviour.
"""

import gzip

import pytest

import receivers.archive.reindex as rx
from receivers.archive.reindex import backfill_archive_catalog
from receivers.utils.content_hash import content_sha256

LOC = "test_backfill"


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


def _catalog_hashes(conn, key):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content_sha256, compressed_sha256 FROM archive_catalog "
            "WHERE storage_location=%s AND canonical_key=%s",
            (LOC, key),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


REL_A = "2026/jun/ELDC/15s_24hr/raw/ELDC_a.sbf.gz"
REL_B = "2026/jun/ELDC/1Hz_1hr/raw/ELDC_b.sbf.gz"


def test_indexes_new_files_with_both_hashes(db_conn, tmp_path):
    _, ca = _seed_gz(tmp_path, REL_A, b"alpha payload" * 40)
    _seed_gz(tmp_path, REL_B, b"beta payload" * 40)

    stats = backfill_archive_catalog(
        [None],
        [str(tmp_path / REL_A), str(tmp_path / REL_B)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/gpsdata",
    )
    assert stats.hashed == 2
    assert stats.skipped_done == 0
    assert stats.writes["localhost"]["ok"] == 2
    content, compressed = _catalog_hashes(db_conn, "eldc_a.sbf")
    assert content == ca  # decompressed-content hash
    assert compressed is not None and len(compressed) == 64  # on-disk-bytes hash too


def test_resume_skips_already_done(db_conn, tmp_path):
    _seed_gz(tmp_path, REL_A, b"one" * 40)
    _seed_gz(tmp_path, REL_B, b"two" * 40)
    files = [str(tmp_path / REL_A), str(tmp_path / REL_B)]

    first = backfill_archive_catalog(
        [None], files, root=str(tmp_path), storage_location=LOC, dest_prefix="~/g"
    )
    assert first.hashed == 2

    second = backfill_archive_catalog(
        [None], files, root=str(tmp_path), storage_location=LOC, dest_prefix="~/g"
    )
    assert second.hashed == 0
    assert second.skipped_done == 2  # both already fully hashed → the resume cursor


def test_limit_caps_files_hashed(db_conn, tmp_path):
    rels = [f"2026/jun/ELDC/15s_24hr/raw/ELDC_{i}.sbf.gz" for i in range(3)]
    for i, rel in enumerate(rels):
        _seed_gz(tmp_path, rel, f"file{i}".encode() * 40)
    files = [str(tmp_path / r) for r in rels]

    capped = backfill_archive_catalog(
        [None],
        files,
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/g",
        limit=2,
    )
    assert capped.hashed == 2  # bounded run

    rest = backfill_archive_catalog(
        [None], files, root=str(tmp_path), storage_location=LOC, dest_prefix="~/g"
    )
    assert rest.hashed == 1  # the third; the first two skip as already-done
    assert rest.skipped_done == 2


def test_dry_run_writes_nothing(db_conn, tmp_path):
    _seed_gz(tmp_path, REL_A, b"preview" * 40)
    stats = backfill_archive_catalog(
        [None],
        [str(tmp_path / REL_A)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/g",
        dry_run=True,
    )
    assert stats.hashed == 1  # counted as "would index"
    assert _catalog_hashes(db_conn, "eldc_a.sbf") == (None, None)  # nothing written


def test_unparsable_path_skipped(db_conn, tmp_path):
    stray = tmp_path / "not-archive-layout.txt"
    stray.write_text("nope")
    stats = backfill_archive_catalog(
        [None],
        [str(stray)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/g",
    )
    assert stats.skipped_parse == 1
    assert stats.hashed == 0


def test_hashes_once_and_fans_out_to_all_hosts(monkeypatch, tmp_path):
    """content_sha256 (the expensive decompress) runs ONCE per file even with
    multiple hosts; the row is upserted to every host that needs it."""

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            self._last = a[0]

        def fetchall(self):
            return []  # nothing pre-existing → both hosts "need" the file

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    conns = {"h1.is": _Conn(), "h2.is": _Conn()}
    monkeypatch.setattr(
        "receivers.db.connection.get_connection",
        lambda host_override=None: conns[host_override],
    )
    hash_calls = {"content": 0, "compressed": 0}
    monkeypatch.setattr(
        rx,
        "content_sha256",
        lambda f: (
            hash_calls.__setitem__("content", hash_calls["content"] + 1),
            "c" * 64,
        )[1],
    )
    monkeypatch.setattr(
        rx,
        "compressed_sha256",
        lambda f: (
            hash_calls.__setitem__("compressed", hash_calls["compressed"] + 1),
            "z" * 64,
        )[1],
    )
    upserts = []
    monkeypatch.setattr(
        rx,
        "upsert_catalog_row",
        lambda conn, **kw: upserts.append(kw["archive_path"]),
    )

    p = tmp_path / REL_A
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 32)

    stats = backfill_archive_catalog(
        ["h1.is", "h2.is"],
        [str(p)],
        root=str(tmp_path),
        storage_location="imo_archive",
        dest_prefix="~/gpsdata",
    )
    assert hash_calls == {"content": 1, "compressed": 1}  # hashed ONCE, not per host
    assert len(upserts) == 2  # written to both hosts
    assert stats.hashed == 1
    assert stats.writes["h1.is"]["ok"] == 1
    assert stats.writes["h2.is"]["ok"] == 1


def test_connect_failure_is_recorded_not_raised(monkeypatch):
    def _boom(host_override=None):
        raise OSError("connection refused")

    monkeypatch.setattr("receivers.db.connection.get_connection", _boom)
    stats = backfill_archive_catalog(
        ["bad.host"],
        ["/whatever"],
        root="/w",
        storage_location="imo_archive",
        dest_prefix="~/g",
    )
    assert stats.hashed == 0
    assert any("connect bad.host" in e for e in stats.errors)
    assert any("no catalog hosts reachable" in e for e in stats.errors)


def _seed_content_only_row(conn, filename, content_hash):
    """A row already content-hashed but WITHOUT compressed_sha256 — the state of
    the ~285k mature-archive rows from prior reindex/sync work."""
    from receivers.archive.catalog import upsert_catalog_row

    upsert_catalog_row(
        conn,
        storage_location=LOC,
        station="ELDC",
        session_type="15s_24hr",
        file_category="raw",
        file_date=None,
        file_hour=None,
        archive_path=f"~/g/{filename}",
        filename=filename,
        file_size=1,
        content_sha256=content_hash,
        compressed_sha256=None,
    )
    conn.commit()


def test_content_only_row_skipped_by_default(db_conn, tmp_path):
    """Primary pass: a row that already has content_sha256 (but no compressed) is
    SKIPPED — we do NOT re-read the whole archive for rows prior work hashed."""
    _, ca = _seed_gz(tmp_path, REL_A, b"already content-hashed" * 40)
    _seed_content_only_row(db_conn, "ELDC_a.sbf.gz", ca)

    stats = backfill_archive_catalog(
        [None],
        [str(tmp_path / REL_A)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/g",
    )
    assert stats.skipped_done == 1
    assert stats.hashed == 0  # not re-read


def test_refill_compressed_reprocesses_content_only_row(db_conn, tmp_path):
    """The deliberate later pass: --refill-compressed re-reads a content-only row
    to add the compressed_sha256 (EPOS-md5) counterpart."""
    _, ca = _seed_gz(tmp_path, REL_A, b"needs compressed hash" * 40)
    _seed_content_only_row(db_conn, "ELDC_a.sbf.gz", ca)
    assert _catalog_hashes(db_conn, "eldc_a.sbf")[1] is None  # compressed NULL

    stats = backfill_archive_catalog(
        [None],
        [str(tmp_path / REL_A)],
        root=str(tmp_path),
        storage_location=LOC,
        dest_prefix="~/g",
        require_compressed=True,
    )
    assert stats.hashed == 1  # re-processed
    content, compressed = _catalog_hashes(db_conn, "eldc_a.sbf")
    assert content == ca and compressed is not None  # compressed now filled


def _build_tree(root, stations, sessions):
    for sta in stations:
        for sess in sessions:
            d = root / "2015" / "jun" / sta / sess / "raw"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{sta}202506150000a.sbf.gz").write_bytes(b"x")


class TestIterArchiveFiles:
    """Station/session pruning for scoped runs (index just these stations)."""

    def _rel_stations(self, root, files):
        # station is component index 2 under root: YYYY/mon/STA/...
        import os

        return {os.path.relpath(f, str(root)).split(os.sep)[2] for f in files}

    def test_no_filter_yields_all(self, tmp_path):
        _build_tree(tmp_path, ["RHOF", "ELEY", "OLKE"], ["15s_24hr", "1Hz_1hr"])
        files = list(rx.iter_archive_files(str(tmp_path), root=str(tmp_path)))
        assert len(files) == 6

    def test_station_filter_prunes_walk(self, tmp_path):
        _build_tree(tmp_path, ["RHOF", "ELEY", "OLKE"], ["15s_24hr", "1Hz_1hr"])
        files = list(
            rx.iter_archive_files(
                str(tmp_path), root=str(tmp_path), stations={"RHOF", "ELEY"}
            )
        )
        assert len(files) == 4  # 2 stations × 2 sessions
        assert self._rel_stations(tmp_path, files) == {"RHOF", "ELEY"}

    def test_station_and_session_filter(self, tmp_path):
        _build_tree(tmp_path, ["RHOF", "ELEY", "OLKE"], ["15s_24hr", "1Hz_1hr"])
        files = list(
            rx.iter_archive_files(
                str(tmp_path),
                root=str(tmp_path),
                stations={"RHOF", "ELEY"},
                sessions={"15s_24hr"},
            )
        )
        assert len(files) == 2  # RHOF/15s + ELEY/15s
        assert all("15s_24hr" in f for f in files)

    def test_station_filter_is_case_insensitive(self, tmp_path):
        # dirs are upper-cased station IDs; the filter set is upper-cased by caller.
        _build_tree(tmp_path, ["RHOF", "OLKE"], ["15s_24hr"])
        files = list(
            rx.iter_archive_files(str(tmp_path), root=str(tmp_path), stations={"RHOF"})
        )
        assert len(files) == 1 and "RHOF" in files[0]

    def test_rinex_archive_backups_skipped(self, tmp_path):
        d = tmp_path / "2015" / "jun" / "RHOF" / "15s_24hr" / "rinex_archive"
        d.mkdir(parents=True)
        (d / "RHOF1660.15D.Z").write_bytes(b"x")
        files = list(rx.iter_archive_files(str(tmp_path), root=str(tmp_path)))
        assert files == []  # backup dir excluded

    def test_snapshot_dir_pruned(self, tmp_path):
        # NetApp/NFS ``.snapshot`` mirrors the whole archive under two extra
        # prefix dirs — the walk must never descend into it.
        _build_tree(tmp_path, ["RHOF"], ["15s_24hr"])  # 1 real file
        snap = (
            tmp_path
            / ".snapshot"
            / "Anti_ransomware_backup.2026-07-08_0809"
            / "2003"
            / "jan"
            / "RHOF"
            / "15s_24hr"
            / "rinex"
        )
        snap.mkdir(parents=True)
        (snap / "RHOF0010.03D.Z").write_bytes(b"x")
        files = list(rx.iter_archive_files(str(tmp_path), root=str(tmp_path)))
        assert len(files) == 1  # only the real file
        assert all("/.snapshot/" not in f for f in files)


class TestParseArchivePathGuards:
    """The year-slot guard rejects shifted (non-``YYYY/mon/STA/...``) paths."""

    def test_snapshot_path_rejected(self, tmp_path):
        from receivers.archive.path_parse import parse_archive_path

        p = str(
            tmp_path
            / ".snapshot"
            / "Anti_ransomware_backup.2026-07-08_0809"
            / "2003"
            / "jan"
            / "RHOF"
            / "15s_24hr"
            / "rinex"
            / "RHOF0010.03D.Z"
        )
        # Pre-guard this mis-parsed to station="2003"/session="jan"/category="RHOF".
        assert parse_archive_path(p, str(tmp_path)) is None

    def test_real_path_still_parses(self, tmp_path):
        from receivers.archive.path_parse import parse_archive_path

        p = str(
            tmp_path
            / "2026"
            / "apr"
            / "AKUR"
            / "15s_24hr"
            / "raw"
            / "AKUR202604070000a.T02.gz"
        )
        parsed = parse_archive_path(p, str(tmp_path))
        assert parsed is not None
        assert parsed.station == "AKUR"
        assert parsed.session_type == "15s_24hr"
        assert parsed.file_category == "raw"
