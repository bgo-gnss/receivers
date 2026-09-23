"""``archive-sort`` must restamp ``archive_catalog`` for every move the gateway
confirmed — and must NOT touch it for anything else.

Closes the third catalog-drift source: ``archive-sort`` relocated files through
rawdata and left the OLD path's row in place (a phantom that ``archive-prune``
would trust) with NO row at the new path. See :mod:`receivers.archive.restamp`.

The DB double is a real in-memory sqlite ``archive_catalog`` carrying the
production UNIQUE logical key, driven through the same ``%s`` SQL the module
sends to psycopg2 — so "no row at the old key", "the UNIQUE was never
violated" and "the rollback restored the phantom" are properties of stored
state, not of a recorded SQL string. No network: connections are injected.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from receivers.archive import restamp as restamp_mod
from receivers.archive.path_parse import parse_archive_path
from receivers.archive.relocate import RelocateResult
from receivers.archive.restamp import (
    RestampStats,
    hosts_diverged,
    restamp_relocated_rows,
    restamp_relocated_rows_multi,
)
from receivers.utils.canonical_key import canonical_key

LOC = "imo_archive"
DEST = "~/gpsdata"

# A wrong-DATE move (RHOF 2001 batch holding 2011 data): station stays, the
# filename's year token and the year/month dirs change → new canonical_key.
DATE_SRC = "2001/jan/RHOF/15s_24hr/raw/RHOF0010.01.T02.gz"
DATE_DST = "2011/jan/RHOF/15s_24hr/raw/RHOF0010.11.T02.gz"
# A wrong-STATION move (a stray proven by position): station prefix and dir
# change, date stays → new canonical_key.
STA_SRC = "2024/jan/VMOS/15s_24hr/raw/VMOS0100.24.T02.gz"
STA_DST = "2024/jan/FAGC/15s_24hr/raw/FAGC0100.24.T02.gz"
# A directory-only move (filed under the wrong month dir): filename identical
# → the logical key does NOT change, only file_path.
DIR_SRC = "2024/feb/VMOS/15s_24hr/raw/VMOS0100.24.T02.gz"
DIR_DST = "2024/jan/VMOS/15s_24hr/raw/VMOS0100.24.T02.gz"

SHA = "a" * 64
CSHA = "b" * 64
SIZE = 123_456

sqlite3.register_adapter(date, date.isoformat)
sqlite3.register_converter("DATE", lambda b: date.fromisoformat(b.decode()))

_SCHEMA = """
CREATE TABLE archive_catalog (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    storage_location  TEXT NOT NULL,
    station           TEXT,
    file_date         DATE,
    file_hour         INTEGER,
    session_type      TEXT,
    file_category     TEXT NOT NULL,
    canonical_key     TEXT NOT NULL,
    file_path         TEXT NOT NULL,
    compression       TEXT,
    file_size         INTEGER,
    content_sha256    TEXT,
    compressed_sha256 TEXT,
    indexed_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_verified_at  TEXT,
    CONSTRAINT archive_catalog_logical_key
        UNIQUE (storage_location, session_type, file_category, canonical_key)
)
"""


class _Cursor:
    """psycopg2-shaped cursor over sqlite: ``%s`` → ``?``, context manager,
    records every statement's verb, and can be told to fail on a verb."""

    def __init__(self, cur, conn):
        self._cur = cur
        self._conn = conn

    def execute(self, sql, params=None):
        verb = sql.strip().split()[0].upper()
        self._conn.statements.append((verb, sql, params))
        if verb in self._conn.fail_on:
            raise RuntimeError(f"injected failure on {verb}")
        return self._cur.execute(sql.replace("%s", "?"), params or ())

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._cur.close()


