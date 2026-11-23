"""Slice 3: the GPS Daily Solutions Grafana dashboard.

Static checks always run. With ``RECEIVERS_TEST_PG_DSN`` set (see
``test_solution_store.py``) every panel's SQL is also executed, with the
Grafana macros filled in, against a freshly migrated disposable database.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest

# reuse the disposable-database fixtures
from tests.test_solution_store import admin_dsn, db  # noqa: F401

DASHBOARD = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "grafana"
    / "gps_daily_solutions_dashboard.json"
)


@pytest.fixture(scope="module")
def dash() -> dict:
    return json.loads(DASHBOARD.read_text())


def _sqls(dash: dict) -> list[tuple[str, str]]:
    return [
        (p["title"], t["rawSql"])
        for p in dash["panels"]
        for t in p.get("targets", [])
        if "rawSql" in t
    ]


def _grid_panel(dash: dict) -> dict:
    return next(p for p in dash["panels"] if p["title"] == "Station × UTC day")


def test_states_come_only_from_solution_day_grid(dash):
    # No panel may re-derive a state from the fact tables (plan §3, S3).
    for title, sql in _sqls(dash):
        for table_or_column in ("solution_fact", "deadline_time", "expected_lag_days"):
            assert table_or_column not in sql, title


def test_matrix_columns_are_keyed_by_iso_date_and_shown_uniquely(dash):
    p = _grid_panel(dash)
    assert "to_char(day, 'YYYY-MM-DD') AS day" in p["targets"][0]["rawSql"]
    matrix, rename = p["transformations"]
    assert matrix["id"] == "groupingToMatrix"
    assert matrix["options"]["columnField"] == "day"
    assert rename["id"] == "renameByRegex"
    # the shown header must stay unique across months (a bare DD merged
    # October and November columns in Grafana 12.3.1)
    rx = re.compile(rename["options"]["regex"])
    pattern = (
        rename["options"]["renamePattern"].replace("$1", r"\1").replace("$2", r"\2")
    )
    days = [dt.date(2026, 10, 1) + dt.timedelta(days=k) for k in range(400)]
    shown = [rx.sub(pattern, d.isoformat()) for d in days[:365]]
    assert len(set(shown)) == len(shown)


def test_every_state_has_a_colour_and_unknown_frozen_are_not_green(dash):
    p = _grid_panel(dash)
    opts = p["fieldConfig"]["defaults"]["mappings"][0]["options"]
    assert set(opts) == {str(k) for k in range(1, 11)}
    green = opts["1"]["color"]
    for code in ("2", "10"):  # frozen, unknown
        assert opts[code]["color"] != green


def test_station_column_is_frozen_and_links_to_context(dash):
    p = _grid_panel(dash)
    assert p["options"]["frozenColumns"] == {"left": 1}
    props = p["fieldConfig"]["overrides"][0]["properties"]
    links = next(x["value"] for x in props if x["id"] == "links")
    assert any("/d/gps-data-delivery/" in link["url"] for link in links)


# ------------------------------------------------- panel SQL on a real schema


def _fill(
    sql: str,
    stations: str,
    frm: dt.datetime,
    to: dt.datetime,
    show: str = "problems",
    scope: str = "local",
) -> str:
    # the panels pass now() to the grid; pin it to the window end, or the
    # results drift with the wall clock (this broke the day after it was written)
    return (
        sql.replace("now()", f"'{to.isoformat()}'::timestamptz")
        .replace("${stream}", "imo.24hr_itrf14")
        .replace("$__timeFrom()", f"'{frm.isoformat()}'::timestamptz")
        .replace("$__timeTo()", f"'{to.isoformat()}'::timestamptz")
        .replace("${station:sqlstring}", stations)
        .replace("${show}", show)
        .replace("${scope}", scope)
        .replace("${owner}", "all")
        .replace("${operator}", "all")
    )


def test_every_panel_query_runs_against_migration_077(dash, db):  # noqa: F811 (fixture)
    from receivers.monitoring.solutions.store import record_check
    from tests.test_solution_store import NOW, payload, q

    record_check(db, payload())
    # the list and the map join the fleet tables gps_health already has
    # (column names/types as on pgdev); the disposable DB needs stand-ins
    q(db, "CREATE TABLE stations (sid varchar PRIMARY KEY, station_name varchar,"
          " latitude double precision, longitude double precision)")  # fmt: skip
    q(db, "CREATE TABLE station_areas (area_id varchar PRIMARY KEY, area_name varchar,"
          " area_type varchar)")  # fmt: skip
    q(db, "CREATE TABLE station_area_members (area_id varchar, sid varchar)")
    q(db, "INSERT INTO stations VALUES ('AFST', 'Afstapahraun', 63.98, -22.14)")
    q(db, "INSERT INTO station_areas VALUES ('reykjanes', 'Reykjanes', 'regional'),"
          " ('krysuvik', 'Krýsuvík', 'volcanic')")  # fmt: skip
    q(db, "INSERT INTO station_area_members VALUES"
          " ('reykjanes', 'AFST'), ('krysuvik', 'AFST')")  # fmt: skip
    stations = "'AFST','NEWS','THOB'"
    for v in dash["templating"]["list"]:
        if v["type"] == "query":
            q(
                db,
                v["query"]
                .replace("${stream}", "imo.24hr_itrf14")
                .replace("${area}", "all")
                .replace("${scope}", "local")
                .replace("${owner}", "all"),
            )
    for title, sql in _sqls(dash):
        rows = q(db, _fill(sql, stations, NOW - dt.timedelta(days=45), NOW))
        assert rows is not None, title
    grid_sql = _grid_panel(dash)["targets"][0]["rawSql"]
    frm = NOW - dt.timedelta(days=45)
    grid = q(db, _fill(grid_sql, stations, frm, NOW, show="all"))
    assert len(grid) == 3 * 46


def _panel(dash: dict, title: str) -> dict:
    return next(p for p in dash["panels"] if p["title"] == title)


def test_station_list_has_since_days_and_a_plot_link(dash):
    p = _panel(dash, "Stations with missing solutions")
    sql = p["targets"][0]["rawSql"]
    cols = ("SID", "Status", "Since", "Days", "Last good", "Total", "Plot")
    for col in (f'"{c}"' for c in cols):
        assert col in sql
    plot = next(
        o for o in p["fieldConfig"]["overrides"] if o["matcher"]["options"] == "Plot"
    )
    links = next(x["value"] for x in plot["properties"] if x["id"] == "links")
    # all three plate-fixed plots brunnur makes, each named in the menu
    assert [lk["url"].rsplit("-", 1)[1] for lk in links] == [
        "90d.png",
        "year.png",
        "full.png",
    ]
    assert len({lk["title"] for lk in links}) == 3
    link = links[0]
    assert link["url"].startswith("${plot_base}/${__value.raw}-plate-")
    base = next(v for v in dash["templating"]["list"] if v["name"] == "plot_base")
    # yang's gpsplot tree, not brunnur (which gpsplot no longer publishes to)
    assert (
        base["query"] == "http://yang-d01.vedur.is/gps/timeseries/graphs/standard_24h"
    )
    assert base["type"] == "constant" and base["hide"] == 2


def test_map_value_mappings_are_scoped_to_the_status_field(dash):
    # on the defaults they also turned "Days = 1" into "Unknown" in the tooltip
    p = _panel(dash, "Map: current status")
    assert "mappings" not in p["fieldConfig"]["defaults"]
    status = next(o for o in p["fieldConfig"]["overrides"]
                  if o["matcher"]["options"] == "status")  # fmt: skip
    assert any(x["id"] == "mappings" for x in status["properties"])


def test_list_and_map_rows_come_back(dash, db):  # noqa: F811 (fixture)
    from receivers.monitoring.solutions.store import record_check
    from tests.test_solution_store import NOW, d, payload, q

    record_check(db, payload(facts={("AFST", d(3)): ("absent", None, None)}))
    q(db, "CREATE TABLE stations (sid varchar PRIMARY KEY, station_name varchar,"
          " latitude double precision, longitude double precision)")  # fmt: skip
    q(db, "CREATE TABLE station_areas (area_id varchar PRIMARY KEY, area_name varchar,"
          " area_type varchar)")  # fmt: skip
    q(db, "CREATE TABLE station_area_members (area_id varchar, sid varchar)")
    q(db, "INSERT INTO stations VALUES ('AFST', 'Afstapahraun', 63.98, -22.14)")
    q(db, "INSERT INTO station_areas VALUES ('reykjanes', 'Reykjanes', 'regional'),"
          " ('krysuvik', 'Krýsuvík', 'volcanic')")  # fmt: skip
    q(db, "INSERT INTO station_area_members VALUES"
          " ('reykjanes', 'AFST'), ('krysuvik', 'AFST')")  # fmt: skip
    frm = NOW - dt.timedelta(days=45)
    sql = _panel(dash, "Stations with missing solutions")["targets"][0]["rawSql"]
    rows = q(db, _fill(sql, "'AFST','NEWS','THOB'", frm, NOW, show="all"))
    afst = next(r for r in rows if r[0] == "AFST")
    # AFST: one absent day, d(3), since recovered -> OK now, 1 problem day
    assert afst[4] == 0 and afst[10] == 1  # Total is the last column
    # one short Area label: the volcano area first, "+N" for the rest
    assert afst[2] == "Krýsuvík +1"
    # a plot link only for our own stations (yang has none for the others)
    assert afst[9] == "AFST"
    assert all(r[9] is None for r in rows if r[0] != "AFST")
    assert any(r[0] != "AFST" for r in rows)
    sql = _panel(dash, "Map: current status")["targets"][0]["rawSql"]
    assert [r[3] for r in q(db, _fill(sql, "'AFST'", frm, NOW))] == ["AFST"]
    # the station counts on top are the list's statuses, one per station
    counts = {
        t: q(
            db,
            _fill(
                _panel(dash, t)["targets"][0]["rawSql"],
                "'AFST','NEWS','THOB'",
                frm,
                NOW,
            ),
        )[0][
            0
        ]  # fmt: skip
        for t in ("Missing", "Late", "OK")
    }
    status = [r[4] for r in rows]
    assert counts == {
        "Missing": status.count(4),
        "Late": sum(s in (1, 2, 3) for s in status),
        "OK": status.count(0),
    }
    assert sum(counts.values()) == len(rows)


def test_files_outside_the_fleet_are_recorded_but_not_counted(dash, db):  # noqa: F811
    from receivers.monitoring.solutions.store import record_check
    from tests.test_solution_store import NOW, payload, q

    doc = payload()
    doc["unexpected_files"] = [
        {"name": "BJAC.imo.24hr_itrf14.pos", "reason": "station_not_in_roster"},
        {"name": "BRUX.imo.24hr_itrf14.pos", "reason": "not_local"},
        # runs recorded before migration 078 used the old name
        {"name": "TSKB.imo.24hr_itrf14.pos", "reason": "not_in_fleet"},
    ]
    record_check(db, doc)
    frm = NOW - dt.timedelta(days=45)
    # no stat for it any more: an admin signal, it lives in the table only
    assert not any(p["title"] == "Unexpected files" for p in dash["panels"])
    table = next(p for p in dash["panels"] if p["id"] == 11)["targets"][0]["rawSql"]

    def files(scope: str) -> set[str]:
        rows = q(db, _fill(table, "'AFST'", frm, NOW, scope=scope))
        return {r[0][:4] for r in rows if r[0].endswith(".pos")}

    # one option for the list and the files: local hides them, all shows them
    assert files("local") == {"BJAC"}
    assert files("all") == {"BJAC", "BRUX", "TSKB"}
    names = [v["name"] for v in dash["templating"]["list"]]
    assert "ignored" not in names


def test_station_scope_and_agency_come_from_solution_station(dash, db):  # noqa: F811
    # the roster carries reference stations too (their solutions arrive);
    # "local" (in Iceland) and the agency are recorded per station by the
    # check (078). Not the stations table: HOFN/REYK/BJAC are not in it.
    from receivers.monitoring.solutions.store import record_check
    from tests.test_solution_store import payload, q

    doc = payload()
    doc["station_info"] = [
        {"station": "AFST", "local": True, "agency": "IMO", "operator": "IMO"},
        {"station": "NEWS", "local": True, "agency": "KAUST", "operator": "IMO"},
        {"station": "THOB", "local": False, "agency": "IGS"},
    ]
    record_check(db, doc)
    q(db, "CREATE TABLE station_area_members (area_id varchar, sid varchar)")
    tvars = dash["templating"]["list"]
    var = next(v for v in tvars if v["name"] == "station")
    names = [v["name"] for v in tvars]
    # station refreshes on both
    assert names.index("scope") < names.index("station")
    assert names.index("owner") < names.index("station")
    assert names.index("operator") < names.index("station")

    def listed(scope: str, owner: str = "all", operator: str = "all") -> list[str]:
        sql = (var["query"].replace("${stream}", "imo.24hr_itrf14")
               .replace("${area}", "all").replace("${scope}", scope)
               .replace("${owner}", owner)
               .replace("${operator}", operator))  # fmt: skip
        return [r[0] for r in q(db, sql)]

    assert listed("local") == ["AFST", "NEWS"]
    assert listed("all") == ["AFST", "NEWS", "THOB"]
    assert listed("all", "IGS") == ["THOB"]
    assert listed("local", "IMO") == ["AFST"]
    scope = next(v for v in tvars if v["name"] == "scope")
    assert scope["current"]["value"] == "local"
    owner = next(v for v in tvars if v["name"] == "owner")
    assert owner["label"] == "Agency" and owner["current"]["value"] == "all"
    assert [r[0] for r in q(db, owner["query"])] == ["all", "IGS", "IMO", "KAUST"]
    # Operator: who runs it, apart from the owner (080). IMO runs NEWS for KAUST
    assert listed("all", operator="IMO") == ["AFST", "NEWS"]
    assert listed("all", "KAUST", "IMO") == ["NEWS"]
    op = next(v for v in tvars if v["name"] == "operator")
    assert op["label"] == "Operator" and op["current"]["value"] == "all"
    assert [r[0] for r in q(db, op["query"])] == ["all", "IMO"]
