"""``receivers solution-health``: daily POS solution monitoring.

``check`` reads a POS directory and a roster file and reports facts per
station-day. Without ``--record-dsn`` it touches no database and writes
nothing. With it, the run is recorded in each named database separately
(owner decision G8); in slice 2 only local disposable databases are accepted.

Exit codes: 0 = the check ran (whatever it found); 2 = it could not run
(roster invalid, source unavailable, destination refused); 3 = a
``--record-dsn`` destination failed (the others were still written).
An exit code of 0 says nothing about completeness: read the report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import sys
from pathlib import Path

from ..monitoring.solutions.check import Cell, CheckReport, run_check
from ..monitoring.solutions.roster import RosterError, load_roster
from ..monitoring.solutions.store import (
    DestinationRefusedError,
    check_destination,
    record_to_destinations,
)
from ..monitoring.solutions.window import WINDOW_DAYS

EXIT_OK = 0
EXIT_CANNOT_CHECK = 2
EXIT_RECORD_FAILED = 3


def _parse_now(value: str | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.UTC)
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--now: not ISO 8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--now must carry a UTC offset (e.g. ...Z)")
    return parsed


def _ranges(cells: list[Cell]) -> list[tuple[dt.date, dt.date, str]]:
    """Collapse consecutive days with the same fact/code into ranges."""
    out: list[tuple[dt.date, dt.date, str]] = []
    for c in cells:
        label = c.fact if c.code is None else f"{c.fact}:{c.code}"
        if out and out[-1][2] == label and out[-1][1] + dt.timedelta(days=1) == c.day:
            out[-1] = (out[-1][0], c.day, label)
        else:
            out.append((c.day, c.day, label))
    return out


def _print_human(report: CheckReport) -> None:
    w = report.window
    print(f"stream   {report.roster.spec.stream}  ({report.source})")
    print(f"window   {w[0]} .. {w[-1]}  ({len(w)} UTC days)")
    print(
        f"roster   {len(report.roster.stations)} stations, "
        f"version {report.roster.version[:12]}"
    )
    print(f"outcome  {report.outcome}" + (f": {report.error}" if report.error else ""))
    if report.outcome != "ok":
        print("No station-day facts: the source could not be read (all UNKNOWN).")
        return
    counts = ", ".join(f"{k} {v}" for k, v in report.fact_counts.items())
    print(f"facts    {counts}")

    exceptions = report.exceptions()
    print(f"\nExpected station-days not valid: {len(exceptions)}")
    for sid, group in itertools.groupby(exceptions, key=lambda c: c.station):
        parts = []
        for start, end, label in _ranges(list(group)):
            span = str(start) if start == end else f"{start}..{end}"
            parts.append(f"{span} {label}")
        print(f"  {sid}  " + "; ".join(parts))

    if report.unexpected_files:
        print(f"\nUnexpected files: {len(report.unexpected_files)}")
        for u in report.unexpected_files:
            print(f"  {u.name}  ({u.reason})")
    if report.other_products:
        others = ", ".join(f"{k} {v}" for k, v in report.other_products.items())
        print(f"\nOther products in the directory (not checked): {others}")


def cmd_solution_health_check(args: argparse.Namespace) -> int:
    try:
        now = _parse_now(args.now)
    except argparse.ArgumentTypeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CANNOT_CHECK
    try:
        roster = load_roster(Path(args.roster).expanduser())
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CANNOT_CHECK

    dsns: list[str] = list(args.record_dsn or [])
    for dsn in dsns:  # refuse before reading anything, not after
        try:
            check_destination(dsn, production=args.production)
        except DestinationRefusedError as exc:
            print(f"error: --record-dsn {exc}", file=sys.stderr)
            return EXIT_CANNOT_CHECK

    report = run_check(
        Path(args.source).expanduser(), roster, now, window_days=args.window_days
    )
    payload = report.to_json()
    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_human(report)

    record_failed = False
    if dsns:
        # A source_unavailable run is recorded too: it is what turns the
        # window's cells unknown instead of leaving an old green in place.
        for res in record_to_destinations(dsns, payload, production=args.production):
            if res.ok:
                print(f"recorded {res.label}: {res.status}", file=sys.stderr)
            else:
                record_failed = True
                print(f"error: record {res.label}: {res.error}", file=sys.stderr)
    if record_failed:
        return EXIT_RECORD_FAILED
    return EXIT_OK if report.outcome == "ok" else EXIT_CANNOT_CHECK


def create_solution_health_parser(subparsers) -> argparse.ArgumentParser:
    """Register ``solution-health`` (with its ``check`` verb) on ``subparsers``."""
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "solution-health",
        help="Daily GNSS POS solution monitoring",
        description=(
            "Check daily POS solution files against an explicit roster. "
            "Reports facts per station-day (valid / invalid / absent / "
            "unverifiable); it never infers expectations from the files present."
        ),
    )
    verbs = parser.add_subparsers(dest="solution_health_verb", required=True)
    check = verbs.add_parser(
        "check",
        help="Scan a POS directory against a roster",
        description=(
            f"Re-check the last {WINDOW_DAYS} completed UTC days of every roster "
            "station. Reads only; writes nothing unless --record-dsn is given."
        ),
    )
    check.add_argument("--source", required=True, help="POS directory to read")
    check.add_argument(
        "--roster", required=True, help="Roster file (daily_solutions.yaml)"
    )
    check.add_argument(
        "--now",
        help="Evaluate as of this instant (ISO 8601 with offset; default: now)",
    )
    check.add_argument(
        "--window-days",
        type=int,
        default=WINDOW_DAYS,
        help=f"Completed UTC days to check (default {WINDOW_DAYS})",
    )
    check.add_argument("--json", action="store_true", help="JSON facts on stdout")
    check.add_argument(
        "--record-dsn",
        action="append",
        metavar="DSN",
        help="Also record the run in this database via record_solution_check "
        "(repeatable; each destination is written separately and every failure "
        "is reported). Without --production: local disposable databases only.",
    )
    check.add_argument(
        "--production",
        action="store_true",
        help="Allow --record-dsn to name the production databases, and only "
        "those: gps_health on this machine, or on pgdev.vedur.is",
    )
    check.set_defaults(func=cmd_solution_health_check)
    return parser
