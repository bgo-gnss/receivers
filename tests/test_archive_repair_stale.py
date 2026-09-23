"""``archive-repair-stale`` re-hashes ONLY provably-stale catalog rows.

A verify hash mismatch has two causes — a stale catalog hash (the archive
file was rewritten after cataloguing; re-hash is the fix) and genuine archive
corruption (re-hashing would BLESS it). These tests pin the guard between them
and prove the write lands on every host.

The DB double is a real in-memory sqlite ``archive_catalog`` + ``file_tracking``
driven through the module's own ``%s`` SQL (the reindex upsert included), so
"the row now holds the on-disk hash", "no INSERT/UPDATE ran" and "host B never
took the write" are properties of STORED STATE, not of a recorded SQL string.
No network: ``get_connection`` is patched to hand out one fake per host label.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sqlite3
from datetime import date
from unittest.mock import patch

import pytest

from receivers.archive.repair_stale import (
    RepairStats,
    classify_candidates,
    recheck_repaired,
    repair_stale_rows,
)
from receivers.archive.verify import VerifyStats, _row_record
from receivers.utils.canonical_key import canonical_key
from receivers.utils.content_hash import EMPTY_CONTENT_SHA256, content_sha256

LOC = "imo_archive"
DEST = "~/gpsdata"

DAILY_RAW = "2026/feb/ELDC/15s_24hr/raw/ELDC202602100000a.sbf.gz"
DAILY_RINEX = "2026/feb/ELDC/15s_24hr/rinex/ELDC0410.26D.gz"
HOURLY_RINEX = "2026/feb/THOB/1Hz_1hr/rinex/THOB041o.26d.gz"

OLD = b"pre-rewrite RINEX header + body\n" * 50
NEW = b"post-fix-headers RINEX header + body\n" * 50

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
    md5checksum       TEXT,
    md5uncompressed   TEXT,
    file_tracking_id  INTEGER,
    indexed_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_verified_at  TEXT,
    CONSTRAINT archive_catalog_logical_key
        UNIQUE (storage_location, session_type, file_category, canonical_key)
);
CREATE TABLE file_tracking (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sid            TEXT NOT NULL,
    session_type   TEXT NOT NULL,
    file_date      DATE,
    file_hour      INTEGER,
    content_sha256 TEXT
);
"""


