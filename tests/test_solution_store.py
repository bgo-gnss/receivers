"""Slice 2: migration 077, record_solution_check and solution_day_grid.

Runs against a DISPOSABLE local Postgres only. Set ``RECEIVERS_TEST_PG_DSN``
to an admin DSN on this machine (e.g. ``host=127.0.0.1 port=54329 user=bgo
dbname=postgres``); every test module gets its own freshly created database,
dropped afterwards. Without the variable these tests are skipped. The DSN is
refused unless it is local and not ``gps_health``; ``database.cfg`` and the
config singleton are never read.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from receivers.monitoring.solutions.store import (
    DestinationRefusedError,
    check_destination,
    record_check,
    record_to_destinations,
)

psycopg2 = pytest.importorskip("psycopg2")

MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations" / "077_daily_solutions.sql"
)
STREAM = "imo.24hr_itrf14"
UTC = dt.UTC
NOW = dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
TODAY = NOW.date()
W_FROM = TODAY - dt.timedelta(days=45)
W_TO = TODAY - dt.timedelta(days=1)
V1 = "a" * 64
V2 = "c" * 64
FP1 = "1" * 64
FP2 = "2" * 64


def d(k: int) -> dt.date:
    """``k`` days before today."""
    return TODAY - dt.timedelta(days=k)


# ----------------------------------------------------------------- fixtures


def _admin_dsn() -> str:
    dsn = os.environ.get("RECEIVERS_TEST_PG_DSN", "")
    if not dsn:
        pytest.skip("RECEIVERS_TEST_PG_DSN not set (no disposable Postgres)")
    from psycopg2.extensions import parse_dsn

    p = parse_dsn(dsn)
    host = p.get("host", "")
    if not host or p.get("hostaddr") or p.get("service"):
        pytest.fail("RECEIVERS_TEST_PG_DSN needs an explicit local host=")
    if any(os.environ.get(v) for v in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")):
        pytest.fail("unset PGHOSTADDR / PGSERVICE / PGSERVICEFILE for these tests")
    local = {"localhost", "127.0.0.1", "::1"}
    if not all(h in local or h.startswith("/") for h in host.split(",")):
        pytest.fail("RECEIVERS_TEST_PG_DSN must point at this machine")
    if p.get("dbname") == "gps_health":
        pytest.fail("RECEIVERS_TEST_PG_DSN must not be gps_health")
    return dsn


def _with_db(dsn: str, name: str) -> str:
    from psycopg2.extensions import make_dsn

    return make_dsn(dsn, dbname=name)


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    dsn = _admin_dsn()
    try:
        psycopg2.connect(dsn, connect_timeout=3).close()
    except psycopg2.OperationalError:
        pytest.skip("disposable Postgres not reachable")
    return dsn


def _create_db(admin_dsn: str, *, migrate: bool = True) -> str:
    name = f"sol_test_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(admin_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    dsn = _with_db(admin_dsn, name)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE schema_migrations (migration_name text PRIMARY KEY,"
            " applied_at timestamptz DEFAULT now())"
        )
        if migrate:
            cur.execute(MIGRATION.read_text())
    conn.close()
    return dsn


def _drop_db(admin_dsn: str, dsn: str) -> None:
    from psycopg2.extensions import parse_dsn

    name = parse_dsn(dsn)["dbname"]
    conn = psycopg2.connect(admin_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


@pytest.fixture()
def db(admin_dsn: str) -> Iterator[str]:
    dsn = _create_db(admin_dsn)
    try:
        yield dsn
    finally:
        _drop_db(admin_dsn, dsn)


def q(dsn: str, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


# ----------------------------------------------------------------- payloads

ROSTER_STATIONS = ("AFST", "NEWS", "THOB")


def roster(version: str = V1, stations: tuple[str, ...] = ROSTER_STATIONS) -> dict:
    intervals = []
    if "AFST" in stations:
        intervals.append(
            {"station": "AFST", "kind": "expected", "day_from": "2000-01-01"}
        )
    if "THOB" in stations:
        intervals += [
            {"station": "THOB", "kind": "expected", "day_from": "2000-01-01"},
            {"station": "THOB", "kind": "excluded", "day_from": d(10).isoformat(),
             "day_to": d(10).isoformat(), "reason": "antenna swap"},
        ]  # fmt: skip
    if "NEWS" in stations:
        intervals.append(
            {"station": "NEWS", "kind": "expected", "day_from": d(5).isoformat()}
        )
    return {
        "version": version,
        "reviewed_from": (TODAY - dt.timedelta(days=200)).isoformat(),
        "reviewed_to": None,
        "stations": list(stations),
        "intervals": intervals,
    }


def payload(
    *,
    checked_at: dt.datetime = NOW,
    w_from: dt.date = W_FROM,
    w_to: dt.date = W_TO,
    facts: dict[tuple[str, dt.date], tuple[str, str | None, str | None]] | None = None,
    default: tuple[str, str | None, str | None] = ("valid", None, FP1),
    outcome: str = "ok",
    run_id: str | None = None,
    ros: dict | None = None,
) -> dict[str, Any]:
    """A complete snapshot: every roster station x every window day."""
    ros = ros or roster()
    facts = facts or {}
    cells = []
    if outcome == "ok":
        day = w_from
        while day <= w_to:
            for sid in ros["stations"]:
                fact, code, fp = facts.get((sid, day), default)
                cells.append({"station": sid, "day": day.isoformat(), "fact": fact,
                              "code": code, "fingerprint": fp})  # fmt: skip
            day += dt.timedelta(days=1)
    return {
        "schema": "solution-check-facts/2",
        "run_id": run_id or str(uuid.uuid4()),
        "checked_at": checked_at.isoformat(),
        "stream": STREAM,
        "source": "/test/pos",
        "outcome": outcome,
        "error": None if outcome == "ok" else "boom",
        "window": {"from": w_from.isoformat(), "to": w_to.isoformat(),
                   "days": (w_to - w_from).days + 1},  # fmt: skip
        "roster": ros,
        "files": [{"station": "AFST", "path": "/test/pos/AFST.imo.24hr_itrf14.pos",
                   "sha256": FP1, "size": 1}],  # fmt: skip
        "cells": cells,
        "unexpected_files": [{"name": "ZZZZ.imo.24hr_itrf14.pos", "reason": "x"}],
        "other_products": {"imo.24hr_itrf08": 1},
    }


def grid(
    dsn: str, now: dt.datetime = NOW, day_from: dt.date | None = None,
    day_to: dt.date | None = None,
) -> dict[tuple[str, dt.date], tuple[str, str]]:  # fmt: skip
    rows = q(
        dsn,
        "SELECT station, day, state, reason FROM solution_day_grid(%s, %s, %s, %s)",
        (STREAM, day_from or d(80), day_to or d(-1), now),
    )
    return {(r[0], r[1]): (r[2], r[3]) for r in rows}


# ============================================================ the §3 rules


def test_rule1_outside_reviewed_coverage_is_unknown(db):
    record_check(db, payload())
    g = grid(db, day_from=TODAY - dt.timedelta(days=210))
    assert g[("AFST", TODAY - dt.timedelta(days=201))] == (
        "unknown",
        "roster_unreviewed",
    )


def test_rule2_excluded_and_not_expected(db):
    record_check(db, payload())
    g = grid(db)
    assert g[("THOB", d(10))] == ("not_expected", "roster_excluded")
    assert g[("NEWS", d(6))] == ("not_expected", "roster_not_expected")
    assert g[("NEWS", d(5))] == ("available", "verified")


def test_rule3_in_window_day_no_run_covers(db):
    record_check(db, payload(w_from=d(10)))  # covers d(10)..d(1) only
    g = grid(db)
    assert g[("AFST", d(20))] == ("unknown", "no_check_run")
    assert g[("AFST", d(9))] == ("available", "verified")


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("source_unavailable", "source_unavailable"), ("failed", "check_failed")],
)
def test_rule3_failed_latest_check_makes_window_unknown(db, outcome, reason):
    record_check(db, payload())
    record_check(db, payload(checked_at=NOW + dt.timedelta(hours=1), outcome=outcome))
    g = grid(db, now=NOW + dt.timedelta(hours=2))
    assert g[("AFST", d(3))] == ("unknown", reason)
    # outside the window history is kept, never hidden by the newer failure
    assert grid(db, now=NOW + dt.timedelta(days=60))[("AFST", d(3))] == (
        "available",
        "frozen",
    )


def test_runs_after_now_are_not_seen(db):
    record_check(db, payload())
    record_check(db, payload(checked_at=NOW + dt.timedelta(hours=1), outcome="failed"))
    assert grid(db, now=NOW + dt.timedelta(minutes=30))[("AFST", d(3))] == (
        "available",
        "verified",
    )


def test_rule3_stale_check(db):
    record_check(db, payload())
    # freshness_ttl 3 h: three missed hourly runs
    g = grid(db, now=NOW + dt.timedelta(hours=5))
    assert g[("AFST", d(3))] == ("unknown", "check_stale")
    assert grid(db, now=NOW + dt.timedelta(hours=3))[("AFST", d(3))][0] == "available"


def test_rule3_unverifiable_in_latest_run(db):
    record_check(db, payload())
    record_check(db, payload(
        checked_at=NOW + dt.timedelta(hours=1),
        facts={("AFST", d(3)): ("unverifiable", "unreadable", None)},
    ))  # fmt: skip
    g = grid(db, now=NOW + dt.timedelta(hours=1))
    assert g[("AFST", d(3))] == ("unknown", "file_unverifiable")


def test_rule3_fact_not_observed_by_the_latest_covering_run(db):
    record_check(db, payload())
    # A newer ok run row that carried no fact for this cell (defensive case)
    q(db, "INSERT INTO solution_check_run (run_id, stream, checked_at, window_from,"
          " window_to, outcome, source, roster_version, cell_count, payload_sha256)"
          " VALUES (%s, %s, %s, %s, %s, 'ok', 'x', %s, 0, %s)",
      (str(uuid.uuid4()), STREAM, NOW + dt.timedelta(hours=1), W_FROM, W_TO, V1, V1))  # fmt: skip
    g = grid(db, now=NOW + dt.timedelta(hours=1))
    assert g[("AFST", d(3))] == ("unknown", "not_in_last_check")


def test_rule3_precedes_invalid(db):
    record_check(
        db, payload(facts={("AFST", d(3)): ("invalid", "duplicate_date", None)})
    )
    assert grid(db)[("AFST", d(3))] == ("invalid", "duplicate_date")
    stale = grid(db, now=NOW + dt.timedelta(hours=27))
    assert stale[("AFST", d(3))] == ("unknown", "check_stale")


def test_rule4_invalid_carries_the_code(db):
    record_check(
        db, payload(facts={("AFST", d(4)): ("invalid", "session_mismatch", FP2)})
    )
    assert grid(db)[("AFST", d(4))] == ("invalid", "session_mismatch")


def test_rule5_verified_in_window_frozen_after(db):
    record_check(db, payload())
    assert grid(db)[("AFST", d(3))] == ("available", "verified")
    later = grid(db, now=NOW + dt.timedelta(days=50))
    assert later[("AFST", d(3))] == ("available", "frozen")


@pytest.mark.parametrize(
    ("checked", "now", "expected"),
    [
        ("09:00", "09:14", ("pending", "before_deadline")),
        # absence seen at 09:00 does not prove lateness at 09:15
        ("09:00", "09:15", ("pending", "awaiting_recheck")),
        ("09:00", "11:00", ("pending", "awaiting_recheck")),  # within the 3 h ttl
        ("09:15", "09:16", ("overdue", "no_completion_record")),
        ("09:20", "09:30", ("overdue", "no_completion_record")),
    ],
)
def test_rule6_deadline_is_d_plus_lag_at_0915_utc(db, checked, now, expected):
    def at(hhmm: str) -> dt.datetime:
        h, m = map(int, hhmm.split(":"))
        return dt.datetime(2026, 11, 21, h, m, tzinfo=UTC)

    record_check(
        db,
        payload(checked_at=at(checked), facts={("AFST", d(1)): ("absent", None, None)}),
    )
    assert grid(db, now=at(now))[("AFST", d(1))] == expected


def test_rule6_today_is_pending(db):
    record_check(db, payload())
    assert grid(db)[("AFST", TODAY)] == ("pending", "before_deadline")


def _completion(db, cid: str, at: dt.datetime, items=()):
    q(db, "INSERT INTO solution_completion VALUES (%s, %s, %s, %s, %s)",
      (cid, STREAM, at, W_FROM, W_TO))  # fmt: skip
    for sid, day, outcome in items:
        q(db, "INSERT INTO solution_completion_item VALUES (%s, %s, %s, %s)",
          (cid, sid, day, outcome))  # fmt: skip


@pytest.mark.parametrize(
    ("item", "reason"),
    [("rejected", "producer_rejected"), ("failed", "producer_failed")],
)
def test_rule7_producer_lists_the_station_day(db, item, reason):
    record_check(db, payload(facts={("AFST", d(5)): ("absent", None, None)}))
    _completion(db, "c1", NOW - dt.timedelta(hours=1), [("AFST", d(5), item)])
    assert grid(db)[("AFST", d(5))] == ("overdue", reason)


def test_rule8_absent_after_completion(db):
    record_check(db, payload(facts={("AFST", d(5)): ("absent", None, None)}))
    _completion(db, "c1", NOW - dt.timedelta(hours=1))
    assert grid(db)[("AFST", d(5))] == ("overdue", "absent_after_completion")


def test_rule8_ignores_completions_after_now(db):
    record_check(db, payload(facts={("AFST", d(5)): ("absent", None, None)}))
    _completion(db, "c1", NOW + dt.timedelta(hours=1))
    assert grid(db)[("AFST", d(5))] == ("overdue", "no_completion_record")


def test_rule9_no_completion_record(db):
    record_check(db, payload(facts={("AFST", d(5)): ("absent", None, None)}))
    assert grid(db)[("AFST", d(5))] == ("overdue", "no_completion_record")


def test_rule10_never_checked_and_never_verified(db):
    record_check(db, payload(
        w_from=d(46), w_to=d(1),
        facts={("AFST", d(46)): ("unverifiable", "undatable_row", None)},
    ))  # fmt: skip
    g = grid(db, now=NOW + dt.timedelta(days=1))  # d(46) is now outside the window
    assert g[("AFST", d(47))] == ("unknown", "never_checked")
    assert g[("AFST", d(46))] == ("unknown", "never_verified")


def test_an_absent_day_stays_overdue_after_leaving_the_window(db):
    record_check(db, payload(facts={("AFST", d(5)): ("absent", None, None)}))
    later = grid(db, now=NOW + dt.timedelta(days=60))
    assert later[("AFST", d(5))] == ("overdue", "no_completion_record")


def test_unverifiable_never_erases_the_last_known_fact(db):
    record_check(db, payload())
    record_check(db, payload(
        checked_at=NOW + dt.timedelta(hours=1),
        facts={("AFST", d(3)): ("unverifiable", "unreadable", None)},
    ))  # fmt: skip
    later = grid(db, now=NOW + dt.timedelta(days=60))
    assert later[("AFST", d(3))] == ("available", "frozen")


def test_every_cell_gets_exactly_one_state(db):
    record_check(db, payload())
    rows = q(db, "SELECT state, reason FROM solution_day_grid(%s, %s, %s, %s)",
             (STREAM, d(250), d(-2), NOW))  # fmt: skip
    assert len(rows) == 3 * 253
    assert {s for s, _ in rows} <= {
        "available", "pending", "overdue", "invalid", "not_expected", "unknown"
    }  # fmt: skip
    assert all(r for _, r in rows)


def test_grid_range_is_clamped_to_400_days(db):
    record_check(db, payload())
    rows = q(db, "SELECT DISTINCT day FROM solution_day_grid(%s, %s, %s, %s)",
             (STREAM, d(1000), d(0), NOW))  # fmt: skip
    assert len(rows) == 400


def test_grid_is_inlined_so_explain_sees_the_tables(db):
    plan = "\n".join(
        r[0] for r in q(db, "EXPLAIN SELECT * FROM solution_day_grid(%s, %s, %s, %s)",
                        (STREAM, d(45), d(1), NOW))  # fmt: skip
    )
    assert "solution_fact" in plan and "solution_check_run" in plan
    assert "Function Scan on solution_day_grid" not in plan


def test_grid_does_not_call_now():
    body = MIGRATION.read_text().split("CREATE OR REPLACE FUNCTION solution_day_grid")[
        1
    ]
    body = body.split("$fn$")[1]
    assert "now()" not in body.lower() and "current_" not in body.lower()


# ============================================================ the writer


def _counts(dsn: str) -> dict[str, int]:
    tables = ["solution_check_run", "solution_fact", "solution_transition",
              "solution_roster", "solution_roster_station", "solution_roster_interval"]  # fmt: skip
    return {t: q(dsn, f"SELECT count(*) FROM {t}")[0][0] for t in tables}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["cells"].pop(),                                  # incomplete
        lambda p: p["cells"].__setitem__(1, dict(p["cells"][0])),    # duplicate
        lambda p: p["cells"][0].update(fact="green"),                # bad fact
        lambda p: p["cells"][0].update(station="ZZZZ"),              # not in roster
        lambda p: p["cells"][0].update(day="1999-01-01"),            # outside window
        lambda p: p["cells"][0].update(fingerprint=None),            # valid w/o fp
        lambda p: p.update(schema="solution-check-facts/1"),
        lambda p: p.update(stream="imo.24hr_itrf99"),
        lambda p: p.update(outcome="maybe"),
        lambda p: p["roster"].update(version="nope"),
    ],
)  # fmt: skip
def test_any_defect_writes_nothing_at_all(db, mutate):
    p = payload()
    mutate(p)
    with pytest.raises(psycopg2.Error):
        record_check(db, p)
    assert set(_counts(db).values()) == {0}


def test_duplicate_cells_are_refused_by_name(db):
    # same cell count, one (station, day) twice: only the duplicate check sees it
    p = payload()
    p["cells"][1] = dict(p["cells"][0])
    with pytest.raises(psycopg2.Error, match=r"duplicate \(station, day\) cells"):
        record_check(db, p)


def test_a_failed_run_carries_no_cells(db):
    p = payload(outcome="failed")
    p["cells"] = payload()["cells"][:1]
    with pytest.raises(psycopg2.Error, match="no cells"):
        record_check(db, p)


def test_replay_is_idempotent_only_for_the_same_payload(db):
    p = payload()
    assert record_check(db, p) == "recorded:ok"
    assert record_check(db, p) == "duplicate"
    changed = dict(p, source="/elsewhere")
    with pytest.raises(psycopg2.Error, match="different payload"):
        record_check(db, changed)


def test_run_row_keeps_inventory_and_exception_lists(db):
    record_check(db, payload(run_id="00000000-0000-4000-8000-000000000001"))
    files, unexpected, others, n = q(
        db, "SELECT files, unexpected_files, other_products, cell_count"
            " FROM solution_check_run")[0]  # fmt: skip
    assert files[0]["sha256"] == FP1
    assert unexpected[0]["name"].startswith("ZZZZ")
    assert others == {"imo.24hr_itrf08": 1}
    assert n == 3 * 45


def test_transitions_record_presence_and_validity_changes_only(db):
    record_check(db, payload(facts={("AFST", d(3)): ("absent", None, None)}))
    record_check(db, payload(checked_at=NOW + dt.timedelta(hours=1)))  # absent -> valid
    record_check(
        db,
        payload(checked_at=NOW + dt.timedelta(hours=2), default=("valid", None, FP2)),
    )  # revision, not a transition
    record_check(db, payload(checked_at=NOW + dt.timedelta(hours=3),
                             default=("valid", None, FP2),
                             facts={("AFST", d(3)): ("absent", None, None)}))  # fmt: skip
    rows = q(db, "SELECT from_fact, to_fact FROM solution_transition"
                 " WHERE station = 'AFST' AND day = %s ORDER BY at", (d(3),))  # fmt: skip
    assert rows == [("absent", "valid"), ("valid", "absent")]
    rev = q(db, "SELECT revision_count, last_revised_at FROM solution_fact"
                " WHERE station = 'AFST' AND day = %s", (d(4),))  # fmt: skip
    assert rev[0][0] == 1 and rev[0][1] == NOW + dt.timedelta(hours=2)
    total = q(db, "SELECT count(*) FROM solution_transition")[0][0]
    assert total == 2  # the 3x45 fingerprint revisions made no transition rows


def test_first_seen_survives_and_absence_does_not_set_it(db):
    record_check(db, payload(facts={("AFST", d(3)): ("absent", None, None)}))
    assert q(db, "SELECT first_seen_at FROM solution_fact WHERE station='AFST'"
                 " AND day=%s", (d(3),))[0][0] is None  # fmt: skip
    record_check(db, payload(checked_at=NOW + dt.timedelta(hours=1)))
    record_check(db, payload(checked_at=NOW + dt.timedelta(hours=2)))
    first = q(db, "SELECT first_seen_at FROM solution_fact WHERE station='AFST'"
                  " AND day=%s", (d(3),))[0][0]  # fmt: skip
    assert first == NOW + dt.timedelta(hours=1)


def test_an_older_snapshot_never_moves_a_fact_back(db):
    record_check(db, payload(checked_at=NOW + dt.timedelta(hours=1)))
    # pgdev catching up: an OLDER run saying absent arrives later
    record_check(db, payload(facts={("AFST", d(3)): ("absent", None, None)}))
    assert grid(db, now=NOW + dt.timedelta(hours=1))[("AFST", d(3))][0] == "available"
    assert q(db, "SELECT count(*) FROM solution_transition")[0][0] == 0


def test_roster_is_replaced_only_by_a_newer_different_version(db):
    record_check(db, payload())
    smaller = roster(V2, stations=("AFST",))
    record_check(db, payload(checked_at=NOW + dt.timedelta(hours=1), ros=smaller))
    assert q(db, "SELECT version FROM solution_roster")[0][0] == V2
    assert {r[0] for r in q(db, "SELECT station FROM solution_roster_station")} == {
        "AFST"
    }
    # an older run with the old roster does not bring it back
    record_check(db, payload(checked_at=NOW - dt.timedelta(hours=1)))
    assert q(db, "SELECT version FROM solution_roster")[0][0] == V2


def test_writer_is_security_definer_and_not_public(db):
    row = q(db, "SELECT prosecdef, has_function_privilege('public',"
                " 'record_solution_check(jsonb)', 'EXECUTE') FROM pg_proc"
                " WHERE proname = 'record_solution_check'")[0]  # fmt: skip
    assert row == (True, False)


# ============================================================ destinations (G8)


@pytest.mark.parametrize(
    "dsn",
    [
        "host=rek-d01.vedur.is dbname=sol",
        "host=pgdev.vedur.is dbname=sol",
        "host=127.0.0.1,pgdev.vedur.is dbname=sol",
        "host=127.0.0.1 hostaddr=10.0.0.1 dbname=sol",
        "host=127.0.0.1 dbname=gps_health",
        "host=127.0.0.1",
        "postgresql://u:secret@pgdev.vedur.is/sol",
        "dbname=sol",  # no host: libpq would fall back to PGHOST
        "host=127.0.0.1 service=prod dbname=sol",
    ],
)
def test_slice2_refuses_non_disposable_destinations(dsn):
    with pytest.raises(DestinationRefusedError) as exc:
        check_destination(dsn)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize(
    "dsn",
    ["host=127.0.0.1 dbname=sol", "host=/var/run/postgresql dbname=sol"],
)
def test_local_destinations_are_accepted(dsn):
    check_destination(dsn)


@pytest.mark.parametrize(
    "dsn",
    [
        "host=/var/run/postgresql dbname=gps_health",  # the rek-d01 timer
        "host=localhost dbname=gps_health",
        "host=pgdev.vedur.is dbname=gps_health user=bgo",  # G8 second destination
    ],
)
def test_production_accepts_exactly_the_production_databases(dsn):
    check_destination(dsn, production=True)


@pytest.mark.parametrize(
    "dsn",
    [
        "host=rek-d01.vedur.is dbname=gps_health",  # remote, but not pgdev
        "host=pgdev.vedur.is dbname=sol",  # pgdev, wrong database
        "host=127.0.0.1 dbname=sol",  # a disposable DB is not production
        "host=127.0.0.1,pgdev.vedur.is dbname=gps_health",
        "host=pgdev.vedur.is hostaddr=10.0.0.1 dbname=gps_health",
        "host=pgdev.vedur.is service=prod dbname=gps_health",
        "dbname=gps_health",  # no host: PGHOST would decide
        "postgresql://u:secret@pgdev.example.org/gps_health",
    ],
)
def test_production_refuses_everything_else(dsn):
    with pytest.raises(DestinationRefusedError) as exc:
        check_destination(dsn, production=True)
    assert "secret" not in str(exc.value)


def test_production_still_refuses_libpq_environment_redirects(monkeypatch):
    monkeypatch.setenv("PGSERVICE", "prod")
    with pytest.raises(DestinationRefusedError, match="PGSERVICE"):
        check_destination("host=localhost dbname=gps_health", production=True)


def test_production_flag_reaches_every_write(db):
    # the disposable test DB is not a production destination, so a production
    # write to it must be refused: proves the flag is passed down, not dropped
    (res,) = record_to_destinations([db], payload(), production=True)
    assert not res.ok and "--production writes only" in (res.error or "")
    assert q(db, "SELECT count(*) FROM solution_check_run")[0][0] == 0


def test_a_dsn_without_host_is_refused_by_name():
    with pytest.raises(DestinationRefusedError, match="explicit local host"):
        check_destination("dbname=sol")


@pytest.mark.parametrize("var", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"])
def test_libpq_environment_redirects_are_refused(monkeypatch, var):
    monkeypatch.setenv(var, "10.0.0.1" if var == "PGHOSTADDR" else "prod")
    with pytest.raises(DestinationRefusedError, match=var):
        check_destination("host=127.0.0.1 dbname=sol")


def test_each_destination_is_written_separately_and_failures_reported(admin_dsn, db):
    broken = _create_db(admin_dsn, migrate=False)  # no 077: the call must fail
    try:
        results = record_to_destinations([db, broken], payload())
    finally:
        _drop_db(admin_dsn, broken)
    assert results[0].ok and results[0].status == "recorded:ok"
    assert not results[1].ok and "record_solution_check" in (results[1].error or "")
    assert q(db, "SELECT count(*) FROM solution_check_run")[0][0] == 1  # kept


# ============================================================ CLI wiring


def _cli(argv: list[str]) -> int:
    import argparse

    from receivers.cli.solution_health import create_solution_health_parser

    p = argparse.ArgumentParser()
    create_solution_health_parser(p.add_subparsers(dest="command"))
    args = p.parse_args(argv)
    return int(args.func(args))


def _source_and_roster(tmp_path: Path) -> tuple[Path, Path]:
    from receivers.monitoring.solutions.window import check_window
    from tests.solution_pos_helpers import write_pos

    src = tmp_path / "pos"
    src.mkdir()
    write_pos(src / "AFST.imo.24hr_itrf14.pos", check_window(TODAY)[:-1])
    ros = tmp_path / "daily_solutions.yaml"
    ros.write_text(
        "version: 1\nstream: imo.24hr_itrf14\nreviewed: {from: 2026-01-01}\n"
        "stations:\n  AFST: {expected: [{from: 2021-01-01}]}\n"
    )
    return src, ros


def test_cli_records_the_run_and_the_grid_reads_it(db, tmp_path, capsys):
    src, ros = _source_and_roster(tmp_path)
    rc = _cli(["solution-health", "check", "--source", str(src), "--roster", str(ros),
               "--now", NOW.isoformat(), "--record-dsn", db])  # fmt: skip
    assert rc == 0
    assert "recorded:ok" in capsys.readouterr().err
    g = grid(db, now=NOW + dt.timedelta(minutes=1))
    assert g[("AFST", d(2))] == ("available", "verified")
    assert g[("AFST", d(1))] == ("overdue", "no_completion_record")


def test_cli_records_a_source_unavailable_run(db, tmp_path):
    _, ros = _source_and_roster(tmp_path)
    rc = _cli(["solution-health", "check", "--source", str(tmp_path / "gone"),
               "--roster", str(ros), "--now", NOW.isoformat(), "--record-dsn", db])  # fmt: skip
    assert rc == 2
    assert q(db, "SELECT outcome FROM solution_check_run")[0][0] == "source_unavailable"


def test_cli_exit_3_when_a_destination_fails(admin_dsn, db, tmp_path, capsys):
    src, ros = _source_and_roster(tmp_path)
    broken = _create_db(admin_dsn, migrate=False)
    try:
        rc = _cli(["solution-health", "check", "--source", str(src), "--roster",
                   str(ros), "--now", NOW.isoformat(), "--json",
                   "--record-dsn", db, "--record-dsn", broken])  # fmt: skip
    finally:
        _drop_db(admin_dsn, broken)
    assert rc == 3
    err = capsys.readouterr().err
    assert "recorded" in err and "error: record" in err
    assert q(db, "SELECT count(*) FROM solution_check_run")[0][0] == 1


def test_cli_refuses_a_remote_destination_before_reading(tmp_path, capsys):
    src, ros = _source_and_roster(tmp_path)
    rc = _cli(["solution-health", "check", "--source", str(src), "--roster", str(ros),
               "--record-dsn", "host=pgdev.vedur.is dbname=gps_health"])  # fmt: skip
    assert rc == 2
    assert "local disposable" in capsys.readouterr().err


def test_cli_production_flag_reaches_the_destination_check(tmp_path, capsys):
    src, ros = _source_and_roster(tmp_path)
    rc = _cli(["solution-health", "check", "--source", str(src), "--roster", str(ros),
               "--production", "--record-dsn", "host=127.0.0.1 dbname=sol"])  # fmt: skip
    assert rc == 2
    assert "--production writes only" in capsys.readouterr().err


def test_payload_from_a_real_check_is_accepted_as_is(db, tmp_path):
    from receivers.monitoring.solutions.check import run_check
    from receivers.monitoring.solutions.roster import load_roster

    src, ros = _source_and_roster(tmp_path)
    report = run_check(src, load_roster(ros), NOW)
    doc = json.loads(json.dumps(report.to_json()))
    assert record_check(db, doc) == "recorded:ok"
