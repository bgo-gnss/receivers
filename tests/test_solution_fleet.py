"""Files of stations outside the fleet are recorded but not counted."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pytest

from receivers.monitoring.solutions.check import IGNORED_REASON, run_check
from receivers.monitoring.solutions.fleet import FleetError, load_fleet
from receivers.monitoring.solutions.roster import load_roster
from receivers.monitoring.solutions.window import check_window
from tests.solution_pos_helpers import write_pos

NOW = dt.datetime(2026, 11, 21, 10, 0, tzinfo=dt.UTC)

STATIONS_CFG = """\
[AFST]
station_owner = IMO
receiver_pwd = hunter2
[OURS]
health_check = passive
station_role = passive
[GONE]
station_status = discontinued
[IDLE]
station_status = inactive
[BRUX]
is_reference_site = true
station_role = passive
[UP]
station_status = active
is_reference_site = false
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / "stations.cfg"
    p.write_text(STATIONS_CFG)
    return p


def test_fleet_is_active_non_reference_stations(cfg):
    # a passive station of ours stays in: station_role is NOT the test
    assert load_fleet(cfg) == {"AFST", "OURS", "UP"}


def test_fleet_errors_never_echo_values(tmp_path):
    with pytest.raises(FleetError) as exc:
        load_fleet(tmp_path / "missing.cfg")
    assert "hunter2" not in str(exc.value)
    empty = tmp_path / "empty.cfg"
    empty.write_text("[GONE]\nstation_status = inactive\n")
    with pytest.raises(FleetError, match="no active stations"):
        load_fleet(empty)


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "pos"
    src.mkdir()
    days = check_window(NOW.date())
    for sid in ("AFST", "OURS", "BRUX", "GONE", "XXXX"):
        write_pos(src / f"{sid}.imo.24hr_itrf14.pos", days, station=sid)
    ros = tmp_path / "daily_solutions.yaml"
    ros.write_text(
        "version: 1\nstream: imo.24hr_itrf14\nreviewed: {from: 2026-01-01}\n"
        "stations:\n  AFST: {expected: [{from: 2021-01-01}]}\n"
    )
    return src, ros


def test_only_fleet_stations_count_as_unexpected(tmp_path, cfg):
    src, ros = _setup(tmp_path)
    report = run_check(src, load_roster(ros), NOW, fleet=load_fleet(cfg))
    reasons = {u.name[:4]: u.reason for u in report.unexpected_files}
    # everything is still RECORDED, with its reason
    assert reasons == {
        "OURS": "station_not_in_roster",  # ours, missing from the roster
        "BRUX": IGNORED_REASON,  # reference site
        "GONE": IGNORED_REASON,  # discontinued
        "XXXX": IGNORED_REASON,  # not in stations.cfg at all
    }
    summary = report.to_json()["summary"]
    assert (summary["unexpected_files"], summary["ignored_files"]) == (1, 3)


def test_without_a_fleet_everything_counts(tmp_path):
    src, ros = _setup(tmp_path)
    report = run_check(src, load_roster(ros), NOW)
    assert {u.reason for u in report.unexpected_files} == {"station_not_in_roster"}
    assert report.to_json()["summary"]["ignored_files"] == 0


def _cli(argv: list[str]) -> int:
    from receivers.cli.solution_health import create_solution_health_parser

    p = argparse.ArgumentParser()
    create_solution_health_parser(p.add_subparsers(dest="command"))
    args = p.parse_args(argv)
    return int(args.func(args))


def _summary(capsys) -> dict:
    return json.loads(capsys.readouterr().out)["summary"]


def test_cli_reads_the_fleet_from_gps_config_path(tmp_path, cfg, capsys, monkeypatch):
    monkeypatch.setenv("GPS_CONFIG_PATH", str(cfg.parent))
    src, ros = _setup(tmp_path)
    argv = ["solution-health", "check", "--source", str(src), "--roster", str(ros)]
    _cli([*argv, "--now", NOW.isoformat(), "--json"])
    assert _summary(capsys)["unexpected_files"] == 1
    # --no-ignore: the option to see them all again
    _cli([*argv, "--now", NOW.isoformat(), "--json", "--no-ignore"])
    summary = _summary(capsys)
    assert (summary["unexpected_files"], summary["ignored_files"]) == (4, 0)


def test_cli_without_stations_cfg_ignores_nothing_and_says_so(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("GPS_CONFIG_PATH", str(tmp_path / "nowhere"))
    src, ros = _setup(tmp_path)
    rc = _cli(["solution-health", "check", "--source", str(src), "--roster",
               str(ros), "--now", NOW.isoformat(), "--json"])  # fmt: skip
    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["summary"]["unexpected_files"] == 4
    assert "no files are ignored" in captured.err


def test_cli_explicit_missing_stations_cfg_is_an_error(tmp_path, capsys):
    src, ros = _setup(tmp_path)
    rc = _cli(["solution-health", "check", "--source", str(src), "--roster",
               str(ros), "--stations-cfg", str(tmp_path / "typo.cfg")])  # fmt: skip
    assert rc == 2
    assert "--stations-cfg" in capsys.readouterr().err
