"""Verify archive_catalog's upsert suppresses no-op updates. NEEDS A LIVE DB.

Lives in scripts/dev/ and NOT in tests/ deliberately: the unit suite must never
require a database (see the "receivers tests hit production" lesson — the suite
used to open real receiver and TOS connections). Run it by hand against a host
whose schema you want to check:

    ssh bgo@rek-d01.vedur.is 'cd ~/git/receivers && ./venv/bin/python3 \
        /path/to/check_catalog_upsert_noop.py'

Exits 0 when every case behaves, 1 otherwise. Run it against the PRE-fix code to
confirm it discriminates: without the WHERE guard it reports BAD on all six
no-op cases and OK on all five real updates.

Exercises the SHIPPED upsert against the real schema, then ROLLBACKs.

Observable is pg_stat_xact_user_tables.n_tup_upd — the number of row updates
made by THIS transaction. indexed_at cannot be used: now() returns the
transaction start time, so it is constant across every statement in one
transaction (that mistake made a first version of this script report a
successful update as a failure).
"""

import sys

sys.path.insert(0, "/home/bgo/git/receivers/src")
import psycopg2
from receivers.archive.catalog import upsert_catalog_row

KEY = dict(
    storage_location="__d3_test__",
    station="ZZZZ",
    session_type="1Hz_1hr",
    file_category="raw",
)
COMMON = dict(
    KEY,
    filename="ZZZZ2560a.sbf.gz",
    archive_path="/tmp/a/ZZZZ2560a.sbf.gz",
    file_date="2026-09-13",
    file_hour=0,
)


def upd_count(cur):
    cur.execute(
        "SELECT coalesce(n_tup_upd,0) FROM pg_stat_xact_user_tables "
        "WHERE relname='archive_catalog'"
    )
    r = cur.fetchone()
    return r[0] if r else 0


def main():
    conn = psycopg2.connect(dbname="gps_health")
    conn.autocommit = False
    ok = True
    try:
        with conn.cursor() as cur:

            def step(label, expect_update, **kw):
                nonlocal ok
                before = upd_count(cur)
                upsert_catalog_row(conn, **{**COMMON, **kw})
                delta = upd_count(cur) - before
                got = delta > 0
                mark = "OK " if got == expect_update else "BAD"
                if got != expect_update:
                    ok = False
                want = "UPDATE" if expect_update else "NO-OP "
                print(
                    f"  {mark} {label:28s} want={want} got={'UPDATE' if got else 'NO-OP '} (n_tup_upd +{delta})"
                )

            # seed (an INSERT, so no update expected)
            upsert_catalog_row(conn, file_size=100, content_sha256=None, **COMMON)
            print("  --- seeded the row (insert) ---")

            step("identical re-write", False, file_size=100, content_sha256=None)
            step("changed file_size", True, file_size=200, content_sha256=None)
            step("same size again", False, file_size=200, content_sha256=None)
            step("incoming NULL sha256", False, file_size=200, content_sha256=None)
            step("new sha256 arrives", True, file_size=200, content_sha256="a" * 64)
            step("same sha256 again", False, file_size=200, content_sha256="a" * 64)
            step("different sha256", True, file_size=200, content_sha256="b" * 64)
            step("NULL sha256 over stored", False, file_size=200, content_sha256=None)
            step(
                "changed archive_path",
                True,
                file_size=200,
                content_sha256="b" * 64,
                archive_path="/tmp/b/ZZZZ2560a.sbf.gz",
            )
            step(
                "file_tracking_id filled",
                True,
                file_size=200,
                content_sha256="b" * 64,
                archive_path="/tmp/b/ZZZZ2560a.sbf.gz",
                file_tracking_id=42,
            )
            step(
                "same tracking_id again",
                False,
                file_size=200,
                content_sha256="b" * 64,
                archive_path="/tmp/b/ZZZZ2560a.sbf.gz",
                file_tracking_id=42,
            )
    finally:
        conn.rollback()
        conn.close()
        print("  (rolled back — no production rows changed)")
    return 0 if ok else 1


sys.exit(main())
