"""Every link between the GPS Grafana boards must lead somewhere.

A board link is ``/d/<uid>/...``. Grafana renders a missing uid as "Dashboard
not found", so these checks run on the JSON in ``docs/grafana`` and on the
compose files that provision those boards (rek-d01's Grafana and the laptop's).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GRAFANA = ROOT / "docs" / "grafana"
COMPOSE = {
    "rek-d01": ROOT / "deployment" / "server" / "docker-compose.yml",
    "laptop": ROOT / "deployment" / "docker-dev" / "docker-compose.yml",
}
LINK = re.compile(r"/d/([A-Za-z0-9_-]+)")


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _boards() -> dict[str, dict]:
    """file name -> dashboard JSON, for every board in docs/grafana."""
    return {
        p.name: json.loads(p.read_text())
        for p in sorted(GRAFANA.glob("gps_*_dashboard.json"))
    }


def _targets(board: dict) -> set[str]:
    return {m for s in _strings(board) for m in LINK.findall(s)}


def _provisioned(compose: Path) -> set[str]:
    services = yaml.safe_load(compose.read_text())["services"]
    names = set()
    for svc in services.values():
        for vol in svc.get("volumes", []):
            src = str(vol).split(":", 1)[0]
            if "docs/grafana/" in src and src.endswith("_dashboard.json"):
                names.add(Path(src).name)
    return names


BOARDS = _boards()
UID_TO_FILE = {b["uid"]: name for name, b in BOARDS.items()}


def test_uids_are_unique():
    assert len(UID_TO_FILE) == len(BOARDS)


@pytest.mark.parametrize("name", sorted(BOARDS))
def test_every_link_targets_a_board_in_the_repo(name):
    missing = _targets(BOARDS[name]) - set(UID_TO_FILE)
    assert not missing, f"{name} links to unknown boards {sorted(missing)}"


@pytest.mark.parametrize("host", sorted(COMPOSE))
def test_a_provisioned_board_only_links_to_provisioned_boards(host):
    provisioned = _provisioned(COMPOSE[host])
    assert provisioned, f"no boards found in {COMPOSE[host]}"
    for name in sorted(provisioned):
        for uid in _targets(BOARDS[name]):
            assert (
                UID_TO_FILE[uid] in provisioned
            ), f"{host}: {name} links to {UID_TO_FILE[uid]}, which is not provisioned"


def test_daily_solutions_is_reachable_from_the_station_boards():
    for name in (
        "gps_health_dashboard.json",
        "gps_map_dashboard.json",
        "gps_data_delivery_dashboard.json",
        "gps_station_detail_dashboard.json",
    ):
        assert "gps-daily-solutions" in {
            m for link in BOARDS[name]["links"] for m in LINK.findall(link["url"])
        }, name


def test_links_into_and_out_of_daily_solutions_do_not_carry_the_time_range():
    # 45 days on the block_* boards is a heavy query; their 7 d / 24 h on the
    # solutions board is a grid of one day. Each board keeps its own range.
    out = BOARDS["gps_daily_solutions_dashboard.json"]["links"]
    into = [
        link
        for b in BOARDS.values()
        for link in b.get("links", [])
        if "/d/gps-daily-solutions" in link["url"]
    ]
    for link in out + into:
        assert link["keepTime"] is False, link["title"]
        assert link["includeVars"] is False, link["title"]


def test_area_variable_matches_the_other_boards():
    # var-area=<value> links only work when every board offers the same values
    def area(board: dict) -> dict:
        return next(v for v in board["templating"]["list"] if v["name"] == "area")

    mine = area(BOARDS["gps_daily_solutions_dashboard.json"])
    theirs = area(BOARDS["gps_health_dashboard.json"])
    assert mine["query"] == theirs["query"]
    assert mine["includeAll"] is False


def test_sync_drops_top_bar_links_to_boards_the_target_lacks():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "grafana_sync", ROOT / "scripts" / "grafana_sync.py"
    )
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    targets = yaml.safe_load((ROOT / "scripts" / "grafana_targets.yaml").read_text())
    vedur = targets["targets"]["vedur"]["dashboards"]
    board = json.loads((GRAFANA / "gps_data_delivery_dashboard.json").read_text())
    dropped = sync.drop_links_to_unsynced(board, vedur)
    kept = {m for link in board["links"] for m in LINK.findall(link["url"])}
    # nothing that vedur does not carry survives; everything it carries does
    assert kept <= {
        b["uid"] for n, b in BOARDS.items() if n.removesuffix(".json") in vedur
    }
    assert "gps-health-voltage" in kept
    # vedur carries Daily Solutions since 2026-11-23; LTB Runs stays local
    assert "gps-daily-solutions" in kept
    assert "Daily Solutions" not in dropped and "LTB Runs" in dropped
