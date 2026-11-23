"""Local = in Iceland (stations.cfg); files of other stations are not counted."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pytest

from receivers.monitoring.solutions.check import IGNORED_REASON, run_check
from receivers.monitoring.solutions.fleet import (
    FleetError,
    StationInfo,
    load_fleet,
    load_stations,
)
from receivers.monitoring.solutions.roster import load_roster
from receivers.monitoring.solutions.window import check_window
from tests.solution_pos_helpers import write_pos

NOW = dt.datetime(2026, 11, 21, 10, 0, tzinfo=dt.UTC)

STATIONS_CFG = """\
[AFST]
station_owner = IMO
receiver_pwd = hunter2
latitude = 63.98
longitude = -22.14
[OURS]
# in Iceland, run by someone else, no owner in the cfg (the BJAC case)
station_role = passive
latitude = 65.605
longitude = -16.825
[HOFN]
# an IGS reference site in Iceland is still local
is_reference_site = true
station_owner = LMI
latitude = 64.267
longitude = 344.80
[GONE]
station_status = discontinued
latitude = 63.9
longitude = -22.2
[IDLE]
station_status = inactive
latitude = 64.0
longitude = -21.0
[BRUX]
is_reference_site = true
station_role = passive
latitude = 50.80
longitude = 4.36
[UP]
station_status = active
is_in_iceland = true
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / "stations.cfg"
    p.write_text(STATIONS_CFG)
    return p


def test_local_is_in_iceland_and_not_inactive(cfg):
    # a place, not an owner: HOFN (IGS, 0-360 longitude) and OURS (not run
    # by IMO) are local; UP has no coordinates and falls back to is_in_iceland
    assert load_fleet(cfg) == {"AFST", "OURS", "HOFN", "UP"}
    info = load_stations(cfg)
    assert info["AFST"] == StationInfo(True, "IMO")
    assert info["OURS"] == StationInfo(True, None)
    assert info["BRUX"] == StationInfo(False, None)


def test_fleet_errors_never_echo_values(tmp_path):
    with pytest.raises(FleetError) as exc:
        load_fleet(tmp_path / "missing.cfg")
    assert "hunter2" not in str(exc.value)
    empty = tmp_path / "empty.cfg"
    empty.write_text("[GONE]\nstation_status = inactive\n")
    with pytest.raises(FleetError, match="no active stations in Iceland"):
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


def test_only_local_stations_count_as_unexpected(tmp_path, cfg):
    src, ros = _setup(tmp_path)
    report = run_check(src, load_roster(ros), NOW, fleet=load_fleet(cfg))
    reasons = {u.name[:4]: u.reason for u in report.unexpected_files}
    # everything is still RECORDED, with its reason
    assert reasons == {
        "OURS": "station_not_in_roster",  # local, missing from the roster
        "BRUX": IGNORED_REASON,  # abroad
        "GONE": IGNORED_REASON,  # discontinued
        "XXXX": IGNORED_REASON,  # not in stations.cfg at all
    }
    summary = report.to_json()["summary"]
    assert (summary["unexpected_files"], summary["ignored_files"]) == (1, 3)
    assert IGNORED_REASON == "not_local"


def test_station_info_is_sent_for_roster_stations_only(tmp_path, cfg):
    src, ros = _setup(tmp_path)
    info = load_stations(cfg)
    doc = run_check(src, load_roster(ros), NOW, stations=info).to_json()
    assert doc["station_info"] == [{"station": "AFST", "local": True, "agency": "IMO"}]
    # without stations.cfg nothing is sent, so the board keeps the last facts
    assert "station_info" not in run_check(src, load_roster(ros), NOW).to_json()


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
    doc = json.loads(capsys.readouterr().out)
    summary = doc["summary"]
    assert (summary["unexpected_files"], summary["ignored_files"]) == (4, 0)
    # --no-ignore changes the counting only; the board still gets the facts
    assert doc["station_info"] == [{"station": "AFST", "local": True, "agency": "IMO"}]


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


def test_completion_is_inferred_per_contiguous_range_of_processed_days(tmp_path):
    # three expected stations; a day is processed when >= half have it.
    # days[2] has one of three (an outage): it splits the ranges and stays out.
    src = tmp_path / "pos"
    src.mkdir()
    days = check_window(NOW.date())
    outage, gap = days[2], days[10]
    write_pos(src / "AFST.imo.24hr_itrf14.pos", [x for x in days if x != gap])
    write_pos(src / "THOB.imo.24hr_itrf14.pos",
              [x for x in days if x != outage], station="THOB")  # fmt: skip
    write_pos(src / "OLKE.imo.24hr_itrf14.pos",
              [x for x in days if x != outage], station="OLKE")  # fmt: skip
    ros = tmp_path / "daily_solutions.yaml"
    ros.write_text(
        "version: 1\nstream: imo.24hr_itrf14\nreviewed: {from: 2026-01-01}\n"
        "stations:\n" + "".join(f"  {s}: {{expected: [{{from: 2021-01-01}}]}}\n"
                                for s in ("AFST", "THOB", "OLKE"))  # fmt: skip
    )
    # only AFST has the outage day: 1 of 3 < 0.5. AFST's own gap is covered
    report = run_check(src, load_roster(ros), NOW)
    comps = report.completions()
    spans = [(c["day_from"], c["day_to"]) for c in comps]
    assert spans == [
        (days[0].isoformat(), days[1].isoformat()),
        (days[3].isoformat(), days[-1].isoformat()),
    ]
    doc = report.to_json()
    assert doc["completion_id"] == f"files:{report.run_id}"
    assert all(c["completion_id"].startswith("files:") for c in doc["completions"])
