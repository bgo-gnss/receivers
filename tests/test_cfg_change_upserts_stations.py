"""Adding a station to ``stations.cfg`` must create its ``stations`` row.

The bug these pin, measured on rek-d01 2026-10-02: four of 200 rows had NULL
``latitude``/``longitude``/``height`` with the values sitting in
``stations.cfg`` the whole time — VFLS and VFLN from that day, **NPSK since
2026-08-09 and VOTT since 2026-06-17**. Three writers disagreed about what a
row is:

* ``db/seeder.py`` wrote all 17 columns but only ran on demand;
* ``health/db_writer.py`` auto-created a row from a 12-column subset that
  omits the three position columns;
* ``_sync_station_status_to_db`` was an ``UPDATE … WHERE sid = %s`` of four
  fields, so for a station with no row yet it matched nothing and did nothing.

It never looked like an outage because ``station_dashboard_data`` does
``COALESCE(s.latitude, m.latitude)`` and silently fell back to the receiver's
own live PVT fix, placing the station at a single-epoch position instead of its
configured one. **A test asserting "the station appears on the map" would
therefore have passed throughout** — which is why these assert the ROW, and
specifically that the position columns are populated.

The fake cursor models the one thing that makes the upsert correct and is easy
to get wrong: ``ON CONFLICT … DO UPDATE … WHERE`` returns NOTHING for a row
that already matches, so a no-op run must not be counted or fanned out.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from receivers.db.station_rows import (
    STATION_COLUMNS,
    STATION_UPSERT_SQL,
    station_row_from_cfg,
)

VFLS_CFG = {
    "receiver_type": "PolaRX5",
    "antenna_type": "SEPVC6150L",
    "rinex_marker_name": "VFLS",
    "rinex_agency": "Icelandic Meteorological Office",
    "station_name": "Vatnsfellsvirkjun Suður",
    "latitude": "64.195575",
    "longitude": "-19.029078",
    "height": "624.59",
    "router_ip": "10.6.1.211",
    "receiver_httpport": "80",
}


def _code_of(func) -> str:
    """Source of ``func`` with its docstring removed.

    These tests assert on what the code DOES, and several of the strings they
    look for (``single_host=True``, ``resolve_ip``) also appear in the prose
    explaining why. A substring check over raw ``inspect.getsource`` therefore
    matches the docstring and the assertion becomes unfalsifiable — which it
    was, on the first run, until the docstring was written.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    fn = tree.body[0]
    body = fn.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(node) for node in body)


# --- the mapping -----------------------------------------------------------


def test_the_row_carries_the_position_columns():
    """The regression in one assertion: coordinates must reach the row."""
    row = dict(zip(STATION_COLUMNS, station_row_from_cfg("VFLS", VFLS_CFG)))
    assert row["latitude"] == 64.195575
    assert row["longitude"] == -19.029078
    assert row["height"] == 624.59
    assert row["sid"] == "VFLS"
    assert row["station_name"] == "Vatnsfellsvirkjun Suður"


def test_no_dns_unless_asked():
    """`resolve_ip=False` must not hit the network — 200 blocking lookups on
    the scheduler's config-watcher thread is the reason this flag exists."""
    row = dict(zip(STATION_COLUMNS, station_row_from_cfg("VFLS", VFLS_CFG)))
    assert row["ip_address"] is None, (
        "ip_address must be left to db_writer, which has the probe result; "
        "resolving it here blocks the watcher thread once per station"
    )


def test_an_absent_coordinate_is_none_not_zero():
    """An empty cfg value must not become 0.0 — that is a real position."""
    row = dict(zip(STATION_COLUMNS, station_row_from_cfg("XXXX", {"latitude": ""})))
    assert row["latitude"] is None
    assert row["longitude"] is None


def test_explicit_active_normalises_to_null():
    """`station_status = active` in cfg must store NULL.

    Every consumer treats a non-NULL status as "not operational", so writing
    the literal string would take the station off the dashboards.
    """
    row = dict(
        zip(
            STATION_COLUMNS,
            station_row_from_cfg(
                "XXXX", {"station_status": "Active", "health_check": "active"}
            ),
        )
    )
    assert row["station_status"] is None
    assert row["health_check"] is None


def test_the_sid_is_not_stored_as_a_name():
    row = dict(
        zip(STATION_COLUMNS, station_row_from_cfg("VFLS", {"station_name": "VFLS"}))
    )
    assert row["station_name"] is None


