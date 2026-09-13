"""Verify _enqueue_backfill RESETS a stale cursor instead of resurrecting it.

NEEDS A LIVE DB; lives in scripts/dev/ not tests/ for the same reason as
check_catalog_upsert_noop.py — the unit suite must never require a database.

    ssh bgo@rek-d01.vedur.is 'cd ~/git/receivers && ./venv/bin/python3 \
        scripts/dev/check_backfill_cursor_reset.py'

Exits 0 when every case behaves. Against the PRE-fix code (LEAST on next_date)
the "stale cursor is reset" case reports BAD, which is the discrimination that
makes this worth running.

Everything happens in one transaction that is rolled back, and it operates on a
synthetic sid, so production rows are never touched.

IMPORTANT — why DatabaseConnectionFactory.connection is patched below:
_enqueue_backfill takes no connection argument, it opens its OWN via the
factory. A first version of this script therefore (a) wrote outside the
transaction it believed it controlled, and (b) HUNG, because that second
connection waited on the row lock and the stations-FK held by the first. Patching
the factory to hand back this script's connection is what makes the rollback real
AND keeps the shipped SQL under test. Do not "simplify" it by re-typing the
upsert here — that is the mirror-test trap: the copy would pass while production
stayed broken.
"""

import sys

sys.path.insert(0, "/home/bgo/git/receivers/src")
import psycopg2  # noqa: E402

from contextlib import contextmanager  # noqa: E402

import receivers.health.database_factory as dbf  # noqa: E402
from receivers.scheduling.backfill import _enqueue_backfill  # noqa: E402

SID = "ZZZZ"
SESSION = "15s_24hr"
DAYS_BACK = 7


def row(cur):
    cur.execute(
        "SELECT status, backfill_start, next_date, backfill_end "
        "FROM backfill_progress WHERE sid=%s AND session_type=%s",
        (SID, SESSION),
    )
    return cur.fetchone()


def main():
    conn = psycopg2.connect(dbname="gps_health")
    conn.autocommit = False
    ok = True

    @contextmanager
    def _same_conn(*a, **kw):
        """Hand the shipped code THIS transaction, and never close/commit it."""
        yield conn

    dbf.DatabaseConnectionFactory.connection = staticmethod(_same_conn)

    try:
        with conn.cursor() as cur:
            # A synthetic station row is needed for the FK on backfill_progress.
            cur.execute(
                "INSERT INTO stations (sid) VALUES (%s) ON CONFLICT DO NOTHING", (SID,)
            )

            def seed(status, start, nxt, end):
                cur.execute("DELETE FROM backfill_progress WHERE sid=%s", (SID,))
                cur.execute(
                    "INSERT INTO backfill_progress (sid, session_type, "
                    "backfill_start, next_date, backfill_end, status) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (SID, SESSION, start, nxt, end, status),
                )

            def check(label, expect):
                nonlocal ok
                got = row(cur)
                good = got[2] == expect
                if not good:
                    ok = False
                print(
                    f"  {'OK ' if good else 'BAD'} {label:38s} "
                    f"next_date={got[2]} (want {expect}) status={got[0]}"
                )

            cur.execute("SELECT CURRENT_DATE - %s, CURRENT_DATE", (DAYS_BACK,))
            window_start, today = cur.fetchone()

            # 1. The bug: an old cursor on a completed row must NOT survive.
            seed("completed", "2026-06-14", "2026-05-17", "2026-08-10")
            _enqueue_backfill(SESSION, [SID], DAYS_BACK)
            check("stale cursor is reset", window_start)

            # 2. A healthy completed row is also reset to the window (same rule).
            seed("completed", "2026-06-14", today, today)
            _enqueue_backfill(SESSION, [SID], DAYS_BACK)
            check("healthy completed row reset", window_start)

            # 3. A failed row re-activates the same way.
            seed("failed", "2026-06-14", "2026-05-17", "2026-08-10")
            _enqueue_backfill(SESSION, [SID], DAYS_BACK)
            check("failed row reset", window_start)

            # 4. An in-flight row must be LEFT ALONE — never disturb a cursor
            #    a worker is currently advancing.
            seed("in_progress", "2026-06-14", "2026-05-17", "2026-08-10")
            _enqueue_backfill(SESSION, [SID], DAYS_BACK)
            import datetime

            check("in_progress UNTOUCHED", datetime.date(2026, 5, 17))

            # 5. backfill_end must only move forward, never shrink.
            seed("completed", "2026-06-14", "2026-05-17", "2099-01-01")
            _enqueue_backfill(SESSION, [SID], DAYS_BACK)
            got = row(cur)
            if got[3] == datetime.date(2099, 1, 1):
                print("  OK  backfill_end not shrunk               (GREATEST kept)")
            else:
                print(f"  BAD backfill_end shrunk to {got[3]}")
                ok = False
    finally:
        conn.rollback()
        conn.close()
        print("  (rolled back — no production rows changed)")
    return 0 if ok else 1


sys.exit(main())
