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
    sql: str, stations: str, frm: dt.datetime, to: dt.datetime, show: str = "problems"
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
                .replace("${area}", "all"),
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
    link = next(x["value"] for x in plot["properties"] if x["id"] == "links")[0]
    assert link["url"].startswith(
        "http://brunnur.vedur.is/gps/timeseries/${__value.raw}"
    )


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
    rows = q(db, _fill(sql, "'AFST','NEWS','THOB'", frm, NOW))
    afst = next(r for r in rows if r[0] == "AFST")
    # AFST: one absent day, d(3), since recovered -> OK now, 1 problem day
    assert afst[3] == 0 and afst[9] == 1  # Total is the last column
    # one short Area label: the volcano area first, "+N" for the rest
    assert afst[2] == "Krýsuvík +1"
    sql = _panel(dash, "Map: current status")["targets"][0]["rawSql"]
    assert [r[3] for r in q(db, _fill(sql, "'AFST'", frm, NOW))] == ["AFST"]