def test_the_upsert_inserts_and_only_updates_a_differing_row():
    """Both halves of the fix, read off the SQL itself.

    The INSERT is what makes a cfg addition land; the `DO UPDATE … WHERE` is
    what stops a cfg edit from rewriting ~200 rows and fanning every one of
    them to the pgdev mirror.
    """
    sql = " ".join(STATION_UPSERT_SQL.split())
    assert "INSERT INTO stations" in sql
    assert "ON CONFLICT (sid) DO UPDATE" in sql
    assert "IS DISTINCT FROM" in sql, "an ungated upsert rewrites every row"
    for col in ("latitude", "longitude", "height"):
        assert f"{col} = COALESCE(EXCLUDED.{col}, stations.{col})" in sql, (
            f"{col} must be COALESCE — cfg fills it, an absent cfg key must "
            f"never erase a value already in the DB"
        )
    # The two cfg-authoritative fields must NOT be COALESCE: clearing one back
    # to active (NULL) in cfg has to clear it in the DB.
    assert "station_status = EXCLUDED.station_status" in sql
    assert "health_check = EXCLUDED.health_check" in sql


# --- the CALL SITE ---------------------------------------------------------
# The seam the user asked for is "edit stations.cfg -> the row exists", and that
# runs through the scheduler's mtime watcher. Testing station_row_from_cfg alone
# would pass with the watcher still wired to the old UPDATE-only statement.


class _FakeCursor:
    def __init__(self, existing: Dict[str, Dict[str, Any]]) -> None:
        self.existing = existing
        self.executed: List[tuple] = []
        self._result: Optional[tuple] = None
        self.rowcount = 0

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed.append((" ".join(sql.split()), params))
        if "INSERT INTO stations" in sql:
            sid = params[0]
            row = dict(zip(STATION_COLUMNS, params))
            if sid not in self.existing:
                self.existing[sid] = row
                self._result = (True,)  # xmax = 0 -> inserted
            else:
                merged = dict(self.existing[sid])
                for k, v in row.items():
                    if k in ("station_status", "health_check"):
                        merged[k] = v
                    elif v is not None:
                        merged[k] = v
                if merged == self.existing[sid]:
                    # DO UPDATE ... WHERE skipped -> the statement returns NOTHING
                    self._result = None
                else:
                    self.existing[sid] = merged
                    self._result = (False,)
            self.rowcount = 0 if self._result is None else 1
        else:
            self._result = None
            self.rowcount = 0

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cur: _FakeCursor) -> None:
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _scheduler_with(cfg_rows, station_ids, cur, monkeypatch):
    """A BulkDownloadScheduler shell — no APScheduler, no DB, no cfg file."""
    from receivers.health import database_factory
    from receivers.scheduling.bulk_scheduler import BulkDownloadScheduler

    sched = BulkDownloadScheduler.__new__(BulkDownloadScheduler)
    import logging

    sched.logger = logging.getLogger("test.sync")
    sched.stations = {sid: {"station_id": sid} for sid in station_ids}
    sched._cfg_station_rows = lambda: cfg_rows  # type: ignore[method-assign]
    monkeypatch.setattr(
        database_factory.DatabaseConnectionFactory,
        "connection",
        staticmethod(lambda *a, **k: _FakeConn(cur)),
    )
    return sched


def test_a_station_added_to_cfg_gets_a_full_row(monkeypatch, caplog):
    """The user's ask: a cfg section the DB has never seen becomes a row."""
    cur = _FakeCursor(existing={})
    sched = _scheduler_with({"VFLS": VFLS_CFG}, ["VFLS"], cur, monkeypatch)

    with caplog.at_level("INFO"):
        sched._sync_station_status_to_db()

    assert "VFLS" in cur.existing, (
        "the watcher must INSERT — an UPDATE ... WHERE sid matches nothing for "
        "a station that has no row yet, which is exactly the new-station case"
    )
    row = cur.existing["VFLS"]
    assert row["latitude"] == 64.195575
    assert row["longitude"] == -19.029078
    assert row["height"] == 624.59
    assert "New station VFLS added to the database" in caplog.text


def test_a_second_sync_writes_nothing(monkeypatch):
    """An unchanged row must not be written, so it is not fanned out to pgdev."""
    cur = _FakeCursor(existing={})
    sched = _scheduler_with({"VFLS": VFLS_CFG}, ["VFLS"], cur, monkeypatch)
    sched._sync_station_status_to_db()
    before = dict(cur.existing["VFLS"])

    sched._sync_station_status_to_db()

    assert cur.existing["VFLS"] == before
    upserts = [e for e in cur.executed if "INSERT INTO stations" in e[0]]
    assert len(upserts) == 2, "it still issues the statement"
    # ...but the second one changed nothing, which is what the WHERE buys.


