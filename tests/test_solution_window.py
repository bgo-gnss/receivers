"""The 45-day check window and the roster/directory check built on it."""

from __future__ import annotations

import datetime as dt
import json
import textwrap
from pathlib import Path

import pytest

from receivers.cli.solution_health import (
    EXIT_CANNOT_CHECK,
    EXIT_OK,
    create_solution_health_parser,
)
from receivers.monitoring.solutions.check import run_check
from receivers.monitoring.solutions.roster import RosterError, load_roster
from receivers.monitoring.solutions.window import check_window, utc_today
from tests.solution_pos_helpers import row, write_pos

UTC = dt.UTC
NOW = dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
TODAY = NOW.date()
YESTERDAY = TODAY - dt.timedelta(days=1)
FIRST = TODAY - dt.timedelta(days=45)

# ---------------------------------------------------------------- window


def test_window_is_exactly_45_completed_days_ending_yesterday():
    w = check_window(TODAY)
    assert len(w) == 45
    assert w[0] == FIRST and w[-1] == YESTERDAY
    assert TODAY not in w
    assert w == sorted(w) and len(set(w)) == 45


def test_window_crosses_a_leap_day_by_date():
    w = check_window(dt.date(2028, 3, 1), 3)
    assert w == [dt.date(2028, 2, 27), dt.date(2028, 2, 28), dt.date(2028, 2, 29)]


def test_window_crosses_a_year_boundary():
    w = check_window(dt.date(2027, 1, 2), 3)
    assert w == [dt.date(2026, 12, 30), dt.date(2026, 12, 31), dt.date(2027, 1, 1)]


def test_utc_today_uses_utc_not_the_local_offset():
    late_evening_west = dt.datetime(
        2026, 11, 20, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=-2))
    )
    assert utc_today(late_evening_west) == dt.date(2026, 11, 21)


def test_naive_now_is_refused():
    with pytest.raises(ValueError):
        utc_today(dt.datetime(2026, 11, 21, 10, 0))


def test_window_days_must_be_positive():
    with pytest.raises(ValueError):
        check_window(TODAY, 0)


# ---------------------------------------------------------------- roster

ROSTER = """\
version: 1
stream: imo.24hr_itrf14
reviewed: {{from: {reviewed_from}}}
stations:
  AFST:
    expected:
      - {{from: 2021-03-04}}
    excluded:
      - {{from: {excl_from}, to: {excl_to}, reason: antenna swap}}
  THOB:
    expected:
      - {{from: 2020-01-01, to: {thob_end}}}
  NEWS:
    expected:
      - {{from: {news_start}}}
"""


def _roster_text(**over: object) -> str:
    values: dict[str, object] = {
        "reviewed_from": FIRST - dt.timedelta(days=5),
        "excl_from": TODAY - dt.timedelta(days=10),
        "excl_to": TODAY - dt.timedelta(days=8),
        "thob_end": TODAY - dt.timedelta(days=3),
        "news_start": TODAY - dt.timedelta(days=2),
    }
    values.update(over)
    return ROSTER.format(**values)


def _roster(tmp_path: Path, **over: object):
    p = tmp_path / "daily_solutions.yaml"
    p.write_text(_roster_text(**over))
    return load_roster(p)


def test_roster_facts_per_day(tmp_path):
    r = _roster(tmp_path, reviewed_from=TODAY - dt.timedelta(days=20))
    assert r.fact("AFST", TODAY - dt.timedelta(days=21)) == "unreviewed"
    assert r.fact("AFST", TODAY - dt.timedelta(days=9)) == "excluded"
    assert r.fact("AFST", TODAY - dt.timedelta(days=7)) == "expected"
    assert r.fact("THOB", TODAY - dt.timedelta(days=3)) == "expected"  # inclusive
    assert r.fact("THOB", TODAY - dt.timedelta(days=2)) == "not_expected"
    assert r.fact("NEWS", TODAY - dt.timedelta(days=3)) == "not_expected"
    assert r.fact("NEWS", TODAY - dt.timedelta(days=2)) == "expected"
    assert r.fact("ZZZZ", YESTERDAY) == "not_expected"


