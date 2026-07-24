"""Tests for the routine catalog-write fan-out (rek-d01 + pgdev).

Regression guard for the divergence bug: the routine sync engine used to upsert
archive_catalog rows to a single default connection (localhost), so the mirror
(pgdev) only ever received explicit ``--catalog-prod`` reindex pushes and fell
~10x behind. ``ArchiveSync`` now fans each row out to every catalog host, and
``open_catalog_conns`` resolves that host set. These tests pin both.
"""

from datetime import datetime

import pytest

from receivers.archive import engine as engine_mod
from receivers.archive import reindex as reindex_mod
from receivers.archive.config import SyncTarget
from receivers.archive.engine import ArchiveSync

CUTOVER = datetime(2026, 6, 22, 0, 0, 0)


class _FakeConn:
    """Minimal DB connection double recording commits/rollbacks."""

    def __init__(self, label):
        self.label = label
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _target(tmp_root):
    return SyncTarget(
        name="imo_archive",
        active=True,
        tier="archive",
        host="rawdata.vedur.is",
        user="gpsops",
        dest="~/gpsdata",
        source_root=str(tmp_root),
        sessions=("1Hz_1hr",),
        file_categories=("rinex",),
        exclude_stations=frozenset(),
        cutover=CUTOVER,
        overlap_minutes=5,
    )


def _make_archive_file(tmp_path):
    """Create a file at a valid YYYY/mon/STA/SESSION/CAT/ archive path."""
    rel = "2021/mar/NYLA/1Hz_1hr/rinex/NYLA060a.21D.Z"
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"dummy")
    return rel


# --------------------------------------------------------- engine fan-out


def test_catalog_transferred_fans_out_to_every_host(tmp_path, monkeypatch):
    rel = _make_archive_file(tmp_path)
    calls = []
    monkeypatch.setattr(
        engine_mod, "upsert_catalog_row", lambda conn, **kw: calls.append(conn.label)
    )
    monkeypatch.setattr(engine_mod, "content_sha256", lambda _p: "deadbeef")

    c1, c2 = _FakeConn("rek-d01"), _FakeConn("pgdev")
    eng = ArchiveSync(_target(tmp_path), conn=c1, catalog_conns=[c1, c2])
    cataloged, errors, digests = eng._catalog_transferred([rel])

    assert cataloged == 1  # counted once (primary committed)
    assert not errors
    assert calls == ["rek-d01", "pgdev"]  # upserted to BOTH hosts
    assert c1.commits == 1 and c2.commits == 1
    assert digests[rel] == "deadbeef"


def test_mirror_failure_is_best_effort_not_fatal(tmp_path, monkeypatch):
    rel = _make_archive_file(tmp_path)

    def _upsert(conn, **kw):
        if conn.label == "pgdev":
            raise RuntimeError("pgdev down")

    monkeypatch.setattr(engine_mod, "upsert_catalog_row", _upsert)
    monkeypatch.setattr(engine_mod, "content_sha256", lambda _p: "deadbeef")

    c1, c2 = _FakeConn("rek-d01"), _FakeConn("pgdev")
    eng = ArchiveSync(_target(tmp_path), conn=c1, catalog_conns=[c1, c2])
    cataloged, errors, digests = eng._catalog_transferred([rel])

    # Primary still committed and the file counts; the mirror failure is surfaced
    # (divergence warning) but does not abort the run.
    assert cataloged == 1
    assert c1.commits == 1 and c2.rollbacks == 1
    assert any("DIVERGE" in e for e in errors)


def test_backward_compat_single_conn(tmp_path, monkeypatch):
    """A caller passing only conn= (no catalog_conns) writes that one host."""
    rel = _make_archive_file(tmp_path)
    calls = []
    monkeypatch.setattr(
        engine_mod, "upsert_catalog_row", lambda conn, **kw: calls.append(conn.label)
    )
    monkeypatch.setattr(engine_mod, "content_sha256", lambda _p: "deadbeef")

    c1 = _FakeConn("only")
    eng = ArchiveSync(_target(tmp_path), conn=c1)
    cataloged, _, _ = eng._catalog_transferred([rel])
    assert cataloged == 1
    assert calls == ["only"]


# --------------------------------------------------------- open_catalog_conns


def test_open_catalog_conns_prod_set_host_mapping(monkeypatch):
    """Element 0 (operational) opens via default None; mirrors via host_override."""
    monkeypatch.setattr(
        reindex_mod,
        "resolve_catalog_hosts",
        lambda override, prod: ["rek-d01.vedur.is", "pgdev.vedur.is"],
    )
    opened = []

    def _fake_get_conn(host_override=None, **_):
        opened.append(host_override)
        return _FakeConn(host_override or "default")

    monkeypatch.setattr(
        "receivers.db.connection.get_connection", _fake_get_conn, raising=True
    )

    with reindex_mod.open_catalog_conns(prod=True) as conns:
        assert len(conns) == 2
    assert opened == [None, "pgdev.vedur.is"]  # primary via default, mirror by name


def test_open_catalog_conns_secondary_unreachable_is_dropped(monkeypatch):
    monkeypatch.setattr(
        reindex_mod,
        "resolve_catalog_hosts",
        lambda override, prod: ["rek-d01.vedur.is", "pgdev.vedur.is"],
    )

    def _fake_get_conn(host_override=None, **_):
        if host_override == "pgdev.vedur.is":
            raise RuntimeError("pgdev unreachable")
        return _FakeConn(host_override or "default")

    monkeypatch.setattr(
        "receivers.db.connection.get_connection", _fake_get_conn, raising=True
    )

    with reindex_mod.open_catalog_conns(prod=True) as conns:
        assert len(conns) == 1  # mirror dropped, primary survives


def test_open_catalog_conns_explicit_override_honored(monkeypatch):
    """An explicit override opens exactly that host (no None remapping)."""
    monkeypatch.setattr(
        reindex_mod, "resolve_catalog_hosts", lambda override, prod: [override]
    )
    opened = []

    def _fake_get_conn(host_override=None, **_):
        opened.append(host_override)
        return _FakeConn(host_override or "default")

    monkeypatch.setattr(
        "receivers.db.connection.get_connection", _fake_get_conn, raising=True
    )

    with reindex_mod.open_catalog_conns(prod=True, override="pgdev.vedur.is") as conns:
        assert len(conns) == 1
    assert opened == ["pgdev.vedur.is"]


def test_open_catalog_conns_primary_required_raises(monkeypatch):
    monkeypatch.setattr(
        reindex_mod, "resolve_catalog_hosts", lambda override, prod: [None]
    )

    def _boom(host_override=None, **_):
        raise RuntimeError("no DB")

    monkeypatch.setattr("receivers.db.connection.get_connection", _boom, raising=True)

    with pytest.raises(RuntimeError):
        with reindex_mod.open_catalog_conns(prod=True, required=True):
            pass


def test_open_catalog_conns_not_required_yields_empty(monkeypatch):
    monkeypatch.setattr(
        reindex_mod, "resolve_catalog_hosts", lambda override, prod: [None]
    )

    def _boom(host_override=None, **_):
        raise RuntimeError("no DB")

    monkeypatch.setattr("receivers.db.connection.get_connection", _boom, raising=True)

    with reindex_mod.open_catalog_conns(prod=True, required=False) as conns:
        assert conns == []
