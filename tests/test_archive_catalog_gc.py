"""``archive-catalog-gc`` deletes ONLY provable phantom rows.

A phantom row is a catalog row whose archive file is gone and whose digest is
the empty-content one. These tests pin the guards that keep the verb from ever
deleting the pointer to a real file: filesystem-derived presence, the real-
digest report-only branch, the size cap (re-applied at delete time on each
host), the fraction brake, natural-key-only SQL, and per-host symmetry.

The DB double is a real in-memory sqlite ``archive_catalog`` driven through the
module's own ``%s`` SQL, so "the row is gone" / "the row is still there" are
properties of STORED STATE, not of a recorded call. No network:
``get_connection`` is patched to hand out one fake per host label.
"""

from __future__ import annotations

import argparse
import inspect
import re
import sqlite3
from datetime import date
from unittest.mock import patch

import pytest

from receivers.archive import catalog_gc
from receivers.archive.catalog_gc import (
    DEFAULT_MAX_SIZE,
    GcStats,
    apply_gc,
    classify_rows,
    count_location_rows,
    gc_catalog_rows,
    gc_hosts_diverged,
    select_stub_rows,
)
from receivers.utils import content_hash
from receivers.utils.canonical_key import canonical_key
from receivers.utils.content_hash import EMPTY_CONTENT_SHA256

LOC = "imo_archive"
DEST = "~/gpsdata"

# A spread of stations / years / sessions so the breakdown has something to count.
STUB_Z = "2017/mar/HLID/15s_24hr/rinex/HLID0600.17D.Z"  # 3 B compress header
STUB_EMPTY = "2019/jul/ROTH/1Hz_1hr/raw/ROTH201907040100b.sbf.gz"  # 0 B
STUB_GZ = "2019/jul/ROTH/15s_24hr/raw/ROTH201907040000a.sbf.gz"  # 33 B gzip-of-nothing
STUB_42 = "2022/jan/THOB/1Hz_1hr/rinex/THOB001o.22d.Z"  # the one-off
REAL_ABSENT = "2020/may/ELDC/15s_24hr/rinex/ELDC1210.20D.Z"  # real digest, gone
REAL_PRESENT = "2026/feb/ELDC/15s_24hr/rinex/ELDC0410.26D.gz"
LIVE_STUB = "2026/feb/THOB/1Hz_1hr/rinex/THOB041o.26d.gz"  # empty-digest, EXISTS