def test_roster_version_is_the_file_hash_and_changes_with_any_edit(tmp_path):
    a = _roster(tmp_path)
    b = _roster(tmp_path, thob_end=TODAY - dt.timedelta(days=4))
    assert len(a.version) == 64 and a.version != b.version


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("version: 2\nstream: s\n", "version"),
        ("- a\n", "mapping"),
        ("version: 1\nstream: imo.24hr_itrf14\nreviewed: {from: 2026-01-01}\n"
         "stations: {afst: {}}\n", "4-character"),
        ("version: 1\nstream: imo.24hr_itrf14\nreviewed: {from: 2026-01-01}\n"
         "stations: {}\n", "non-empty"),
        ("version: 1\nstream: imo.24hr_itrf14\nreviewed: {from: 2026-01-01, "
         "to: 2025-01-01}\nstations: {AFST: {}}\n", "before"),
        ("version: 1\nstream: imo.24hr_itrf14\nreviewed: {from: 2026-01-01}\n"
         "stations: {AFST: {expectd: []}}\n", "unknown keys"),
        ("version: 1\nstream: imo.24hr_itrf20\nreviewed: {from: 2026-01-01}\n"
         "stations: {AFST: {}}\n", "frame_label and session"),
        ("version: 1\nstream: imo.24hr_itrf14\nreviewed: {from: 2026-13-01}\n"
         "stations: {AFST: {}}\n", "date"),
        ("version: 1\nstream: imo.24hr_itrf14\nstations: {AFST: {}}\n", "from"),
        ("version: [\n", "YAML"),
    ],
)  # fmt: skip
def test_roster_defects_are_refused(tmp_path, text, match):
    p = tmp_path / "r.yaml"
    p.write_text(text)
    with pytest.raises(RosterError, match=match):
        load_roster(p)


def test_missing_roster_file(tmp_path):
    with pytest.raises(RosterError, match="cannot read"):
        load_roster(tmp_path / "nope.yaml")


# ---------------------------------------------------------------- check


def _populate(src: Path) -> None:
    window = check_window(TODAY)
    middle = window[20]
    # AFST: every day except one in the middle; an invalid day too
    rows = [row(d) for d in window if d != middle]
    rows[5] = row(window[5], soln="08hrs")
    write_pos(src / "AFST.imo.24hr_itrf14.pos", [], rows=rows)
    # THOB: only a legacy-named file, which must NOT be read in its place
    write_pos(src / "THOB.imo.daily_igb14.pos", window, station="THOB")
    # NEWS: absent entirely
    # A station not in the roster, a compressed copy, and noise
    write_pos(src / "ZZZZ.imo.24hr_itrf14.pos", window, station="ZZZZ")
    (src / "AFST.imo.24hr_itrf14.pos.gz").write_bytes(b"\x1f\x8b")
    write_pos(src / "AFST.imo.24hr_itrf08.pos", window)
    write_pos(src / "AFST.imo.08hr_itrf20.pos", window)
    (src / "days.imo.24hr_itrf14.csv").write_text("x\n")
    (src / "afst.imo.24hr_itrf14.pos").write_text("x\n")


def _cells(report, station):
    return {c.day: c for c in report.cells if c.station == station}


def test_check_reports_facts_for_every_roster_station_day(tmp_path):
    src = tmp_path / "pos"
    src.mkdir()
    _populate(src)
    report = run_check(src, _roster(tmp_path), NOW, run_id="r1")
    window = check_window(TODAY)

    assert report.outcome == "ok" and report.run_id == "r1"
    assert len(report.cells) == 3 * 45

    afst = _cells(report, "AFST")
    assert afst[window[20]].fact == "absent"
    assert (afst[window[5]].fact, afst[window[5]].code) == (
        "invalid",
        "session_mismatch",
    )
    assert afst[window[0]].fact == "valid" and afst[window[0]].fingerprint

    # the legacy alias is reported, never read: THOB is absent
    assert {c.fact for c in _cells(report, "THOB").values()} == {"absent"}
    assert {c.fact for c in _cells(report, "NEWS").values()} == {"absent"}

    unexpected = {(u.name, u.reason) for u in report.unexpected_files}
    assert unexpected == {
        ("THOB.imo.daily_igb14.pos", "legacy_alias"),
        ("ZZZZ.imo.24hr_itrf14.pos", "station_not_in_roster"),
        ("AFST.imo.24hr_itrf14.pos.gz", "compressed"),
        ("afst.imo.24hr_itrf14.pos", "bad_station_name"),
    }
    assert report.other_products == {"imo.08hr_itrf20": 1, "imo.24hr_itrf08": 1}
    assert [f.station for f in report.files] == ["AFST"]


def test_exceptions_are_expected_days_that_are_not_valid(tmp_path):
    src = tmp_path / "pos"
    src.mkdir()
    _populate(src)
    report = run_check(src, _roster(tmp_path), NOW)
    exc = report.exceptions()
    assert all(c.roster == "expected" and c.fact != "valid" for c in exc)
    # AFST's excluded days and NEWS's pre-start days are not exceptions
    news_expected = [c for c in exc if c.station == "NEWS"]
    assert len(news_expected) == 2