class FakeCatalog:
    """One catalog host: an in-memory sqlite ``archive_catalog``."""

    def __init__(self, label="localhost"):
        self.label = label
        self._db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self._db.execute(_SCHEMA)
        self._db.commit()
        self.statements: list = []
        self.fail_on: set = set()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    # -- psycopg2 connection surface -------------------------------------
    def cursor(self):
        return _Cursor(self._db.cursor(), self)

    def commit(self):
        self.commits += 1
        self._db.commit()

    def rollback(self):
        self.rollbacks += 1
        self._db.rollback()

    def close(self):
        self.closed = True

    # -- test helpers -------------------------------------------------------
    def seed(
        self, rel, *, sha=SHA, csha=CSHA, size=SIZE, file_path=None, verified=None
    ):
        p = parse_archive_path(rel, "")
        assert p is not None, rel
        self._db.execute(
            """INSERT INTO archive_catalog
               (storage_location, station, file_date, file_hour, session_type,
                file_category, canonical_key, file_path, compression, file_size,
                content_sha256, compressed_sha256, last_verified_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                LOC,
                p.station,
                p.file_date,
                p.file_hour,
                p.session_type,
                p.file_category,
                canonical_key(rel),
                file_path or f"{DEST}/{rel}",
                ".gz" if rel.endswith(".gz") else "",
                size,
                sha,
                csha,
                verified,
            ),
        )
        self._db.commit()

    def row_at(self, rel):
        """The row at the LOGICAL KEY derived from ``rel``, as a dict or None."""
        p = parse_archive_path(rel, "")
        cur = self._db.execute(
            """SELECT * FROM archive_catalog WHERE storage_location=? AND
               session_type=? AND file_category=? AND canonical_key=?""",
            (LOC, p.session_type, p.file_category, canonical_key(rel)),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def rows(self):
        cur = self._db.execute("SELECT * FROM archive_catalog ORDER BY id")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def verbs(self):
        return [v for v, _s, _p in self.statements]

    def write_verbs(self):
        return [v for v in self.verbs() if v in {"DELETE", "UPDATE", "INSERT"}]


def _run(conn, pairs, *, exists=lambda rel: False, dry_run=False, unreported=None):
    return restamp_relocated_rows(
        conn,
        pairs,
        storage_location=LOC,
        dest_prefix=DEST,
        exists_locally=exists,
        unreported=unreported,
        dry_run=dry_run,
    )


def _assert_row_matches_dst(row, dst):
    """The repointed row's identity is exactly what parse_archive_path(dst)
    says — the single identity derivation, not a MovePlan field."""
    p = parse_archive_path(dst, "")
    assert row["station"] == p.station
    assert row["file_date"] == p.file_date
    assert row["file_hour"] == p.file_hour
    assert row["session_type"] == p.session_type
    assert row["file_category"] == p.file_category
    assert row["canonical_key"] == canonical_key(dst)
    assert row["file_path"] == f"{DEST}/{p.relative_path}"


# ------------------------------------------------------------------ core


class TestRepoint:
    def test_moved_pair_repoints_old_key_to_new(self):
        db = FakeCatalog()
        db.seed(DATE_SRC)
        stats = _run(db, [(DATE_SRC, DATE_DST)])

        assert stats.repointed == [(DATE_SRC, DATE_DST)]
        assert stats.ok
        assert db.row_at(DATE_SRC) is None, "old logical key must be vacated"
        new = db.row_at(DATE_DST)
        assert new is not None
        _assert_row_matches_dst(new, DATE_DST)
        assert len(db.rows()) == 1, "a repoint moves one row, never duplicates"
        assert db.commits == 1

    def test_repointed_row_carries_original_hashes_and_size(self):
        """A mv preserves bytes: the hash columns are invariant and must be
        carried, never re-hashed (there is no NFS read in this path)."""
        db = FakeCatalog()
        db.seed(DATE_SRC, sha=SHA, csha=CSHA, size=SIZE, verified="2026-09-01")
        _run(db, [(DATE_SRC, DATE_DST)])

        new = db.row_at(DATE_DST)
        assert new["content_sha256"] == SHA
        assert new["compressed_sha256"] == CSHA
        assert new["file_size"] == SIZE
        assert new["last_verified_at"] == "2026-09-01"
        # And the UPDATE never names the hash columns at all.
        updates = [s for v, s, _p in db.statements if v == "UPDATE"]
        assert updates, "expected a repoint UPDATE"
        for sql in updates:
            set_clause = sql.split("WHERE")[0]
            assert "content_sha256" not in set_clause
            assert "compressed_sha256" not in set_clause
            assert "file_size" not in set_clause
            assert "last_verified_at" not in set_clause

    def test_identity_is_derived_from_dst_for_station_change(self):
        db = FakeCatalog()
        db.seed(STA_SRC)
        stats = _run(db, [(STA_SRC, STA_DST)])
        assert stats.repointed == [(STA_SRC, STA_DST)]
        assert db.row_at(STA_SRC) is None
        new = db.row_at(STA_DST)
        _assert_row_matches_dst(new, STA_DST)
        assert new["station"] == "FAGC"  # the stray now belongs to FAGC

    def test_identity_is_derived_from_dst_for_date_change(self):
        db = FakeCatalog()
        db.seed(DATE_SRC)
        _run(db, [(DATE_SRC, DATE_DST)])
        new = db.row_at(DATE_DST)
        _assert_row_matches_dst(new, DATE_DST)
        assert new["file_date"] == date(2011, 1, 1)
        assert new["station"] == "RHOF"

    def test_directory_only_move_keeps_key_and_repoints_path(self):
        """Same filename, wrong month dir: the logical key is unchanged, so
        there is no collision lookup and no DELETE — just the file_path."""
        db = FakeCatalog()
        db.seed(DIR_SRC)
        stats = _run(db, [(DIR_SRC, DIR_DST)])
        assert stats.repointed == [(DIR_SRC, DIR_DST)]
        assert "DELETE" not in db.verbs()
        row = db.row_at(DIR_DST)
        assert row["file_path"] == f"{DEST}/{DIR_DST}"
        assert len(db.rows()) == 1

    def test_sql_is_keyed_on_the_natural_key_never_id(self):
        """Surrogate ids differ per catalog host; the dual-write connection
        refuses ``WHERE id =``. Every statement must use the logical key."""
        db = FakeCatalog()
        db.seed(DATE_SRC)
        db.seed(DATE_DST, file_path=f"{DEST}/{DATE_DST}")  # phantom → DELETE too
        _run(db, [(DATE_SRC, DATE_DST)])
        for _v, sql, _p in db.statements:
            assert " id " not in sql and "id =" not in sql, sql


class TestDryRun:
    def test_dry_run_writes_nothing(self):
        db = FakeCatalog()
        db.seed(DATE_SRC)
        before = db.rows()
        stats = _run(db, [(DATE_SRC, DATE_DST)], dry_run=True)

        assert stats.dry_run is True
        assert stats.repointed == [(DATE_SRC, DATE_DST)]  # what it WOULD do
        assert db.write_verbs() == []
        assert db.rows() == before
        assert db.commits == 0
        assert db.rollbacks >= 1, "the read transaction must be ended explicitly"

    def test_dry_run_previews_phantom_removal_without_deleting(self):
        db = FakeCatalog()
        db.seed(DATE_SRC)
        db.seed(DATE_DST, sha="c" * 64)  # phantom at the destination key
        stats = _run(db, [(DATE_SRC, DATE_DST)], dry_run=True, exists=lambda r: False)
        assert stats.collision_phantom_removed == [(DATE_DST, f"{DEST}/{DATE_DST}")]
        assert db.write_verbs() == []
        assert len(db.rows()) == 2


class TestCollisionGuard:
    def test_collision_with_live_file_is_refused_and_both_rows_untouched(self):
        """A row already at the destination key, pointing at a DIFFERENT path
        whose file exists on the read mount: never delete the only pointer
        to a live file."""
        db = FakeCatalog()
        db.seed(DATE_SRC)
        live_path = (
            f"{DEST}/2011/jan/RHOF/15s_24hr/raw/rhof0010.11.T02"  # uncompressed twin
        )
        db.seed(DATE_DST, sha="c" * 64, file_path=live_path)
        before = db.rows()

        stats = _run(
            db,
            [(DATE_SRC, DATE_DST)],
            exists=lambda rel: rel == "2011/jan/RHOF/15s_24hr/raw/rhof0010.11.T02",
        )

        assert stats.repointed == []
        assert len(stats.refused_collision) == 1
        src, dst, occ, why = stats.refused_collision[0]
        assert (src, dst, occ) == (DATE_SRC, DATE_DST, live_path)
        assert "EXISTS" in why
        assert db.write_verbs() == []
        assert db.rows() == before
        assert db.commits == 0

    def test_collision_with_phantom_is_removed_then_repointed(self):
        db = FakeCatalog()
        db.seed(DATE_SRC, sha=SHA)
        phantom_path = f"{DEST}/2011/jan/RHOF/15s_24hr/raw/rhof0010.11.T02"
        db.seed(DATE_DST, sha="c" * 64, file_path=phantom_path)

        stats = _run(db, [(DATE_SRC, DATE_DST)], exists=lambda rel: False)

        assert stats.repointed == [(DATE_SRC, DATE_DST)]
        assert stats.collision_phantom_removed == [(DATE_DST, phantom_path)]
        assert db.row_at(DATE_SRC) is None
        rows = db.rows()
        assert len(rows) == 1
        assert (
            rows[0]["content_sha256"] == SHA
        ), "the SOURCE row survived, not the phantom"
        _assert_row_matches_dst(rows[0], DATE_DST)
        # DELETE then UPDATE, then one commit — a single transaction.
        assert db.write_verbs() == ["DELETE", "UPDATE"]
        assert db.commits == 1

    def test_collision_row_already_pointing_at_dst_is_a_phantom(self):
        """The gateway only reports MOVED when dst did not exist, so a row that
        already pointed at dst pointed at nothing — even if the probe now sees
        the moved file there."""
        db = FakeCatalog()
        db.seed(DATE_SRC, sha=SHA)
        db.seed(DATE_DST, sha="c" * 64)  # file_path == DEST/DATE_DST
        stats = _run(db, [(DATE_SRC, DATE_DST)], exists=lambda rel: True)
        assert stats.repointed == [(DATE_SRC, DATE_DST)]
        assert len(db.rows()) == 1
        assert db.rows()[0]["content_sha256"] == SHA

    def test_collision_is_refused_when_existence_cannot_be_probed(self):
        db = FakeCatalog()
        db.seed(DATE_SRC)
        other = f"{DEST}/2011/jan/RHOF/15s_24hr/raw/rhof0010.11.T02"
        db.seed(DATE_DST, file_path=other)
        stats = _run(db, [(DATE_SRC, DATE_DST)], exists=None)
        assert len(stats.refused_collision) == 1
        assert "cannot probe" in stats.refused_collision[0][3]
        assert db.write_verbs() == []

    def test_collision_is_refused_when_file_path_is_not_under_dest(self):
        db = FakeCatalog()
        db.seed(DATE_SRC)
        db.seed(DATE_DST, file_path="/somewhere/else/RHOF0010.11.T02.gz")
        stats = _run(db, [(DATE_SRC, DATE_DST)], exists=lambda rel: False)
        assert len(stats.refused_collision) == 1
        assert "unknowable" in stats.refused_collision[0][3]
        assert db.write_verbs() == []

    def test_phantom_delete_and_repoint_are_one_transaction(self):
        """If the repoint fails after the phantom delete, the pair rolls back:
        the phantom row is restored and the source row is untouched."""
        db = FakeCatalog()
        db.seed(DATE_SRC, sha=SHA)
        phantom_path = f"{DEST}/2011/jan/RHOF/15s_24hr/raw/rhof0010.11.T02"
        db.seed(DATE_DST, sha="c" * 64, file_path=phantom_path)
        before = db.rows()
        db.fail_on = {"UPDATE"}

        stats = _run(db, [(DATE_SRC, DATE_DST)], exists=lambda rel: False)

        assert stats.repointed == []
        assert stats.collision_phantom_removed == [], "bookkeeping undone on rollback"
        assert len(stats.errors) == 1 and "injected failure" in stats.errors[0]
        assert not stats.ok
        assert db.rows() == before
        assert db.commits == 0
        assert db.rollbacks >= 1


class TestSkipBuckets:
    def test_source_row_absent_is_uncatalogued_and_writes_nothing(self):
        db = FakeCatalog()  # empty catalog
        stats = _run(db, [(DATE_SRC, DATE_DST)])
        assert stats.uncatalogued == [(DATE_SRC, DATE_DST)]
        assert stats.repointed == []
        assert db.write_verbs() == []
        assert db.rows() == []
        assert db.commits == 0

    def test_unreported_pairs_are_named_unknown_and_never_touched(self, caplog):
        db = FakeCatalog()
        db.seed(DATE_SRC)
        with caplog.at_level("ERROR", logger="receivers.archive.restamp"):
            stats = _run(db, [], unreported=[(DATE_SRC, DATE_DST)])
        assert stats.unknown_unreported == [(DATE_SRC, DATE_DST)]
        assert db.statements == [], "no SELECT, no write — state is unknown"
        assert db.row_at(DATE_SRC) is not None
        assert "UNKNOWN" in caplog.text and DATE_SRC in caplog.text

    def test_unparsable_pair_is_reported_not_written(self):
        db = FakeCatalog()
        db.seed(DATE_SRC)
        bad = "not/an/archive/path.gz"
        stats = _run(db, [(DATE_SRC, bad)])
        assert stats.unparsable == [(DATE_SRC, bad)]
        assert db.write_verbs() == []

    def test_no_connection_with_pairs_is_an_error_not_a_silent_skip(self):
        stats = _run(None, [(DATE_SRC, DATE_DST)])
        assert stats.errors == ["no DB connection"]
        assert not stats.ok


# ---------------------------------------------------------------- multi-host


class TestMultiHost:
    def _hosts(self, monkeypatch, dbs, *, fail=()):
        """Route get_connection(host_override=X) to the FakeCatalog for X."""
        by_label = {db.label: db for db in dbs}

        def fake_get_connection(host_override=None, **_kw):
            label = host_override or "localhost"
            if label in fail:
                raise ConnectionError(f"{label} unreachable")
            return by_label[label]

        monkeypatch.setattr(
            "receivers.db.connection.get_connection", fake_get_connection
        )

    def test_every_host_receives_the_same_repoint(self, monkeypatch):
        a, b = FakeCatalog("rek-d01"), FakeCatalog("pgdev")
        for db in (a, b):
            db.seed(DATE_SRC)
        self._hosts(monkeypatch, [a, b])

        results = restamp_relocated_rows_multi(
            ["rek-d01", "pgdev"],
            [(DATE_SRC, DATE_DST)],
            storage_location=LOC,
            dest_prefix=DEST,
            exists_locally=lambda rel: False,
        )

        assert set(results) == {"rek-d01", "pgdev"}
        assert results["rek-d01"].counts() == results["pgdev"].counts()
        assert not hosts_diverged(results)
        for db in (a, b):
            assert db.row_at(DATE_SRC) is None
            _assert_row_matches_dst(db.row_at(DATE_DST), DATE_DST)
            assert db.closed

    def test_unreachable_secondary_is_reported_as_divergence(self, monkeypatch):
        a, b = FakeCatalog("rek-d01"), FakeCatalog("pgdev")
        a.seed(DATE_SRC)
        self._hosts(monkeypatch, [a, b], fail={"pgdev"})

        results = restamp_relocated_rows_multi(
            ["rek-d01", "pgdev"],
            [(DATE_SRC, DATE_DST)],
            storage_location=LOC,
            dest_prefix=DEST,
            exists_locally=lambda rel: False,
        )
        assert results["pgdev"] is None
        assert results["rek-d01"].repointed == [(DATE_SRC, DATE_DST)]
        assert hosts_diverged(results)

    def test_count_mismatch_between_hosts_is_divergence(self):
        ok = RestampStats(repointed=[(DATE_SRC, DATE_DST)])
        skipped = RestampStats(uncatalogued=[(DATE_SRC, DATE_DST)])
        assert hosts_diverged({"a": ok, "b": skipped})
        assert not hosts_diverged({"a": ok, "b": RestampStats(repointed=[("x", "y")])})


# ---------------------------------------------------------------- CLI seam


class _Target:
    name = LOC
    dest = DEST
    host = "rawdata.vedur.is"
    user = "gpsops"
    tier = "archive"


def _cli_args(argv):
    """Through the REAL parser so the flag names/defaults are exercised."""
    from receivers.cli.archive_sync import create_archive_sort_parser

    root = argparse.ArgumentParser()
    sub = root.add_subparsers()
    create_archive_sort_parser(sub)
    return root.parse_args(["archive-sort", *argv])


class TestCliHelper:
    """``_restamp_catalog_after_relocate`` selects the pair set (moved ONLY on
    execute, would_move on preview), resolves hosts, and reports."""

    def _wire(self, monkeypatch, dbs, *, fail=()):
        by_label = {db.label: db for db in dbs}

        def fake_get_connection(host_override=None, **_kw):
            label = host_override or "localhost"
            if label in fail:
                raise ConnectionError(f"{label} unreachable")
            return by_label[label]

        monkeypatch.setattr(
            "receivers.db.connection.get_connection", fake_get_connection
        )

    def test_only_moved_pairs_reach_the_catalog(self, monkeypatch, tmp_path, capsys):
        """would_move / dst_exists / failed / missing did NOT move in this run."""
        from receivers.cli.archive_sync import _restamp_catalog_after_relocate

        db = FakeCatalog("h1")
        # DIR_SRC shares STA_SRC's canonical key (same filename), so the
        # dst_exists pair uses a distinct file here.
        exists_src = "2021/mar/NYLA/15s_24hr/rinex/NYLA0600.21D.Z"
        exists_dst = "2021/mar/FAGC/15s_24hr/rinex/FAGC0600.21D.Z"
        other_src = "2013/jul/KOSK/15s_24hr/raw/KOSK1860.13.T02"
        for rel in (DATE_SRC, STA_SRC, exists_src, other_src):
            db.seed(rel)
        self._wire(monkeypatch, [db])

        res = RelocateResult()
        res.moved = [(DATE_SRC, DATE_DST)]
        res.would_move = [(STA_SRC, STA_DST)]
        res.dst_exists = [(exists_src, exists_dst)]
        res.failed = [(other_src, "2013/jul/KOSK/15s_24hr/raw/KOSK1870.13.T02")]
        args = _cli_args(["--yes", "--catalog-host", "h1", "--root", str(tmp_path)])

        ok = _restamp_catalog_after_relocate(args, res, target=_Target(), root=tmp_path)

        assert ok
        assert db.row_at(DATE_SRC) is None and db.row_at(DATE_DST) is not None
        # Everything else is exactly where it was.
        for rel in (STA_SRC, exists_src, other_src):
            assert db.row_at(rel) is not None
            assert db.row_at(rel)["file_path"] == f"{DEST}/{rel}"
        assert db.row_at(STA_DST) is None
        assert db.row_at(exists_dst) is None
        assert db.write_verbs() == ["UPDATE"]
        out = capsys.readouterr().out
        assert "CATALOG RESTAMP" in out and "1 repointed" in out

    def test_dry_run_previews_would_move_and_writes_nothing(
        self, monkeypatch, tmp_path, capsys
    ):
        from receivers.cli.archive_sync import _restamp_catalog_after_relocate

        db = FakeCatalog("h1")
        db.seed(DATE_SRC)
        self._wire(monkeypatch, [db])
        res = RelocateResult()
        res.would_move = [(DATE_SRC, DATE_DST)]
        args = _cli_args(["--catalog-host", "h1"])  # no --yes

        ok = _restamp_catalog_after_relocate(args, res, target=_Target(), root=tmp_path)

        assert ok
        assert db.write_verbs() == []
        assert db.row_at(DATE_SRC) is not None
        out = capsys.readouterr().out
        assert "DRY-RUN — nothing written" in out
        assert "1 would repoint" in out

    def test_nothing_to_do_never_opens_a_connection(self, monkeypatch, tmp_path):
        from receivers.cli.archive_sync import _restamp_catalog_after_relocate

        def boom(**_kw):
            raise AssertionError("must not connect")

        monkeypatch.setattr("receivers.db.connection.get_connection", boom)
        res = RelocateResult()
        res.dst_exists = [(DATE_SRC, DATE_DST)]
        args = _cli_args(["--yes", "--catalog-host", "h1"])
        assert _restamp_catalog_after_relocate(
            args, res, target=_Target(), root=tmp_path
        )

    def test_catalog_prod_with_unset_hosts_refuses_loudly(
        self, monkeypatch, tmp_path, capsys
    ):
        from receivers.cli.archive_sync import _restamp_catalog_after_relocate

        def boom(**_kw):
            raise AssertionError("must not connect")

        monkeypatch.setattr("receivers.db.connection.get_connection", boom)
        with patch("receivers.archive.resolve_catalog_hosts", return_value=[]):
            res = RelocateResult()
            res.moved = [(DATE_SRC, DATE_DST)]
            args = _cli_args(["--yes", "--catalog-prod"])
            ok = _restamp_catalog_after_relocate(
                args, res, target=_Target(), root=tmp_path
            )
        assert ok is False
        out = capsys.readouterr().out
        assert "catalog_hosts is unset" in out
        assert "STALE" in out  # the files DID move; say so

    def test_hosts_are_written_symmetrically_and_divergence_is_loud(
        self, monkeypatch, tmp_path, capsys
    ):
        from receivers.cli.archive_sync import _restamp_catalog_after_relocate

        a, b = FakeCatalog("rek-d01"), FakeCatalog("pgdev")
        a.seed(DATE_SRC)
        b.seed(DATE_SRC)
        self._wire(monkeypatch, [a, b])
        res = RelocateResult()
        res.moved = [(DATE_SRC, DATE_DST)]
        args = _cli_args(["--yes", "--catalog-host", "rek-d01,pgdev"])

        ok = _restamp_catalog_after_relocate(args, res, target=_Target(), root=tmp_path)
        assert ok
        assert (
            a.rows()[0]["file_path"] == b.rows()[0]["file_path"] == f"{DEST}/{DATE_DST}"
        )
        out = capsys.readouterr().out
        assert "rek-d01: 1 repointed" in out and "pgdev: 1 repointed" in out
        assert "DIVERGED" not in out

        # Now the mirror is down: primary still repoints, and it is LOUD.
        a2, b2 = FakeCatalog("rek-d01"), FakeCatalog("pgdev")
        a2.seed(STA_SRC)
        b2.seed(STA_SRC)
        self._wire(monkeypatch, [a2, b2], fail={"pgdev"})
        res2 = RelocateResult()
        res2.moved = [(STA_SRC, STA_DST)]
        ok2 = _restamp_catalog_after_relocate(
            args, res2, target=_Target(), root=tmp_path
        )
        assert ok2 is False
        assert a2.row_at(STA_DST) is not None
        assert b2.row_at(STA_SRC) is not None  # untouched, still at the old key
        out2 = capsys.readouterr().out
        assert "restamp FAILED on pgdev" in out2
        assert "CATALOG HOSTS DIVERGED" in out2

    def test_unreported_pairs_are_listed_as_unknown_and_fail_the_run(
        self, monkeypatch, tmp_path, capsys
    ):
        from receivers.cli.archive_sync import _restamp_catalog_after_relocate

        db = FakeCatalog("h1")
        db.seed(DATE_SRC)
        self._wire(monkeypatch, [db])
        res = RelocateResult()
        res.unreported = [(DATE_SRC, DATE_DST)]
        args = _cli_args(["--yes", "--catalog-host", "h1"])

        ok = _restamp_catalog_after_relocate(args, res, target=_Target(), root=tmp_path)

        assert ok is False
        assert db.statements == []
        out = capsys.readouterr().out
        assert "UNKNOWN catalog state for 1 pair(s)" in out
        assert f"? {DATE_SRC} -> {DATE_DST}" in out
        assert "re-run" in out

    def test_missing_read_mount_refuses_collisions(self, monkeypatch, tmp_path, capsys):
        """No --root dir → a phantom cannot be proven → collision refused, and
        the exit code says the catalog is not consistent."""
        from receivers.cli.archive_sync import _restamp_catalog_after_relocate

        db = FakeCatalog("h1")
        db.seed(DATE_SRC)
        db.seed(
            DATE_DST, file_path=f"{DEST}/2011/jan/RHOF/15s_24hr/raw/rhof0010.11.T02"
        )
        self._wire(monkeypatch, [db])
        res = RelocateResult()
        res.moved = [(DATE_SRC, DATE_DST)]
        args = _cli_args(["--yes", "--catalog-host", "h1"])
        missing_root = tmp_path / "no-such-mount"

        ok = _restamp_catalog_after_relocate(
            args, res, target=_Target(), root=missing_root
        )
        assert ok is False
        assert db.write_verbs() == []
        out = capsys.readouterr().out
        assert "not found" in out and "REFUSED" in out

    def test_json_report_carries_every_bucket_per_host(
        self, monkeypatch, tmp_path, capsys
    ):
        import json

        from receivers.cli.archive_sync import _restamp_catalog_after_relocate

        db = FakeCatalog("h1")
        db.seed(DATE_SRC)
        self._wire(monkeypatch, [db])
        res = RelocateResult()
        res.moved = [(DATE_SRC, DATE_DST)]
        res.unreported = [(STA_SRC, STA_DST)]
        args = _cli_args(["--yes", "--catalog-host", "h1", "--json"])

        _restamp_catalog_after_relocate(args, res, target=_Target(), root=tmp_path)

        doc = json.loads(capsys.readouterr().out)["catalog_restamp"]
        assert doc["dry_run"] is False
        assert doc["storage_location"] == LOC
        h = doc["hosts"]["h1"]
        assert h["counts"] == {
            "repointed": 1,
            "collision_phantom_removed": 0,
            "refused_collision": 0,
            "uncatalogued": 0,
            "unknown_unreported": 1,
            "unparsable": 0,
            "errors": 0,
        }
        assert h["repointed"] == [[DATE_SRC, DATE_DST]]
        assert h["unknown_unreported"] == [[STA_SRC, STA_DST]]
        assert doc["diverged"] is False


class TestParserAndCallSites:
    def test_parser_exposes_catalog_flags(self):
        args = _cli_args(["--catalog-prod", "--catalog-host", "x", "--json"])
        assert args.catalog_prod is True
        assert args.catalog_host == "x"
        assert args.json is True
        args = _cli_args([])
        assert args.catalog_prod is False and args.catalog_host is None
        assert args.json is False and args.yes is False

    def test_apply_plan_file_calls_the_restamp_with_the_relocate_result(
        self, monkeypatch, tmp_path
    ):
        """The TSV path has NO MovePlan objects — the same helper must run."""
        from receivers.cli import archive_sync

        plan = tmp_path / "plan.tsv"
        plan.write_text(f"# src\tdst\n{DATE_SRC}\t{DATE_DST}\twrong-date\n")
        res = RelocateResult()
        res.moved = [(DATE_SRC, DATE_DST)]
        seen = {}

        def spy(args, r, *, target, root, plans=None):
            seen["res"] = r
            seen["plans"] = plans
            seen["root"] = root
            return True

        monkeypatch.setattr(archive_sync, "_restamp_catalog_after_relocate", spy)
        monkeypatch.setattr(archive_sync, "_record_plan_applied", lambda *a, **k: None)
        with (
            patch("receivers.archive.load_sync_config", return_value=[_Target()]),
            patch("receivers.archive.relocate_archive_files", return_value=res),
        ):
            args = _cli_args(
                ["--apply-plan", str(plan), "--yes", "--root", str(tmp_path)]
            )
            rc = archive_sync._apply_plan_file(plan, args)
        assert rc == 0
        assert seen["res"] is res
        assert seen["plans"] is None
        assert seen["root"] == Path(str(tmp_path))

    def test_apply_plan_exit_code_reflects_catalog_inconsistency(
        self, monkeypatch, tmp_path
    ):
        from receivers.cli import archive_sync

        plan = tmp_path / "plan.tsv"
        plan.write_text(f"{DATE_SRC}\t{DATE_DST}\n")
        res = RelocateResult()
        res.moved = [(DATE_SRC, DATE_DST)]
        monkeypatch.setattr(
            archive_sync, "_restamp_catalog_after_relocate", lambda *a, **k: False
        )
        monkeypatch.setattr(archive_sync, "_record_plan_applied", lambda *a, **k: None)
        with (
            patch("receivers.archive.load_sync_config", return_value=[_Target()]),
            patch("receivers.archive.relocate_archive_files", return_value=res),
        ):
            args = _cli_args(["--apply-plan", str(plan), "--yes"])
            assert archive_sync._apply_plan_file(plan, args) == 1

    def test_cmd_archive_sort_passes_plans_for_reporting(self, monkeypatch, tmp_path):
        """The scan path has MovePlans: they reach the helper for REPORTING only
        (reasons), never for row construction."""
        from receivers.archive.sort import MovePlan
        from receivers.cli import archive_sync

        plan = MovePlan(
            src_rel=DATE_SRC,
            dst_rel=DATE_DST,
            fmt="trimble",
            decoded_start=datetime(2011, 1, 1, 0, 0),
            claimed=datetime(2001, 1, 1, 0, 0),
            reasons=("wrong-date",),
        )
        res = RelocateResult()
        res.moved = [(DATE_SRC, DATE_DST)]
        seen = {}

        def spy(args, r, *, target, root, plans=None):
            seen["res"] = r
            seen["plans"] = plans
            return True

        monkeypatch.setattr(archive_sync, "_restamp_catalog_after_relocate", spy)
        monkeypatch.setattr(
            archive_sync,
            "_persist_remediation_records",
            lambda *a, **k: None,
        )
        archive_sync._persist_remediation_records.last_written = []
        monkeypatch.setattr(archive_sync, "_print_fix_commands", lambda *a, **k: None)
        with (
            patch("receivers.archive.plan_relocations", return_value=([plan], [])),
            patch("receivers.archive.plan_rinex_relocations", return_value=([], [])),
            patch("receivers.archive.load_sync_config", return_value=[_Target()]),
            patch("receivers.archive.relocate_archive_files", return_value=res),
            patch("receivers.archive.sort.resolve_position_gate_m", return_value=10.0),
        ):
            args = _cli_args(["--file", DATE_SRC, "--yes", "--root", str(tmp_path)])
            rc = archive_sync.cmd_archive_sort(args)
        assert rc == 0
        assert seen["res"] is res
        assert seen["plans"] == [plan]


def test_module_docstring_states_the_uncatalogued_decision():
    """B5 is an explicit decision, not a discovered edge case."""
    assert "Source row missing" in restamp_mod.__doc__
    assert "uncatalogued" in restamp_mod.__doc__