REAL_SHA = "a" * 64
STUBS = {STUB_Z: 3, STUB_EMPTY: 0, STUB_GZ: 33, STUB_42: 42}

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
);
"""


class _Cursor:
    """psycopg2-shaped cursor over sqlite: ``%s`` → ``?``; records every
    statement (verb, sql, params); can be told to fail on a verb."""

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


class FakeHost:
    """One gps_health host: an in-memory sqlite ``archive_catalog``."""

    def __init__(self, label="localhost"):
        self.label = label
        self._db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self.statements: list = []
        self.fail_on: set = set()
        self.commits = 0
        self.rollbacks = 0
        self._filler_next = 0

    def cursor(self):
        return _Cursor(self._db.cursor(), self)

    def commit(self):
        self.commits += 1
        self._db.commit()

    def rollback(self):
        self.rollbacks += 1
        self._db.rollback()

    def close(self):
        pass

    # -- seeding ------------------------------------------------------------
    def seed(self, rel, sha, size, *, file_path=None, loc=LOC):
        from receivers.archive.path_parse import parse_archive_path

        p = parse_archive_path(rel, "")
        assert p is not None, rel
        self._db.execute(
            """INSERT INTO archive_catalog
               (storage_location, station, file_date, file_hour, session_type,
                file_category, canonical_key, file_path, file_size, content_sha256)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                loc,
                p.station,
                p.file_date,
                p.file_hour,
                p.session_type,
                p.file_category,
                canonical_key(rel),
                file_path or f"{DEST}/{rel}",
                size,
                sha,
            ),
        )
        self._db.commit()

    def seed_filler(self, n, *, loc=LOC):
        """``n`` unrelated real rows (the fraction guard's denominator)."""
        start = self._filler_next
        self._filler_next += n
        for i in range(start, start + n):
            self._db.execute(
                """INSERT INTO archive_catalog
                   (storage_location, station, session_type, file_category,
                    canonical_key, file_path, file_size, content_sha256)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    loc,
                    "FILL",
                    "15s_24hr",
                    "raw",
                    f"fill-{i}",
                    f"{DEST}/fill{i}",
                    100,
                    "b" * 64,
                ),
            )
        self._db.commit()

    def set_size(self, rel, size):
        from receivers.archive.path_parse import parse_archive_path

        p = parse_archive_path(rel, "")
        self._db.execute(
            """UPDATE archive_catalog SET file_size=? WHERE storage_location=? AND
               session_type=? AND file_category=? AND canonical_key=?""",
            (size, LOC, p.session_type, p.file_category, canonical_key(rel)),
        )
        self._db.commit()

    # -- assertions -----------------------------------------------------------
    def row_at(self, rel):
        from receivers.archive.path_parse import parse_archive_path

        p = parse_archive_path(rel, "")
        cur = self._db.execute(
            """SELECT * FROM archive_catalog WHERE storage_location=? AND
               session_type=? AND file_category=? AND canonical_key=?""",
            (LOC, p.session_type, p.file_category, canonical_key(rel)),
        )
        row = cur.fetchone()
        return None if row is None else dict(zip([d[0] for d in cur.description], row))

    def nrows(self):
        return self._db.execute("SELECT count(*) FROM archive_catalog").fetchone()[0]

    def write_verbs(self):
        return [
            v for v, _s, _p in self.statements if v in {"INSERT", "UPDATE", "DELETE"}
        ]


# ------------------------------------------------------------------ helpers


def _touch(root, rel, payload=b""):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.fixture
def root(tmp_path):
    """A read root that LOOKS mounted (has a year dir) but holds no stub files."""
    (tmp_path / "2017").mkdir()
    return tmp_path


@pytest.fixture
def hosts():
    fakes = {"localhost": FakeHost("localhost"), "pgdev": FakeHost("pgdev")}

    def _connect(host_override=None, database=None, single_host=False):
        return fakes[host_override or "localhost"]

    with patch("receivers.db.connection.get_connection", side_effect=_connect):
        yield fakes


def _seed_stubs(host, rels=STUBS):
    for rel, size in rels.items():
        host.seed(rel, EMPTY_CONTENT_SHA256, size)


def _run(host, root, *, host_list=(None,), dry_run=False, limit=None, filler=0, **kw):
    """Select from ``host`` (the module's own sweep), then GC on ``host_list``."""
    rows = select_stub_rows(host, storage_location=LOC, limit=limit)
    total = count_location_rows(host, storage_location=LOC)
    return gc_catalog_rows(
        rows,
        hosts=list(host_list),
        read_root=str(root),
        storage_location=LOC,
        dest_prefix=DEST,
        total_rows=total,
        dry_run=dry_run,
        **kw,
    )


# ======================================= 1. absent + empty digest → deleted


class TestPhantomIsDeleted:
    def test_absent_empty_digest_rows_are_deleted(self, root, hosts):
        h = hosts["localhost"]
        _seed_stubs(h)
        h.seed_filler(400)  # 4 / 404 < 2 %
        assert h.nrows() == 404

        stats = _run(h, root, force=False)

        assert stats.counts()["gc_candidate"] == 4
        assert stats.hosts["localhost"].deleted == 4
        for rel in STUBS:
            assert h.row_at(rel) is None, rel
        assert h.nrows() == 400, "only the phantoms went"
        assert stats.ok and not stats.problems and not stats.diverged

    def test_selection_is_the_empty_digest_sweep_only(self, root, hosts):
        """The sweep never fetches a real-digest row, absent or not."""
        h = hosts["localhost"]
        _seed_stubs(h)
        h.seed(REAL_ABSENT, REAL_SHA, 268)
        rows = select_stub_rows(h, storage_location=LOC)
        assert {r["content_sha256"] for r in rows} == {EMPTY_CONTENT_SHA256}
        assert len(rows) == 4
        (sel,) = [s for v, s, _p in h.statements if v == "SELECT"]
        assert "content_sha256 = %s" in sel and "storage_location = %s" in sel

    def test_limit_bounds_the_selection_oldest_first(self, root, hosts):
        h = hosts["localhost"]
        _seed_stubs(h)
        rows = select_stub_rows(h, storage_location=LOC, limit=2)
        assert [r["file_date"] for r in rows] == ["2017-03-01", "2019-07-04"]


# ============================== 2. absent + REAL digest → reported, NOT deleted


class TestRealDigestIsReportOnly:
    def test_absent_real_digest_row_is_reported_not_deleted(self, root, hosts):
        """Fed in as a VerifyStats.missing_rows-shaped dict (the future caller)."""
        h = hosts["localhost"]
        h.seed(REAL_ABSENT, REAL_SHA, 268)
        h.seed_filler(100)
        rows = [
            {
                "station": "ELDC",
                "session_type": "15s_24hr",
                "file_category": "rinex",
                "file_date": "2020-05-01",
                "file_path": f"{DEST}/{REAL_ABSENT}",
                "canonical_key": canonical_key(REAL_ABSENT),
                "content_sha256": REAL_SHA,
                "local_path": str(root / REAL_ABSENT),
            }
        ]
        stats = gc_catalog_rows(
            rows,
            hosts=[None],
            read_root=str(root),
            storage_location=LOC,
            dest_prefix=DEST,
            total_rows=101,
            dry_run=False,
        )
        (item,) = stats.items
        assert item.cls == "report_only"
        assert "relocated or lost" in item.reason
        assert h.row_at(REAL_ABSENT) is not None
        assert h.write_verbs() == []
        assert not stats.ok and any("report-only" in p for p in stats.problems)


# ==================================== 3. file PRESENT → row untouched (core)


class TestPresentFileIsNeverDeleted:
    def test_present_file_row_is_untouched_even_with_empty_digest(self, root, hosts):
        """The data-loss guard: presence is decided by the FILESYSTEM. A row
        whose file exists is kept whatever the row says — a present empty-digest
        file is a live stub, a different problem."""
        h = hosts["localhost"]
        _seed_stubs(h, {STUB_Z: 3})
        h.seed(LIVE_STUB, EMPTY_CONTENT_SHA256, 0)
        h.seed(REAL_PRESENT, REAL_SHA, 5000)
        h.seed_filler(400)
        _touch(root, LIVE_STUB)  # the live stub exists (0 B)
        _touch(root, REAL_PRESENT, b"x" * 5000)

        # Classify BOTH present rows (the real one fed in by hand — the sweep
        # never selects it) alongside the genuine phantom.
        rows = select_stub_rows(h, storage_location=LOC)
        rows.append(
            {
                "station": "ELDC",
                "session_type": "15s_24hr",
                "file_category": "rinex",
                "file_date": "2026-02-10",
                "file_path": f"{DEST}/{REAL_PRESENT}",
                "canonical_key": canonical_key(REAL_PRESENT),
                "content_sha256": REAL_SHA,
                "file_size": 5000,
            }
        )
        stats = gc_catalog_rows(
            rows,
            hosts=[None],
            read_root=str(root),
            storage_location=LOC,
            dest_prefix=DEST,
            total_rows=h.nrows(),
            dry_run=False,
        )
        by_path = {r.file_path: r for r in stats.items}
        assert by_path[f"{DEST}/{LIVE_STUB}"].cls == "present_kept"
        assert "LIVE STUB" in by_path[f"{DEST}/{LIVE_STUB}"].reason
        assert by_path[f"{DEST}/{REAL_PRESENT}"].cls == "present_kept"
        assert by_path[f"{DEST}/{STUB_Z}"].cls == "gc_candidate"
        assert h.row_at(LIVE_STUB) is not None
        assert h.row_at(REAL_PRESENT) is not None
        assert h.row_at(STUB_Z) is None
        assert stats.live_stubs and any("LIVE stub" in p for p in stats.problems)

    def test_row_file_size_is_not_trusted_for_presence(self, root, hosts):
        """A row claiming 0 B whose file is actually present and non-empty is
        still present — the size column never decides existence."""
        h = hosts["localhost"]
        h.seed(STUB_Z, EMPTY_CONTENT_SHA256, 0)
        _touch(root, STUB_Z, b"real bytes after all")
        stats = _run(h, root)
        assert stats.items[0].cls == "present_kept"
        assert h.row_at(STUB_Z) is not None

    def test_dangling_symlink_counts_as_present(self, root, hosts):
        h = hosts["localhost"]
        h.seed(STUB_Z, EMPTY_CONTENT_SHA256, 3)
        target = root / STUB_Z
        target.parent.mkdir(parents=True)
        target.symlink_to(root / "nowhere")
        stats = _run(h, root)
        assert stats.items[0].cls == "present_kept"
        assert h.row_at(STUB_Z) is not None

    def test_file_appearing_between_classify_and_delete_keeps_the_row(
        self, root, hosts
    ):
        h = hosts["localhost"]
        h.seed(STUB_Z, EMPTY_CONTENT_SHA256, 3)
        rows = classify_rows(
            select_stub_rows(h, storage_location=LOC),
            read_root=str(root),
            dest_prefix=DEST,
        )
        assert rows[0].cls == "gc_candidate"
        _touch(root, STUB_Z)  # appears after classification
        res = apply_gc([None], rows, storage_location=LOC, dry_run=False)
        assert res["localhost"].deleted == 0
        assert res["localhost"].file_appeared == [f"{DEST}/{STUB_Z}"]
        assert h.row_at(STUB_Z) is not None


# ======================================================= 4. dry-run deletes nothing


class TestDryRun:
    def test_dry_run_deletes_nothing(self, root, hosts):
        h = hosts["localhost"]
        _seed_stubs(h)
        h.seed_filler(400)
        stats = _run(h, root, dry_run=True)
        assert stats.dry_run
        assert stats.counts()["gc_candidate"] == 4
        assert stats.hosts["localhost"].would_delete == 4
        assert stats.hosts["localhost"].deleted == 0
        assert h.nrows() == 404
        assert h.write_verbs() == [], "a dry run issues no DML"
        assert h.rollbacks >= 1, "the dry-run read transaction is closed"

    def test_default_is_dry_run(self, root, hosts):
        h = hosts["localhost"]
        _seed_stubs(h, {STUB_Z: 3})
        rows = select_stub_rows(h, storage_location=LOC)
        stats = gc_catalog_rows(
            rows,
            hosts=[None],
            read_root=str(root),
            storage_location=LOC,
            dest_prefix=DEST,
            total_rows=1000,
        )
        assert stats.dry_run and h.row_at(STUB_Z) is not None


# ==================================================== 5. --max-size, both times


class TestMaxSize:
    def test_oversized_row_is_refused_at_classification(self, root, hosts):
        h = hosts["localhost"]
        h.seed(STUB_Z, EMPTY_CONTENT_SHA256, 3)
        h.seed(STUB_42, EMPTY_CONTENT_SHA256, 42)
        h.seed_filler(400)
        stats = _run(h, root, max_size=8)
        by_path = {r.file_path: r for r in stats.items}
        assert by_path[f"{DEST}/{STUB_Z}"].cls == "gc_candidate"
        assert by_path[f"{DEST}/{STUB_42}"].cls == "oversized_refused"
        assert h.row_at(STUB_Z) is None
        assert h.row_at(STUB_42) is not None
        assert any("over --max-size" in p for p in stats.problems)

    def test_null_size_is_refused(self, root, hosts):
        h = hosts["localhost"]
        h.seed(STUB_Z, EMPTY_CONTENT_SHA256, None)
        stats = _run(h, root)
        assert stats.items[0].cls == "oversized_refused"
        assert h.row_at(STUB_Z) is not None

    def test_default_cap_admits_every_measured_stub_shape(self, root, hosts):
        assert DEFAULT_MAX_SIZE >= max(STUBS.values())
        h = hosts["localhost"]
        _seed_stubs(h)
        h.seed_filler(400)
        stats = _run(h, root)
        assert stats.counts()["gc_candidate"] == 4

    def test_cap_is_reapplied_from_the_host_at_delete_time(self, root, hosts):
        """A row that PASSES classification but is oversized on the host at
        delete time is still refused — the check re-reads the host's row."""
        h = hosts["localhost"]
        h.seed(STUB_Z, EMPTY_CONTENT_SHA256, 3)
        rows = classify_rows(
            select_stub_rows(h, storage_location=LOC),
            read_root=str(root),
            dest_prefix=DEST,
            max_size=8,
        )
        assert rows[0].cls == "gc_candidate"
        h.set_size(STUB_Z, 10_000)  # grows between classification and delete
        res = apply_gc([None], rows, storage_location=LOC, max_size=8, dry_run=False)
        assert res["localhost"].deleted == 0
        assert res["localhost"].refused_oversized == [(f"{DEST}/{STUB_Z}", 10_000)]
        assert h.row_at(STUB_Z) is not None
        assert "DELETE" not in h.write_verbs()

    def test_digest_changing_before_delete_keeps_the_row(self, root, hosts):
        h = hosts["localhost"]
        h.seed(STUB_Z, EMPTY_CONTENT_SHA256, 3)
        rows = classify_rows(
            select_stub_rows(h, storage_location=LOC),
            read_root=str(root),
            dest_prefix=DEST,
        )
        h._db.execute("UPDATE archive_catalog SET content_sha256=?", (REAL_SHA,))
        h._db.commit()
        res = apply_gc([None], rows, storage_location=LOC, dry_run=False)
        assert res["localhost"].deleted == 0
        assert res["localhost"].digest_changed == [f"{DEST}/{STUB_Z}"]
        assert h.row_at(STUB_Z) is not None


# ========================================================= 6. fraction guard


class TestFractionGuard:
    def test_too_large_run_is_refused_with_a_problem(self, root, hosts):
        h = hosts["localhost"]
        _seed_stubs(h)  # 4 phantoms
        h.seed_filler(10)  # 4 / 14 = 28 % > 2 %
        stats = _run(h, root)
        assert stats.refused_fraction
        assert not stats.ok, "non-zero exit"
        assert any("REFUSED" in p and "--force" in p for p in stats.problems)
        assert stats.hosts == {}, "no host was even opened"
        assert h.nrows() == 14 and h.write_verbs() == []

    def test_force_overrides_the_fraction_guard(self, root, hosts):
        h = hosts["localhost"]
        _seed_stubs(h)
        h.seed_filler(10)
        stats = _run(h, root, force=True)
        assert not stats.refused_fraction
        assert stats.hosts["localhost"].deleted == 4
        assert h.nrows() == 10 and stats.ok

    def test_unknown_total_refuses(self, root, hosts):
        h = hosts["localhost"]
        _seed_stubs(h)
        rows = select_stub_rows(h, storage_location=LOC)
        stats = gc_catalog_rows(
            rows,
            hosts=[None],
            read_root=str(root),
            storage_location=LOC,
            dest_prefix=DEST,
            total_rows=None,
            dry_run=False,
        )
        assert stats.refused_fraction and not stats.ok
        assert h.nrows() == 4

    def test_cli_exit_code_is_nonzero_on_refusal(self, root, hosts, capsys):
        from receivers.cli.archive_sync import cmd_archive_catalog_gc

        h = hosts["localhost"]
        _seed_stubs(h)
        h.seed_filler(10)
        args = _cli_args(root, yes=True)
        with patch("receivers.archive.load_sync_config", return_value=[]):
            rc = cmd_archive_catalog_gc(args)
        assert rc == 1
        assert "REFUSED" in capsys.readouterr().out
        assert h.nrows() == 14


# =========================================================== 7. per-host symmetry


class TestPerHostSymmetry:
    def test_every_host_deletes_the_same_rows(self, root, hosts):
        a, b = hosts["localhost"], hosts["pgdev"]
        for h in (a, b):
            _seed_stubs(h)
            h.seed_filler(400)
        stats = _run(a, root, host_list=[None, "pgdev"])
        assert set(stats.hosts) == {"localhost", "pgdev"}
        assert stats.hosts["localhost"].deleted == stats.hosts["pgdev"].deleted == 4
        assert not stats.diverged and stats.ok
        for h in (a, b):
            for rel in STUBS:
                assert h.row_at(rel) is None

    def test_host_with_a_different_count_is_divergence(self, root, hosts):
        a, b = hosts["localhost"], hosts["pgdev"]
        _seed_stubs(a)
        a.seed_filler(400)
        _seed_stubs(b, {STUB_Z: 3, STUB_EMPTY: 0})  # pgdev lacks two rows
        b.seed_filler(400)
        stats = _run(a, root, host_list=[None, "pgdev"])
        assert stats.hosts["localhost"].deleted == 4
        assert stats.hosts["pgdev"].deleted == 2
        assert len(stats.hosts["pgdev"].absent_on_host) == 2
        assert stats.diverged
        assert not stats.ok
        assert any("DIVERGED" in p for p in stats.problems)
        assert any("pgdev" in p and "NO row" in p for p in stats.problems)

    def test_host_error_is_divergence_and_other_host_still_reported(self, root, hosts):
        a, b = hosts["localhost"], hosts["pgdev"]
        for h in (a, b):
            _seed_stubs(h)
            h.seed_filler(400)
        b.fail_on = {"DELETE"}
        stats = _run(a, root, host_list=[None, "pgdev"])
        assert stats.hosts["localhost"].deleted == 4
        assert stats.hosts["pgdev"].error and "injected" in stats.hosts["pgdev"].error
        assert stats.diverged and not stats.ok
        assert b.nrows() == 404 and b.rollbacks >= 1, "failed host rolled back"

    def test_unreachable_host_is_reported(self, root, hosts):
        a = hosts["localhost"]
        _seed_stubs(a)
        a.seed_filler(400)

        def _connect(host_override=None, database=None, single_host=False):
            if host_override == "pgdev":
                raise ConnectionError("pgdev unreachable")
            return hosts["localhost"]

        with patch("receivers.db.connection.get_connection", side_effect=_connect):
            stats = _run(a, root, host_list=[None, "pgdev"])
        assert (
            stats.hosts["pgdev"].error and "unreachable" in stats.hosts["pgdev"].error
        )
        assert stats.diverged and not stats.ok

    def test_gc_hosts_diverged_helper(self):
        from receivers.archive.catalog_gc import HostGcResult

        assert not gc_hosts_diverged(
            {"a": HostGcResult(deleted=2), "b": HostGcResult(deleted=2)}
        )
        assert gc_hosts_diverged(
            {"a": HostGcResult(deleted=2), "b": HostGcResult(deleted=1)}
        )
        assert gc_hosts_diverged(
            {"a": HostGcResult(deleted=2), "b": HostGcResult(error="x")}
        )

    def test_delete_connections_are_single_host(self, root, hosts):
        """A delete is explicit per host — never the dual-write mirror."""
        a = hosts["localhost"]
        _seed_stubs(a)
        a.seed_filler(400)
        calls = []

        def _connect(host_override=None, database=None, single_host=False):
            calls.append((host_override, single_host))
            return hosts[host_override or "localhost"]

        with patch("receivers.db.connection.get_connection", side_effect=_connect):
            _run(a, root, host_list=[None, "pgdev"])
        assert calls == [(None, True), ("pgdev", True)]


# ================================================= 8. natural key, never id


class TestNaturalKeyOnly:
    def test_delete_is_keyed_on_the_natural_key_never_id(self, root, hosts):
        a, b = hosts["localhost"], hosts["pgdev"]
        # Seed in DIFFERENT orders so the surrogate ids differ per host.
        a.seed_filler(5)
        _seed_stubs(a)
        _seed_stubs(b)
        b.seed_filler(5)
        for h in (a, b):
            h.seed_filler(400)
        ids_a = {rel: a.row_at(rel)["id"] for rel in STUBS}
        ids_b = {rel: b.row_at(rel)["id"] for rel in STUBS}
        assert all(ids_a[r] != ids_b[r] for r in STUBS), "ids must differ to prove it"

        stats = _run(a, root, host_list=[None, "pgdev"])

        assert stats.hosts["localhost"].deleted == stats.hosts["pgdev"].deleted == 4
        for h in (a, b):
            for rel in STUBS:
                assert h.row_at(rel) is None
            assert h.nrows() == 405
            for verb, sql, params in h.statements:
                if verb == "DELETE":
                    assert re.search(r"\bid\b", sql) is None, sql
                    for col in (
                        "storage_location",
                        "session_type",
                        "file_category",
                        "canonical_key",
                    ):
                        assert col in sql
                    assert params[0] == LOC
                    assert not any(isinstance(p, int) for p in params), params

    def test_null_session_type_uses_is_null(self, root, hosts):
        """A sessionless row's key predicate is IS NULL (never `= NULL`)."""
        h = hosts["localhost"]
        h._db.execute(
            """INSERT INTO archive_catalog
               (storage_location, station, file_date, session_type, file_category,
                canonical_key, file_path, file_size, content_sha256)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                LOC,
                "HLID",
                date(2017, 3, 1),
                None,
                "rinex",
                "HLID0600.17D",
                f"{DEST}/2017/mar/HLID/rinex/HLID0600.17D.Z",
                3,
                EMPTY_CONTENT_SHA256,
            ),
        )
        h._db.commit()
        h.seed_filler(400)
        stats = _run(h, root)
        assert stats.hosts["localhost"].deleted == 1
        assert h.nrows() == 400
        dels = [s for v, s, _p in h.statements if v == "DELETE"]
        assert dels and "session_type IS NULL" in dels[0]


# ==================================================== 9. breakdown by station/year/session


class TestBreakdown:
    def test_counts_by_station_year_and_session(self, root, hosts):
        h = hosts["localhost"]
        _seed_stubs(h)
        h.seed_filler(400)
        stats = _run(h, root, dry_run=True)
        bd = stats.breakdown()
        assert bd["by_station"] == {"HLID": 1, "ROTH": 2, "THOB": 1}
        assert bd["by_year"] == {"2017": 1, "2019": 2, "2022": 1}
        assert bd["by_session"] == {"15s_24hr": 2, "1Hz_1hr": 2}
        assert stats.to_dict()["breakdown"] == bd

    def test_breakdown_counts_candidates_only(self, root, hosts):
        h = hosts["localhost"]
        _seed_stubs(h, {STUB_Z: 3})
        h.seed(LIVE_STUB, EMPTY_CONTENT_SHA256, 0)
        _touch(root, LIVE_STUB)
        stats = _run(h, root, dry_run=True)
        assert stats.breakdown()["by_station"] == {"HLID": 1}


# ============================================== 10. discriminator is imported


class TestDiscriminatorIsShared:
    def test_empty_digest_is_imported_not_redeclared(self):
        assert catalog_gc.EMPTY_CONTENT_SHA256 is content_hash.EMPTY_CONTENT_SHA256
        src = inspect.getsource(catalog_gc)
        assert "from ..utils.content_hash import EMPTY_CONTENT_SHA256" in src
        assert "e3b0c442" not in src, "the literal digest must not be re-declared"
        assert "hashlib" not in src

    def test_path_mapping_is_verify_s_own_helper(self):
        from receivers.archive import verify

        assert catalog_gc._local_archive_path is verify._local_archive_path


# ================================================== other guards worth pinning


class TestScopeGuards:
    def test_rinex_org_rows_are_never_touched(self, root, hosts):
        h = hosts["localhost"]
        rel = "2017/mar/HLID/15s_24hr/rinex_org/HLID0600.17D.Z"
        h.seed(rel, EMPTY_CONTENT_SHA256, 3)
        h.seed_filler(400)
        stats = _run(h, root)
        assert stats.items[0].cls == "report_only"
        assert "rinex_org" in stats.items[0].reason
        assert h.row_at(rel) is not None

    def test_file_path_outside_dest_prefix_is_unmappable(self, root, hosts):
        """A misconfigured --dest-prefix must not make every row look absent."""
        h = hosts["localhost"]
        h.seed(STUB_Z, EMPTY_CONTENT_SHA256, 3, file_path=f"/elsewhere/{STUB_Z}")
        h.seed_filler(400)
        stats = _run(h, root)
        assert stats.items[0].cls == "report_only"
        assert "not under --dest-prefix" in stats.items[0].reason
        assert h.row_at(STUB_Z) is not None


# ---------------------------------------------------------------- CLI surface


def _cli_args(root, **over):
    base = dict(
        read_root=str(root),
        storage_location=LOC,
        dest_prefix=DEST,
        limit=None,
        max_size=DEFAULT_MAX_SIZE,
        fraction_limit=0.02,
        force=False,
        catalog_host=None,
        catalog_prod=False,
        config=None,
        host=None,
        verbose_rows=False,
        json=False,
        yes=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


class TestCli:
    def test_catalog_prod_with_no_catalog_hosts_refuses(self, root, hosts, capsys):
        from receivers.cli.archive_sync import cmd_archive_catalog_gc

        h = hosts["localhost"]
        _seed_stubs(h)
        with (
            patch("receivers.archive.load_sync_config", return_value=[]),
            patch("receivers.archive.resolve_catalog_hosts", return_value=[]),
        ):
            rc = cmd_archive_catalog_gc(_cli_args(root, catalog_prod=True, yes=True))
        assert rc == 2
        assert "catalog_hosts is unset" in capsys.readouterr().out
        assert h.nrows() == 4 and h.write_verbs() == []

    def test_unmounted_read_root_refuses(self, tmp_path, hosts, capsys):
        from receivers.cli.archive_sync import cmd_archive_catalog_gc

        h = hosts["localhost"]
        _seed_stubs(h)
        rc = cmd_archive_catalog_gc(_cli_args(tmp_path, yes=True))  # no YYYY dir
        assert rc == 2
        assert "unmounted" in capsys.readouterr().out
        assert h.nrows() == 4 and h.statements == []

    def test_cli_dry_run_by_default_and_yes_deletes(self, root, hosts, capsys):
        from receivers.cli.archive_sync import cmd_archive_catalog_gc

        h = hosts["localhost"]
        _seed_stubs(h)
        h.seed_filler(400)
        with patch("receivers.archive.load_sync_config", return_value=[]):
            rc = cmd_archive_catalog_gc(_cli_args(root))
        out = capsys.readouterr().out
        assert rc == 0 and "DRY-RUN" in out and "re-run with --yes" in out
        assert h.nrows() == 404
        with patch("receivers.archive.load_sync_config", return_value=[]):
            rc = cmd_archive_catalog_gc(_cli_args(root, yes=True))
        out = capsys.readouterr().out
        assert rc == 0 and "4 deleted" in out
        assert h.nrows() == 400

    def test_cli_json(self, root, hosts, capsys):
        import json as _json

        from receivers.cli.archive_sync import cmd_archive_catalog_gc

        h = hosts["localhost"]
        _seed_stubs(h)
        h.seed_filler(400)
        with patch("receivers.archive.load_sync_config", return_value=[]):
            rc = cmd_archive_catalog_gc(_cli_args(root, json=True))
        assert rc == 0
        doc = _json.loads(capsys.readouterr().out)
        assert doc["dry_run"] and doc["counts"]["gc_candidate"] == 4
        assert doc["breakdown"]["by_station"] == {"HLID": 1, "ROTH": 2, "THOB": 1}
        assert doc["hosts"]["localhost"]["would_delete"] == 4

    def test_parser_is_registered(self):
        from receivers.cli.arguments import create_argument_parser

        parser = create_argument_parser()
        ns = parser.parse_args(["archive-catalog-gc", "--read-root", "/x"])
        assert ns.func.__name__ == "cmd_archive_catalog_gc"
        assert ns.yes is False and ns.max_size == DEFAULT_MAX_SIZE
        assert ns.fraction_limit == pytest.approx(0.02)


def test_gcstats_ok_semantics():
    s = GcStats()
    assert s.ok
    s.problems.append("x")
    assert not s.ok
