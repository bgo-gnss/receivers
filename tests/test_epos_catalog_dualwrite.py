"""Tests for the EPOS → archive_catalog dual-write (dissemination/job.py).

At push time epos-disseminate writes an
``archive_catalog(storage_location='epos_portal')`` row carrying both sha256s + both
EPOS md5s; since the gnss-europe DB was retired (2026-11) this is the only index of
the portal. The write is best-effort: it must never raise into the sweep.
"""

import contextlib
from datetime import date
from pathlib import Path
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
        "receivers.dissemination.rinex_md5.rinex_md5s",
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

    monkeypatch.setattr("receivers.dissemination.rinex_md5.rinex_md5s", _boom)
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


def _capture_catalog(monkeypatch):
    _patch_hashers(monkeypatch)
    rows = []
    monkeypatch.setattr(
        "receivers.archive.catalog.upsert_catalog_row",
        lambda conn, **kw: rows.append(kw),
    )
    conn = _FakeConn()
    monkeypatch.setattr(job, "_catalog_conn_factory", lambda: conn)
    monkeypatch.setattr(job, "_catalog_conn", None)
    return rows, conn


def _assert_fully_hashed(row):
    assert row["storage_location"] == "epos_portal"
    assert row["content_sha256"] == "c" * 64
    assert row["compressed_sha256"] == "z" * 64
    assert row["md5checksum"] == "md5checksum_val"
    assert row["md5uncompressed"] == "md5uncompressed_val"


def _sync_yaml(tmp_path):
    cfg = tmp_path / "sync.yaml"
    cfg.write_text(
        "targets:\n"
        "  - name: epos\n"
        "    active: true\n"
        "    tier: dissemination\n"
        "    host: ''\n"
        "    user: u\n"
        f"    dest: {tmp_path}/dest\n"
        "    source_root: /tmp/src\n"
    )
    return cfg


class _StagingEngine:
    """Stages one real file per date into the batcher, as ``run_one`` does."""

    dest_override = None
    dry_run = False

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.pusher = None

    def run_one(self, station, d):
        r = _result(self.tmp_path, station=station, file_date=d, ok=True, cached=False)
        self.pusher.add(r.artifact_path, "2026/001", Path(r.artifact_path).name, ref=r)
        return r


def test_sweep_catalogs_a_file_once_its_batch_is_durable(monkeypatch, tmp_path):
    # The real path: run_one stages -> BatchPush flushes -> on_flush -> catalog row.
    rows, conn = _capture_catalog(monkeypatch)
    monkeypatch.setattr(
        "receivers.utils.net_push.rsync_tree",
        lambda *a, **k: {"transferred": 1, "seconds": 0.0},
    )

    summary = job.run_epos_disseminate_job(
        config_path=str(_sync_yaml(tmp_path)),
        no_qc=True,
        markers=["RHOF"],
        dates=[date(2026, 1, 1)],
        engine_factory=lambda t: _StagingEngine(tmp_path),
    )

    assert summary["pushed"] == 1 and summary["failed"] == 0
    assert "superseded" not in summary
    assert len(rows) == 1
    _assert_fully_hashed(rows[0])
    assert rows[0]["station"] == "RHOF"
    assert conn.committed is True


def test_sweep_writes_no_catalog_row_when_the_push_fails(monkeypatch, tmp_path):
    # rsync raising means the batch never became durable: the index must not
    # claim a file that did not land.
    rows, _ = _capture_catalog(monkeypatch)

    def _rsync_fails(*a, **k):
        raise RuntimeError("rsync failed")

    monkeypatch.setattr("receivers.utils.net_push.rsync_tree", _rsync_fails)

    # Whether the sequential driver re-raises is not what this test is about.
    with contextlib.suppress(RuntimeError):
        job.run_epos_disseminate_job(
            config_path=str(_sync_yaml(tmp_path)),
            no_qc=True,
            markers=["RHOF"],
            dates=[date(2026, 1, 1)],
            engine_factory=lambda t: _StagingEngine(tmp_path),
        )

    assert rows == []


def test_reactive_disseminate_catalogs_each_pushed_file(monkeypatch, tmp_path):
    from receivers.dissemination.reactive import NEW, StationChange

    rows, _ = _capture_catalog(monkeypatch)

    class _Engine:
        def run_one(self, station, d):
            return _result(
                tmp_path, station=station, file_date=d, ok=True, cached=False
            )

    target = SimpleNamespace(
        sessions=["15s_24hr"],
        cutover=None,
        format=SimpleNamespace(country_code="ISL", monument_number="00"),
    )
    actions = job._build_reactive_actions(
        target,
        session_provider=None,
        engine_factory=lambda _t: _Engine(),
        sitelogs_dir=str(tmp_path),
        backfill_days=1,
        today=date(2026, 1, 2),
    )
    try:
        assert actions.disseminate(StationChange("RHOF", NEW, None, None)) is True
    finally:
        job._close_catalog_conn()

    assert len(rows) == 2  # yesterday + 1 back: not today, its file is partial
    _assert_fully_hashed(rows[0])