class _Cursor:
    """psycopg2-shaped cursor over sqlite: ``%s`` → ``?``, ``now()`` →
    ``CURRENT_TIMESTAMP``, records every statement's verb; can be told to
    fail on a verb, or to SWALLOW one (record it, report success, execute
    nothing — the lagging-host simulation)."""

    def __init__(self, cur, conn):
        self._cur = cur
        self._conn = conn

    def execute(self, sql, params=None):
        verb = sql.strip().split()[0].upper()
        self._conn.statements.append((verb, sql, params))
        if verb in self._conn.fail_on:
            raise RuntimeError(f"injected failure on {verb}")
        if verb in self._conn.swallow:
            return None
        sql = sql.replace("%s", "?").replace("now()", "CURRENT_TIMESTAMP")
        return self._cur.execute(sql, params or ())

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
    """One gps_health host: in-memory sqlite ``archive_catalog`` + ``file_tracking``."""

    def __init__(self, label="localhost"):
        self.label = label
        self._db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self.statements: list = []
        self.fail_on: set = set()
        self.swallow: set = set()
        self.commits = 0
        self.rollbacks = 0

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
        pass  # the sqlite handle stays open — the same fake is reused per host

    # -- seeding ------------------------------------------------------------
    def seed_catalog(self, rel, sha, *, file_path=None):
        from receivers.archive.path_parse import parse_archive_path

        p = parse_archive_path(rel, "")
        assert p is not None, rel
        self._db.execute(
            """INSERT INTO archive_catalog
               (storage_location, station, file_date, file_hour, session_type,
                file_category, canonical_key, file_path, compression, file_size,
                content_sha256)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                LOC,
                p.station,
                p.file_date,
                p.file_hour,
                p.session_type,
                p.file_category,
                canonical_key(rel),
                file_path or f"{DEST}/{rel}",
                ".gz",
                len(OLD),
                sha,
            ),
        )
        self._db.commit()

    def seed_tracking(self, rel, sha):
        from receivers.archive.path_parse import parse_archive_path
        from receivers.archive.verify import _local_session

        p = parse_archive_path(rel, "")
        self._db.execute(
            """INSERT INTO file_tracking (sid, session_type, file_date, file_hour,
                                          content_sha256) VALUES (?,?,?,?,?)""",
            (
                p.station,
                _local_session(p.session_type, p.file_category),
                p.file_date,
                p.file_hour,
                sha,
            ),
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


def _write_gz(root, rel, payload):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(path, "wb") as fh:
        fh.write(payload)
    return path


def _verify_row(root, rel, catalog_sha, *, on_disk=None):
    """A ``VerifyStats.mismatched_rows`` entry EXACTLY as verify.py records it
    (``_row_record`` + ``on_disk_sha256``) — the contract this verb consumes."""
    from receivers.archive.path_parse import parse_archive_path

    p = parse_archive_path(rel, "")
    rec = _row_record(
        p.station,
        p.session_type,
        p.file_category,
        p.file_date,
        f"{DEST}/{rel}",
        catalog_sha,
        canonical_key(rel),
        str(root / rel),
    )
    rec["on_disk_sha256"] = (
        on_disk if on_disk is not None else content_sha256(root / rel)
    )
    return rec


@pytest.fixture
def hosts():
    """Two catalog hosts (the identical-catalog set) + get_connection patched to
    hand them out by label (``None`` → localhost)."""
    fakes = {"localhost": FakeHost("localhost"), "pgdev": FakeHost("pgdev")}

    def _connect(host_override=None, database=None, single_host=False):
        return fakes[host_override or "localhost"]

    with patch("receivers.db.connection.get_connection", side_effect=_connect):
        yield fakes


def _run(rows, root, tracking, *, host_list=(None,), dry_run=False, **kw):
    return repair_stale_rows(
        rows,
        hosts=list(host_list),
        read_root=str(root),
        storage_location=LOC,
        dest_prefix=DEST,
        tracking_conn=tracking,
        dry_run=dry_run,
        **kw,
    )


def _stale_setup(root, hosts, rel=DAILY_RINEX, *, which=("localhost",), tracking=True):
    """The measured stale signature: catalog holds OLD, file_tracking holds
    OLD, the archive file now holds NEW."""
    old_sha = content_sha256(_write_gz(root, rel, OLD))
    _write_gz(root, rel, NEW)  # rewritten after cataloguing
    for label in which:
        hosts[label].seed_catalog(rel, old_sha)
    if tracking:
        hosts["localhost"].seed_tracking(rel, old_sha)
    return old_sha, content_sha256(root / rel)


# ============================================================ 1. stale → repaired


class TestProvablyStaleIsRepaired:
    def test_stale_row_is_rehashed_to_the_on_disk_hash(self, tmp_path, hosts):
        old_sha, new_sha = _stale_setup(tmp_path, hosts)
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        stats = _run(rows, tmp_path, hosts["localhost"])

        (item,) = stats.items
        assert item.cls == "stale_confirmed"
        assert item.local_sha256 == old_sha and item.on_disk_sha256 == new_sha
        assert stats.counts()["repaired"] == 1
        row = hosts["localhost"].row_at(DAILY_RINEX)
        assert (
            row["content_sha256"] == new_sha == content_sha256(tmp_path / DAILY_RINEX)
        )
        assert row["file_path"] == f"{DEST}/{DAILY_RINEX}"
        assert hosts["localhost"].nrows() == 1, "a repair never duplicates a row"
        # E4 proved it, and nothing was flagged.
        assert stats.recheck["localhost"].matched == 1
        assert stats.recheck_ok and stats.ok and not stats.problems

    def test_hourly_row_is_corroborated_by_its_own_hour(self, tmp_path, hosts):
        """The verify cross-check only ever looks at file_hour IS NULL; the
        repair keys on the full slot so an hourly product is confirmed by its
        own record (and not left forever 'unconfirmed')."""
        old_sha, new_sha = _stale_setup(tmp_path, hosts, rel=HOURLY_RINEX)
        rows = [_verify_row(tmp_path, HOURLY_RINEX, old_sha)]

        stats = _run(rows, tmp_path, hosts["localhost"])

        assert stats.items[0].cls == "stale_confirmed"
        assert stats.items[0].file_hour == 14
        assert hosts["localhost"].row_at(HOURLY_RINEX)["content_sha256"] == new_sha


# ============================================ 2. undecompressable → NEVER repaired


class TestBlessTheCorruptionGuard:
    def test_undecompressable_file_is_never_repaired(self, tmp_path, hosts):
        old_sha = content_sha256(_write_gz(tmp_path, DAILY_RINEX, OLD))
        hosts["localhost"].seed_catalog(DAILY_RINEX, old_sha)
        hosts["localhost"].seed_tracking(DAILY_RINEX, old_sha)
        # Truncate the archive copy: gzip trailer gone → CorruptArchiveFileError.
        path = tmp_path / DAILY_RINEX
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
        # The verify pass would have failed to hash it too, but a row may still
        # arrive here (e.g. the file degraded between verify and repair) — the
        # guard must hold at repair time regardless of what verify saw.
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha, on_disk="f" * 64)]

        stats = _run(rows, tmp_path, hosts["localhost"])

        (item,) = stats.items
        assert item.cls == "undecompressable"
        assert item.on_disk_sha256 is None
        assert stats.counts()["undecompressable"] == 1
        assert stats.counts()["repaired"] == 0
        assert hosts["localhost"].write_verbs() == []
        assert hosts["localhost"].row_at(DAILY_RINEX)["content_sha256"] == old_sha
        assert stats.hosts == {} and stats.recheck == {}
        assert not stats.ok and any("corruption" in p for p in stats.problems)

    def test_undecompressable_is_not_rescued_by_include_unconfirmed(
        self, tmp_path, hosts
    ):
        """The opt-in widens the SHAPE evidence, never the decompress guard."""
        old_sha = content_sha256(_write_gz(tmp_path, DAILY_RINEX, OLD))
        hosts["localhost"].seed_catalog(DAILY_RINEX, old_sha)
        path = tmp_path / DAILY_RINEX
        path.write_bytes(path.read_bytes()[:20])
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha, on_disk="f" * 64)]

        stats = _run(rows, tmp_path, hosts["localhost"], include_unconfirmed=True)

        assert stats.items[0].cls == "undecompressable"
        assert hosts["localhost"].write_verbs() == []

    def test_stub_that_decompresses_to_nothing_is_report_only(self, tmp_path, hosts):
        """A gzip-of-nothing IS decompressable but is a stub: re-hashing it
        would catalogue a phantom (the class the backfill guard exists for)."""
        old_sha = content_sha256(_write_gz(tmp_path, DAILY_RINEX, OLD))
        hosts["localhost"].seed_catalog(DAILY_RINEX, old_sha)
        hosts["localhost"].seed_tracking(DAILY_RINEX, old_sha)
        _write_gz(tmp_path, DAILY_RINEX, b"")
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        stats = _run(rows, tmp_path, hosts["localhost"])

        assert stats.items[0].cls == "report_only"
        assert stats.items[0].on_disk_sha256 == EMPTY_CONTENT_SHA256
        assert hosts["localhost"].write_verbs() == []
        assert not stats.ok


# ================================================= 3. unconfirmed → opt-in only


class TestUnconfirmedNeedsOptIn:
    def test_no_tracking_record_is_skipped_by_default(self, tmp_path, hosts):
        old_sha, new_sha = _stale_setup(tmp_path, hosts, tracking=False)
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        stats = _run(rows, tmp_path, hosts["localhost"])

        (item,) = stats.items
        assert item.cls == "stale_unconfirmed"
        assert item.local_sha256 is None
        assert stats.counts()["unconfirmed_skipped"] == 1
        assert stats.counts()["repaired"] == 0
        assert hosts["localhost"].write_verbs() == []
        assert hosts["localhost"].row_at(DAILY_RINEX)["content_sha256"] == old_sha
        # A deliberate skip is not a failure — the operator sees it and decides.
        assert stats.ok

    def test_no_tracking_record_is_repaired_with_opt_in(self, tmp_path, hosts):
        old_sha, new_sha = _stale_setup(tmp_path, hosts, tracking=False)
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        stats = _run(rows, tmp_path, hosts["localhost"], include_unconfirmed=True)

        assert stats.items[0].cls == "stale_unconfirmed"
        assert stats.counts() == {
            "repaired": 1,
            "unconfirmed_skipped": 0,
            "report_only": 0,
            "undecompressable": 0,
            "already_current": 0,
        }
        assert hosts["localhost"].row_at(DAILY_RINEX)["content_sha256"] == new_sha
        assert stats.recheck["localhost"].matched == 1 and stats.ok

    def test_no_tracking_connection_means_unconfirmed(self, tmp_path, hosts):
        old_sha, _ = _stale_setup(tmp_path, hosts)
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]
        stats = _run(rows, tmp_path, None)
        assert stats.items[0].cls == "stale_unconfirmed"
        assert hosts["localhost"].write_verbs() == []


# ================================================= shapes that are report-only


class TestNotTheStaleSignature:
    def test_local_equals_on_disk_is_report_only(self, tmp_path, hosts):
        """file_tracking == on-disk, catalog the odd one out — plausible, but
        NOT the measured signature; the spec says report, do not repair."""
        old_sha, new_sha = _stale_setup(tmp_path, hosts, tracking=False)
        hosts["localhost"].seed_tracking(DAILY_RINEX, new_sha)
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        stats = _run(rows, tmp_path, hosts["localhost"])

        assert stats.items[0].cls == "report_only"
        assert "file_tracking == on-disk" in stats.items[0].reason
        assert hosts["localhost"].write_verbs() == [] and not stats.ok

    def test_three_way_divergence_is_report_only(self, tmp_path, hosts):
        old_sha, _ = _stale_setup(tmp_path, hosts, tracking=False)
        hosts["localhost"].seed_tracking(DAILY_RINEX, "c" * 64)
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]
        stats = _run(rows, tmp_path, hosts["localhost"])
        assert stats.items[0].cls == "report_only"
        assert "three-way" in stats.items[0].reason
        assert hosts["localhost"].write_verbs() == []

    def test_file_changed_since_verify_is_report_only(self, tmp_path, hosts):
        old_sha, _ = _stale_setup(tmp_path, hosts)
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]
        _write_gz(tmp_path, DAILY_RINEX, b"changed AGAIN after verify\n" * 50)
        stats = _run(rows, tmp_path, hosts["localhost"])
        assert stats.items[0].cls == "report_only"
        assert "flux" in stats.items[0].reason
        assert hosts["localhost"].write_verbs() == []

    def test_path_outside_read_root_is_report_only(self, tmp_path, hosts):
        old_sha, _ = _stale_setup(tmp_path, hosts)
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]
        rows[0]["local_path"] = "/somewhere/else/" + DAILY_RINEX
        stats = _run(rows, tmp_path, hosts["localhost"])
        assert stats.items[0].cls == "report_only"
        assert "read-root" in stats.items[0].reason
        assert hosts["localhost"].write_verbs() == []


# ======================================================= 4. dry-run writes nothing


class TestDryRunIsReadOnly:
    def test_dry_run_writes_nothing(self, tmp_path, hosts):
        old_sha, new_sha = _stale_setup(tmp_path, hosts, which=("localhost", "pgdev"))
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        stats = _run(
            rows, tmp_path, hosts["localhost"], host_list=(None, "pgdev"), dry_run=True
        )

        assert stats.dry_run
        assert stats.items[0].cls == "stale_confirmed"
        assert stats.counts()["repaired"] == 1  # "would repair"
        for h in hosts.values():
            assert h.write_verbs() == [], f"{h.label} took a write on a dry run"
            assert h.row_at(DAILY_RINEX)["content_sha256"] == old_sha
        # The reindex dry-run still says what it WOULD do, per host.
        assert stats.hosts["localhost"].updated == 1
        assert stats.hosts["pgdev"].updated == 1
        assert not stats.diverged
        # Nothing to prove on a dry run.
        assert stats.recheck == {} and stats.recheck_ok and stats.ok


# ============================================ 5. only_existing — never an INSERT


class TestOnlyExisting:
    def test_row_absent_on_a_host_is_never_inserted(self, tmp_path, hosts):
        """pgdev lacks the row: the repair must not create it there (that is
        catalog-coverage expansion, not repair) — and must say so loudly."""
        old_sha, new_sha = _stale_setup(tmp_path, hosts, which=("localhost",))
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        stats = _run(rows, tmp_path, hosts["localhost"], host_list=(None, "pgdev"))

        assert hosts["localhost"].row_at(DAILY_RINEX)["content_sha256"] == new_sha
        assert hosts["pgdev"].nrows() == 0
        assert hosts["pgdev"].write_verbs() == []
        assert stats.hosts["pgdev"].skipped_new == 1
        assert stats.hosts["pgdev"].inserted == 0
        assert stats.diverged
        assert stats.recheck["pgdev"].absent == [f"{DEST}/{DAILY_RINEX}"]
        assert not stats.ok
        assert any("NO row" in p for p in stats.problems)

    def test_reindex_is_called_with_only_existing(self, tmp_path, hosts):
        old_sha, _ = _stale_setup(tmp_path, hosts)
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]
        with patch(
            "receivers.archive.repair_stale.reindex_files_multi", return_value={}
        ) as m:
            _run(rows, tmp_path, hosts["localhost"])
        assert m.call_count == 1
        kwargs = m.call_args.kwargs
        assert kwargs["only_existing"] is True
        assert kwargs["root"] == str(tmp_path)
        assert m.call_args.args[1] == [str(tmp_path / DAILY_RINEX)]


# ======================================= 6. E4 re-check catches a lagging host


class TestRecheckProvesTheWrite:
    def test_recheck_catches_a_host_that_did_not_take_the_write(self, tmp_path, hosts):
        """pgdev acknowledges the upsert but stores nothing (a lagging /
        misbehaving host). The reindex reports 'updated' on BOTH hosts; only
        the independent re-read by file_path exposes it."""
        old_sha, new_sha = _stale_setup(tmp_path, hosts, which=("localhost", "pgdev"))
        hosts["pgdev"].swallow = {"INSERT"}
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        stats = _run(rows, tmp_path, hosts["localhost"], host_list=(None, "pgdev"))

        # The reindex's own accounting is symmetric — it cannot see the lag.
        assert stats.hosts["localhost"].updated == 1
        assert stats.hosts["pgdev"].updated == 1
        assert not stats.diverged
        # Stored state disagrees, and E4 says so.
        assert hosts["localhost"].row_at(DAILY_RINEX)["content_sha256"] == new_sha
        assert hosts["pgdev"].row_at(DAILY_RINEX)["content_sha256"] == old_sha
        assert stats.recheck["localhost"].matched == 1
        assert stats.recheck["localhost"].ok
        pg = stats.recheck["pgdev"]
        assert pg.matched == 0 and not pg.ok
        assert pg.mismatched == [(f"{DEST}/{DAILY_RINEX}", old_sha, new_sha)]
        assert not stats.recheck_ok and not stats.ok
        assert any("re-check on pgdev" in p for p in stats.problems)

    def test_recheck_reads_by_file_path_never_by_id(self, tmp_path, hosts):
        old_sha, _ = _stale_setup(tmp_path, hosts, which=("localhost", "pgdev"))
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]
        _run(rows, tmp_path, hosts["localhost"], host_list=(None, "pgdev"))
        selects = [
            s for h in hosts.values() for v, s, _p in h.statements if v == "SELECT"
        ]
        recheck_selects = [s for s in selects if "file_path = %s" in s]
        assert len(recheck_selects) == 2, "one re-read per host, keyed on file_path"
        assert not any("WHERE id" in s for s in selects)

    def test_recheck_reports_an_unreachable_host(self, tmp_path, hosts):
        old_sha, _ = _stale_setup(tmp_path, hosts, which=("localhost", "pgdev"))
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]
        repair_set = classify_candidates(
            rows,
            read_root=str(tmp_path),
            dest_prefix=DEST,
            tracking_conn=hosts["localhost"],
        )
        hosts["pgdev"].fail_on = {"SELECT"}
        results, unhashable = recheck_repaired(
            [None, "pgdev"], repair_set, storage_location=LOC
        )
        assert unhashable == []
        assert results["localhost"].error is None
        assert results["pgdev"].error and "injected failure" in results["pgdev"].error
        assert not results["pgdev"].ok


# ==================================== 7. per-host symmetry, loud on divergence


class TestPerHostSymmetry:
    def test_identical_catalog_set_lands_identically(self, tmp_path, hosts):
        old_sha, new_sha = _stale_setup(tmp_path, hosts, which=("localhost", "pgdev"))
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        stats = _run(rows, tmp_path, hosts["localhost"], host_list=(None, "pgdev"))

        for h in hosts.values():
            assert h.row_at(DAILY_RINEX)["content_sha256"] == new_sha
            assert h.nrows() == 1
        assert {k: v.updated for k, v in stats.hosts.items()} == {
            "localhost": 1,
            "pgdev": 1,
        }
        assert {k: v.matched for k, v in stats.recheck.items()} == {
            "localhost": 1,
            "pgdev": 1,
        }
        assert not stats.diverged and stats.recheck_ok and stats.ok

    def test_host_that_errors_is_loud_divergence(self, tmp_path, hosts):
        old_sha, new_sha = _stale_setup(tmp_path, hosts, which=("localhost", "pgdev"))
        hosts["pgdev"].fail_on = {"INSERT"}
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        stats = _run(rows, tmp_path, hosts["localhost"], host_list=(None, "pgdev"))

        assert stats.hosts["pgdev"] is None
        assert stats.diverged
        assert hosts["localhost"].row_at(DAILY_RINEX)["content_sha256"] == new_sha
        assert hosts["pgdev"].row_at(DAILY_RINEX)["content_sha256"] == old_sha
        assert stats.recheck["pgdev"].mismatched  # and E4 sees the old hash too
        assert not stats.ok
        assert any("FAILED on pgdev" in p for p in stats.problems)
        assert any("DIVERGED" in p for p in stats.problems)

    def test_to_dict_is_json_safe_and_carries_every_surface(self, tmp_path, hosts):
        import json

        old_sha, _ = _stale_setup(tmp_path, hosts, which=("localhost", "pgdev"))
        rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]
        stats = _run(rows, tmp_path, hosts["localhost"], host_list=(None, "pgdev"))
        d = json.loads(json.dumps(stats.to_dict()))
        assert set(d["counts"]) == {
            "repaired",
            "unconfirmed_skipped",
            "report_only",
            "undecompressable",
            "already_current",
        }
        assert set(d["hosts"]) == set(d["recheck"]) == {"localhost", "pgdev"}
        assert d["recheck"]["pgdev"]["matched"] == 1
        assert d["items"][0]["class"] == "stale_confirmed"
        assert d["ok"] is True and d["problems"] == []


# ============================================ 8. already current → not rewritten


class TestAlreadyCurrent:
    def test_on_disk_equals_catalog_is_not_rewritten(self, tmp_path, hosts):
        """The row was repaired between the verify pass and now (or the verify
        raced a push): the catalog already holds the on-disk hash."""
        old_sha = content_sha256(_write_gz(tmp_path, DAILY_RINEX, OLD))
        new_sha = content_sha256(_write_gz(tmp_path, DAILY_RINEX, NEW))
        hosts["localhost"].seed_catalog(DAILY_RINEX, new_sha)
        hosts["localhost"].seed_tracking(DAILY_RINEX, old_sha)
        # verify saw catalog=OLD at the time; the row has since been fixed.
        rows = [_verify_row(tmp_path, DAILY_RINEX, new_sha)]

        stats = _run(rows, tmp_path, hosts["localhost"])

        (item,) = stats.items
        assert item.cls == "already_current"
        assert stats.counts()["already_current"] == 1
        assert stats.counts()["repaired"] == 0
        assert hosts["localhost"].write_verbs() == []
        assert hosts["localhost"].row_at(DAILY_RINEX)["content_sha256"] == new_sha
        assert stats.ok


# ================================================================ mixed batch


class TestMixedBatch:
    def test_each_candidate_lands_in_exactly_one_bucket(self, tmp_path, hosts):
        # stale_confirmed
        old_a, new_a = _stale_setup(tmp_path, hosts, rel=DAILY_RINEX)
        # stale_unconfirmed
        old_b, new_b = _stale_setup(tmp_path, hosts, rel=HOURLY_RINEX, tracking=False)
        # undecompressable
        old_c = content_sha256(_write_gz(tmp_path, DAILY_RAW, OLD))
        hosts["localhost"].seed_catalog(DAILY_RAW, old_c)
        hosts["localhost"].seed_tracking(DAILY_RAW, old_c)
        pc = tmp_path / DAILY_RAW
        pc.write_bytes(pc.read_bytes()[:30])
        rows = [
            _verify_row(tmp_path, DAILY_RINEX, old_a),
            _verify_row(tmp_path, HOURLY_RINEX, old_b),
            _verify_row(tmp_path, DAILY_RAW, old_c, on_disk="f" * 64),
        ]

        stats = _run(rows, tmp_path, hosts["localhost"])

        assert [c.cls for c in stats.items] == [
            "stale_confirmed",
            "stale_unconfirmed",
            "undecompressable",
        ]
        assert stats.counts() == {
            "repaired": 1,
            "unconfirmed_skipped": 1,
            "report_only": 0,
            "undecompressable": 1,
            "already_current": 0,
        }
        assert sum(stats.counts().values()) == len(stats.items)
        h = hosts["localhost"]
        assert h.row_at(DAILY_RINEX)["content_sha256"] == new_a
        assert h.row_at(HOURLY_RINEX)["content_sha256"] == old_b
        assert h.row_at(DAILY_RAW)["content_sha256"] == old_c
        assert stats.recheck["localhost"].matched == 1
        assert not stats.ok  # the undecompressable one is a real problem

    def test_verify_unreadable_count_is_a_problem(self, tmp_path, hosts):
        """verify.mismatched also counts files it could not READ — those are
        not enumerable and must not vanish from the report."""
        stats = repair_stale_rows(
            [],
            hosts=[None],
            read_root=str(tmp_path),
            storage_location=LOC,
            dest_prefix=DEST,
            tracking_conn=hosts["localhost"],
            verify_unreadable=2,
        )
        assert not stats.ok
        assert any("2 file(s) unreadable" in p for p in stats.problems)


# ================================================================ CLI surface


def _ns(**over):
    base = dict(
        read_root=None,
        limit=10,
        storage_location=LOC,
        dest_prefix=DEST,
        catalog_host=None,
        catalog_prod=False,
        config=None,
        host=None,
        include_unconfirmed=False,
        json=False,
        yes=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


class TestCLI:
    def test_catalog_prod_with_unset_catalog_hosts_refuses(self, tmp_path, capsys):
        from receivers.cli.archive_sync import cmd_archive_repair_stale

        with (
            patch("receivers.archive.resolve_catalog_hosts", return_value=[]),
            patch("receivers.archive.load_sync_config", return_value=[]),
            patch("receivers.archive.verify_archive_catalog") as verify,
        ):
            rc = cmd_archive_repair_stale(
                _ns(read_root=str(tmp_path), catalog_prod=True, yes=True)
            )
        assert rc == 2
        assert "catalog_hosts is unset" in capsys.readouterr().out
        verify.assert_not_called()  # refused BEFORE touching any DB

    def test_default_is_dry_run_and_feeds_verify_rows_to_the_engine(
        self, tmp_path, hosts, capsys
    ):
        """Candidates come from verify's structured mismatched_rows, in-process;
        without --yes the engine runs dry and the catalog is untouched."""
        from receivers.cli.archive_sync import cmd_archive_repair_stale

        old_sha, new_sha = _stale_setup(tmp_path, hosts)
        vstats = VerifyStats(read_back=True, checked=3, verified=2, mismatched=1)
        vstats.mismatched_rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        with (
            patch("receivers.archive.load_sync_config", return_value=[]),
            patch(
                "receivers.archive.verify_archive_catalog", return_value=vstats
            ) as verify,
            patch(
                "receivers.cli.archive_sync._get_conn", return_value=hosts["localhost"]
            ),
        ):
            rc = cmd_archive_repair_stale(_ns(read_root=str(tmp_path)))

        assert rc == 0
        assert verify.call_args.kwargs["limit"] == 10
        assert verify.call_args.kwargs["read_root"] == str(tmp_path)
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert re.search(r"would repair\s+1\s", out), out
        assert hosts["localhost"].write_verbs() == []
        assert hosts["localhost"].row_at(DAILY_RINEX)["content_sha256"] == old_sha

    def test_yes_applies_and_json_carries_the_recheck(self, tmp_path, hosts, capsys):
        import json

        from receivers.cli.archive_sync import cmd_archive_repair_stale

        old_sha, new_sha = _stale_setup(tmp_path, hosts)
        vstats = VerifyStats(read_back=True, checked=1, mismatched=1)
        vstats.mismatched_rows = [_verify_row(tmp_path, DAILY_RINEX, old_sha)]

        with (
            patch("receivers.archive.load_sync_config", return_value=[]),
            patch("receivers.archive.verify_archive_catalog", return_value=vstats),
            patch(
                "receivers.cli.archive_sync._get_conn", return_value=hosts["localhost"]
            ),
        ):
            rc = cmd_archive_repair_stale(
                _ns(read_root=str(tmp_path), yes=True, json=True)
            )

        assert rc == 0
        d = json.loads(capsys.readouterr().out)
        assert d["dry_run"] is False
        assert d["counts"]["repaired"] == 1
        assert d["recheck"]["localhost"]["matched"] == 1
        assert d["verify"]["mismatched_enumerable"] == 1
        assert hosts["localhost"].row_at(DAILY_RINEX)["content_sha256"] == new_sha

    def test_exit_nonzero_when_a_candidate_is_undecompressable(
        self, tmp_path, hosts, capsys
    ):
        from receivers.cli.archive_sync import cmd_archive_repair_stale

        old_sha = content_sha256(_write_gz(tmp_path, DAILY_RINEX, OLD))
        hosts["localhost"].seed_catalog(DAILY_RINEX, old_sha)
        p = tmp_path / DAILY_RINEX
        p.write_bytes(p.read_bytes()[:20])
        vstats = VerifyStats(read_back=True, checked=1, mismatched=1)
        vstats.mismatched_rows = [
            _verify_row(tmp_path, DAILY_RINEX, old_sha, on_disk="f" * 64)
        ]
        with (
            patch("receivers.archive.load_sync_config", return_value=[]),
            patch("receivers.archive.verify_archive_catalog", return_value=vstats),
            patch(
                "receivers.cli.archive_sync._get_conn", return_value=hosts["localhost"]
            ),
        ):
            rc = cmd_archive_repair_stale(_ns(read_root=str(tmp_path), yes=True))
        assert rc == 1
        out = capsys.readouterr().out
        assert "NEVER re-hashed" in out and "undecompressable" in out
        assert hosts["localhost"].write_verbs() == []

    def test_parser_registers_the_verb_with_dry_run_default(self):
        from receivers.cli.arguments import create_argument_parser

        parser = create_argument_parser()
        a = parser.parse_args(["archive-repair-stale", "--read-root", "/x"])
        assert a.func.__name__ == "cmd_archive_repair_stale"
        assert a.yes is False and a.include_unconfirmed is False
        assert a.limit == 500 and a.storage_location == LOC