def test_an_existing_coordinate_is_never_moved(monkeypatch):
    """A surveyed position in the DB must survive a cfg without coordinates."""
    cur = _FakeCursor(
        existing={
            "VOTT": dict(
                zip(
                    STATION_COLUMNS,
                    station_row_from_cfg(
                        "VOTT", {"latitude": "64.2715", "longitude": "-17.18"}
                    ),
                )
            )
        }
    )
    sched = _scheduler_with(
        {"VOTT": {"receiver_type": "NetRS"}}, ["VOTT"], cur, monkeypatch
    )

    sched._sync_station_status_to_db()

    assert cur.existing["VOTT"]["latitude"] == 64.2715
    assert cur.existing["VOTT"]["longitude"] == -17.18


def test_a_station_missing_from_cfg_is_not_invented(monkeypatch):
    """No cfg section -> no upsert.

    `self.stations` can outlive a cfg read failure, and building a row from the
    scheduler's partial view would write NULL over real values through the two
    authoritative status fields.
    """
    cur = _FakeCursor(existing={})
    sched = _scheduler_with({}, ["GHST"], cur, monkeypatch)

    sched._sync_station_status_to_db()

    assert cur.existing == {}
    assert not [e for e in cur.executed if "INSERT INTO stations" in e[0]]


def test_configured_identity_still_syncs(monkeypatch):
    """The receivers-only columns keep their narrow UPDATE, after the upsert."""
    cur = _FakeCursor(existing={})
    sched = _scheduler_with({"VFLS": VFLS_CFG}, ["VFLS"], cur, monkeypatch)
    sched.stations["VFLS"].update(
        {"configured_serial": "4103681", "configured_firmware": "5.7.0"}
    )

    sched._sync_station_status_to_db()

    ident = [
        e for e in cur.executed if "configured_serial" in e[0] and "UPDATE" in e[0]
    ]
    assert ident, "configured_serial/firmware must still be written"
    upsert_at = next(
        i for i, e in enumerate(cur.executed) if "INSERT INTO stations" in e[0]
    )
    ident_at = next(
        i
        for i, e in enumerate(cur.executed)
        if "configured_serial" in e[0] and "UPDATE" in e[0]
    )
    assert (
        upsert_at < ident_at
    ), "the identity UPDATE is only safe once the row is guaranteed to exist"


def test_the_seeder_and_the_sync_share_one_mapping():
    """Guards the orphaned-abstraction pattern: both writers must call it."""
    from receivers.db import seeder
    from receivers.scheduling import bulk_scheduler

    # _code_of, not getsource: both docstrings name these symbols.
    seed = _code_of(seeder.Seeder.seed_stations)
    sync = _code_of(bulk_scheduler.BulkDownloadScheduler._sync_station_status_to_db)

    assert "station_row_from_cfg" in seed
    assert "STATION_UPSERT_SQL" in seed
    assert "resolve_ip=True" in seed, "the CLI seed resolves router_ip"
    assert "station_row_from_cfg" in sync
    assert "STATION_UPSERT_SQL" in sync
    assert "resolve_ip=False" in sync, "the scheduler must not do DNS per station"


def test_the_sync_uses_the_mirrored_connection():
    """pgdev is what production Grafana reads — a new station has to reach it.

    The seeder is deliberately `single_host=True` (a mirrored seed/DDL is a
    silent cross-host mutation); this path must NOT be.
    """
    import inspect

    from receivers.scheduling import bulk_scheduler

    sync = _code_of(bulk_scheduler.BulkDownloadScheduler._sync_station_status_to_db)
    assert "DatabaseConnectionFactory.connection()" in sync
    # Asserted against the CODE, docstring excluded — the docstring explains at
    # length why the seeder is single-host, so a plain substring check on the
    # source matches its own prose and can never fail.
    assert "single_host=True" not in sync, (
        "pinning this to one host would stop a new station reaching pgdev, "
        "which is the database production Grafana reads"
    )

    from receivers.db.seeder import Seeder

    assert "single_host=True" in _code_of(Seeder._get_conn), (
        "the seeder must STAY single-host — a mirrored seed/DDL is a silent "
        "cross-host mutation"
    )