def test_a_file_level_defect_applies_to_every_window_day(tmp_path):
    src = tmp_path / "pos"
    src.mkdir()
    write_pos(src / "AFST.imo.24hr_itrf14.pos", check_window(TODAY), frame="ITRF2014")
    report = run_check(src, _roster(tmp_path), NOW)
    afst = _cells(report, "AFST").values()
    assert {(c.fact, c.code) for c in afst} == {("invalid", "frame_mismatch")}


def test_missing_source_is_unavailable_not_absent(tmp_path):
    report = run_check(tmp_path / "gone", _roster(tmp_path), NOW)
    assert report.outcome == "source_unavailable"
    assert report.cells == [] and report.error


def test_the_window_follows_now_not_the_files(tmp_path):
    src = tmp_path / "pos"
    src.mkdir()
    write_pos(src / "AFST.imo.24hr_itrf14.pos", [TODAY, YESTERDAY])
    report = run_check(src, _roster(tmp_path), NOW)
    afst = _cells(report, "AFST")
    assert TODAY not in afst  # today is not a completed day
    assert afst[YESTERDAY].fact == "valid"


def test_naive_now_is_refused_by_run_check(tmp_path):
    with pytest.raises(ValueError):
        run_check(tmp_path, _roster(tmp_path), dt.datetime(2026, 11, 21))


# ---------------------------------------------------------------- CLI


def _run_cli(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser()
    create_solution_health_parser(p.add_subparsers(dest="command"))
    args = p.parse_args(argv)
    return int(args.func(args))


def test_cli_json_output_is_facts_only(tmp_path, capsys):
    src = tmp_path / "pos"
    src.mkdir()
    _populate(src)
    roster = tmp_path / "daily_solutions.yaml"
    roster.write_text(_roster_text())
    rc = _run_cli(
        ["solution-health", "check", "--source", str(src), "--roster", str(roster),
         "--now", NOW.isoformat(), "--json"]
    )  # fmt: skip
    assert rc == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "solution-check-facts/2"
    assert doc["window"] == {
        "from": FIRST.isoformat(), "to": YESTERDAY.isoformat(), "days": 45
    }  # fmt: skip
    facts = {c["fact"] for c in doc["cells"]}
    assert facts <= {"valid", "invalid", "absent", "unverifiable"}
    # no display states leak into slice 1
    text = json.dumps(doc)
    for state in ("pending", "overdue", "available", "not expected"):
        assert state not in text


def test_cli_human_output_lists_exceptions(tmp_path, capsys):
    src = tmp_path / "pos"
    src.mkdir()
    _populate(src)
    roster = tmp_path / "daily_solutions.yaml"
    roster.write_text(_roster_text())
    rc = _run_cli(
        ["solution-health", "check", "--source", str(src), "--roster", str(roster),
         "--now", NOW.isoformat()]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "AFST" in out and "invalid:session_mismatch" in out
    assert "legacy_alias" in out


def test_cli_bad_roster_exits_cannot_check(tmp_path, capsys):
    roster = tmp_path / "r.yaml"
    roster.write_text("version: 9\n")
    rc = _run_cli(
        ["solution-health", "check", "--source", str(tmp_path), "--roster", str(roster)]
    )
    assert rc == EXIT_CANNOT_CHECK
    assert "version" in capsys.readouterr().err


def test_cli_missing_source_exits_cannot_check(tmp_path, capsys):
    roster = tmp_path / "daily_solutions.yaml"
    roster.write_text(_roster_text())
    rc = _run_cli(
        ["solution-health", "check", "--source", str(tmp_path / "gone"),
         "--roster", str(roster), "--now", NOW.isoformat()]
    )  # fmt: skip
    assert rc == EXIT_CANNOT_CHECK
    assert "UNKNOWN" in capsys.readouterr().out


def test_cli_naive_now_is_refused(tmp_path, capsys):
    roster = tmp_path / "daily_solutions.yaml"
    roster.write_text(_roster_text())
    rc = _run_cli(
        ["solution-health", "check", "--source", str(tmp_path), "--roster", str(roster),
         "--now", "2026-11-21T10:00:00"]
    )  # fmt: skip
    assert rc == EXIT_CANNOT_CHECK


def test_registered_on_the_main_parser():
    from receivers.cli.arguments import create_argument_parser

    ns = create_argument_parser().parse_args(
        ["solution-health", "check", "--source", "/x", "--roster", "/y"]
    )
    assert ns.func.__name__ == "cmd_solution_health_check"


def test_roster_template_text_is_valid_yaml(tmp_path):
    # the docstring example in roster.py must stay loadable
    from receivers.monitoring.solutions import roster as roster_mod

    doc = roster_mod.__doc__ or ""
    block = doc.split("Schema (version 1)::", 1)[1].split("\n\n", 2)[1]
    p = tmp_path / "doc.yaml"
    p.write_text(textwrap.dedent(block))
    r = load_roster(p)
    assert "AFST" in r.stations
