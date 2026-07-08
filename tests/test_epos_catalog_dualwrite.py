"""Tests for the EPOS → archive_catalog dual-write (dissemination/job.py).

At push time epos-disseminate writes BOTH the legacy EPOS ``rinex_file`` (external
contract, unchanged) AND an ``archive_catalog(storage_location='epos_portal')``
row carrying both sha256s + both EPOS md5s — so the one unified index gains the
portal tier. The write is best-effort: it must never raise into the sweep.
"""

from datetime import date
from types import SimpleNamespace

import receivers.dissemination.job as job


class _FakeConn:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True

    def close(self):
        pass


def _result(tmp_path, **over):
    f = tmp_path / "RHOF00ISL_R_20260010000_01D_15S_MO.crx.gz"
    f.write_bytes(b"portal file bytes")
    base = dict(
        artifact_path=str(f),
        station="RHOF",
        file_date=date(2026, 1, 1),
        relative_path="2026/001/RHOF00ISL_R_20260010000_01D_15S_MO.crx.gz",
        dry_run=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _patch_hashers(monkeypatch):
    monkeypatch.setattr(
        "receivers.dissemination.rinex_index.rinex_md5s",
        lambda p: ("md5checksum_val", "md5uncompressed_val"),
    )
    monkeypatch.setattr(
        "receivers.utils.content_hash.content_sha256", lambda p: "c" * 64
    )
    monkeypatch.setattr(
        "receivers.utils.content_hash.compressed_sha256", lambda p: "z" * 64
    )


def test_dual_write_upserts_epos_portal_with_all_four_hashes(monkeypatch, tmp_path):
    _patch_hashers(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        "receivers.archive.catalog.upsert_catalog_row",
        lambda conn, **kw: captured.update(kw),
    )
    conn = _FakeConn()
    monkeypatch.setattr(job, "_catalog_conn_factory", lambda: conn)
    monkeypatch.setattr(job, "_catalog_conn", None)

    target = SimpleNamespace(sessions=["15s_24hr"])
    try:
        job._catalog_epos_push(target, _result(tmp_path))
    finally:
        job._close_catalog_conn()

    assert captured["storage_location"] == "epos_portal"
    assert captured["file_category"] == "rinex"
    assert captured["station"] == "RHOF"
    assert captured["content_sha256"] == "c" * 64
    assert captured["compressed_sha256"] == "z" * 64
    assert captured["md5checksum"] == "md5checksum_val"
    assert captured["md5uncompressed"] == "md5uncompressed_val"
    assert conn.committed is True


def test_dual_write_is_best_effort_never_raises(monkeypatch, tmp_path):
    # A hashing/DB failure must be swallowed (the sweep must not crash).
    def _boom(p):
        raise RuntimeError("hash blew up")

    monkeypatch.setattr("receivers.dissemination.rinex_index.rinex_md5s", _boom)
    monkeypatch.setattr(job, "_catalog_conn_factory", lambda: _FakeConn())
    monkeypatch.setattr(job, "_catalog_conn", None)
    # Must not raise:
    job._catalog_epos_push(SimpleNamespace(sessions=["15s_24hr"]), _result(tmp_path))


def test_dual_write_skips_dry_run_and_missing_file(monkeypatch, tmp_path):
    _patch_hashers(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "receivers.archive.catalog.upsert_catalog_row",
        lambda conn, **kw: calls.append(kw),
    )
    monkeypatch.setattr(job, "_catalog_conn_factory", lambda: _FakeConn())
    monkeypatch.setattr(job, "_catalog_conn", None)
    target = SimpleNamespace(sessions=["15s_24hr"])

    # dry-run → skip
    job._catalog_epos_push(target, _result(tmp_path, dry_run=True))
    # no artifact_path → skip
    job._catalog_epos_push(target, _result(tmp_path, artifact_path=None))
    # artifact_path points at a non-existent file → skip
    job._catalog_epos_push(target, _result(tmp_path, artifact_path="/no/such/file.gz"))

    assert calls == []  # nothing upserted for any skip case
