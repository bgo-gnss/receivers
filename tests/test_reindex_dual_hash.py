"""Generated-file tests for the real reindex/upsert path; never connect to a DB.

The small SQL model preserves COALESCE for NULL incoming hashes, rather than
pretending that an omitted compressed hash replaced the stored value.
"""

import gzip
import hashlib
import shutil
import subprocess

import pytest

from receivers.archive import reindex as rx

REL = "2026/jun/TEST/15s_24hr/rinex/TEST1720.26D.gz"
KEY = ("fixture_archive", "15s_24hr", "rinex", "test1720.26d")


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.conn.statements.append((sql, params))
        if sql.lstrip().startswith("SELECT"):
            value = self.conn.rows.get(tuple(params))
            if value is None:
                self.row = None
            elif "compressed_sha256" in sql.partition("FROM")[0]:
                self.row = value
            else:
                self.row = (value[0],)
            return
        assert sql.lstrip().startswith("INSERT INTO archive_catalog")
        assert "COALESCE(EXCLUDED.compressed_sha256" in sql
        key = (params[0], params[4], params[5], params[6])
        prior = self.conn.rows.get(key, (None, None))
        self.conn.rows[key] = (
            params[10] if params[10] is not None else prior[0],
            params[11] if params[11] is not None else prior[1],
        )

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, prior=None):
        self.rows = {} if prior is None else {KEY: prior}
        self.statements = []
        self.commits = 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1


def product(tmp_path):
    payload = b"generated observation fixture\n" * 20
    path = tmp_path / REL
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.compress(payload, mtime=1))
    return (
        path,
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def run(conn, path, root, **kwargs):
    return rx.reindex_files(
        conn,
        [str(path)],
        root=str(root),
        storage_location=KEY[0],
        dest_prefix="~/gpsdata",
        **kwargs,
    )


@pytest.mark.parametrize("prior_blob", [None, "0" * 64])
def test_matching_content_still_repairs_missing_or_stale_blob_hash(
    tmp_path, prior_blob
):
    path, content, blob = product(tmp_path)
    conn = Connection((content, prior_blob))
    stats = run(conn, path, tmp_path)
    assert (stats.updated, stats.inserted, stats.unchanged) == (1, 0, 0)
    assert conn.rows[KEY] == (content, blob)
    assert conn.commits == 1


def test_both_matching_hashes_are_a_true_noop(tmp_path):
    path, content, blob = product(tmp_path)
    conn = Connection((content, blob))
    stats = run(conn, path, tmp_path)
    assert (stats.updated, stats.inserted, stats.unchanged) == (0, 0, 1)
    assert conn.commits == 0
    assert not any(sql.lstrip().startswith("INSERT") for sql, _ in conn.statements)


def test_checksum_bound_lzw_readback_uses_actual_open_descriptor(tmp_path):
    if shutil.which("compress") is None:
        pytest.skip("local compress executable unavailable")
    payload = b"generated LZW observation fixture\n" * 40
    relative = REL.replace(".gz", ".Z")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    compressed = subprocess.run(
        ["compress", "-c"], input=payload, capture_output=True, check=True
    ).stdout
    path.write_bytes(compressed)
    blob = hashlib.sha256(compressed).hexdigest()
    conn = Connection()
    stats = run(conn, path, tmp_path, expected_checksums={relative: blob})
    assert not stats.errors and stats.inserted == 1
    assert conn.rows[KEY] == (hashlib.sha256(payload).hexdigest(), blob)


def test_empty_approval_map_never_falls_back_to_unbound_hashing(tmp_path):
    path, _, _ = product(tmp_path)
    conn = Connection()
    stats = run(conn, path, tmp_path, expected_checksums={})
    assert stats.errors and not conn.statements and conn.commits == 0


def test_insert_populates_both_actual_hashes(tmp_path):
    path, content, blob = product(tmp_path)
    conn = Connection()
    stats = run(conn, path, tmp_path)
    assert stats.inserted == 1
    assert conn.rows[KEY] == (content, blob)


def test_existing_null_content_is_updated_not_misreported_as_inserted(tmp_path):
    path, content, blob = product(tmp_path)
    conn = Connection((None, None))
    stats = run(conn, path, tmp_path)
    assert (stats.updated, stats.inserted) == (1, 0)
    assert conn.rows[KEY] == (content, blob)


def test_compression_only_change_is_not_skipped(tmp_path):
    path, content, old_blob = product(tmp_path)
    conn = Connection((content, old_blob))
    path.write_bytes(gzip.compress(gzip.decompress(path.read_bytes()), mtime=2))
    blob = hashlib.sha256(path.read_bytes()).hexdigest()
    assert blob != old_blob
    stats = run(conn, path, tmp_path)
    assert stats.updated == 1
    assert conn.rows[KEY] == (content, blob)


def test_dry_run_reports_repair_without_insert_update_or_commit(tmp_path, caplog):
    path, content, blob = product(tmp_path)
    caplog.set_level("INFO", logger=rx.__name__)
    prior = (content, "0" * 64)
    conn = Connection(prior)
    stats = run(conn, path, tmp_path, dry_run=True)
    assert (stats.updated, stats.inserted, stats.unchanged) == (1, 0, 0)
    assert conn.rows[KEY] == prior
    assert conn.commits == 0
    assert all(sql.lstrip().startswith("SELECT") for sql, _ in conn.statements)
    assert f"content={content[:12]}" in caplog.text
    assert f"compressed={blob[:12]}" in caplog.text


def test_only_existing_does_not_insert(tmp_path):
    path, _, _ = product(tmp_path)
    conn = Connection()
    stats = run(conn, path, tmp_path, only_existing=True)
    assert stats.skipped_new == 1
    assert conn.rows == {}
    assert conn.commits == 0


def test_corrupt_file_is_not_blessed_with_a_blob_hash(tmp_path):
    path, _, _ = product(tmp_path)
    path.write_bytes(b"\x1f\x8bcorrupt generated gzip")
    conn = Connection()
    stats = run(conn, path, tmp_path)
    assert stats.errors and stats.touched == 0
    assert not conn.rows and conn.commits == 0


def test_blob_read_failure_has_no_catalog_effect(tmp_path, monkeypatch):
    path, _, _ = product(tmp_path)

    def fail(_):
        raise OSError("generated read failure")

    monkeypatch.setattr(rx, "compressed_sha256", fail)
    conn = Connection()
    stats = run(conn, path, tmp_path)
    assert stats.errors and stats.touched == 0
    assert not conn.rows and conn.commits == 0


def test_file_changed_between_hashes_is_refused(tmp_path, monkeypatch):
    path, _, _ = product(tmp_path)
    real = rx.compressed_sha256

    def mutate(name):
        path.write_bytes(gzip.compress(b"different generated fixture", mtime=3))
        return real(name)

    monkeypatch.setattr(rx, "compressed_sha256", mutate)
    conn = Connection()
    stats = run(conn, path, tmp_path)
    assert stats.errors and stats.touched == 0
    assert not conn.rows and conn.commits == 0
