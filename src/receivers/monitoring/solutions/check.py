"""One check run: scan a POS directory against a roster and report facts.

The output is facts only (per station-day: the roster fact and the file
fact). It deliberately does not say "pending", "overdue" or "available";
those states need the deadline, the producer completion record and the check
history, and live in one place only, the SQL function ``solution_day_grid``
(plan §3, slice 2).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from .pos_reader import PosInspection, inspect_pos, is_station_id
from .roster import Roster, RosterFact
from .window import WINDOW_DAYS, check_window

CellFactKind = Literal["valid", "invalid", "absent", "unverifiable"]
Outcome = Literal["ok", "source_unavailable"]

#: /2 (slice 2): adds the roster snapshot that record_solution_check installs
SCHEMA = "solution-check-facts/2"
#: the unexpected-file reason that is recorded but not counted (fleet.py)
IGNORED_REASON = "not_in_fleet"

#: earlier names of the same series (mirrors ``geo_dataread.pos.LEGACY_IDS``,
#: kept here because receivers does not depend on geo_dataread). A file under
#: one of these is reported, never read in place of the real name.
LEGACY_IDS: dict[str, tuple[str, ...]] = {
    "imo.24hr_itrf08": ("imo.daily_itr08", "imo.final_itr08"),
    "imo.24hr_itrf14": ("imo.daily_igb14",),
    "imo.24hr_itrf20": ("imo.final_igb20",),
}

_NAME_RE = re.compile(r"^(?P<sid>[A-Za-z0-9]{4})\.(?P<rest>.+)$")


@dataclasses.dataclass(frozen=True)
class Cell:
    station: str
    day: dt.date
    roster: RosterFact
    fact: CellFactKind
    code: str | None
    fingerprint: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "day": self.day.isoformat(),
            "roster": self.roster,
            "fact": self.fact,
            "code": self.code,
            "fingerprint": self.fingerprint,
        }


@dataclasses.dataclass(frozen=True)
class UnexpectedFile:
    name: str
    reason: str

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "reason": self.reason}


@dataclasses.dataclass(frozen=True)
class CheckReport:
    run_id: str
    checked_at: dt.datetime
    source: Path
    roster: Roster
    window: list[dt.date]
    outcome: Outcome
    error: str | None
    files: list[PosInspection]
    cells: list[Cell]
    unexpected_files: list[UnexpectedFile]
    #: other ``.pos`` products in the same directory, by product → count
    other_products: dict[str, int]

    @property
    def fact_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(c.fact for c in self.cells).items()))

    def exceptions(self) -> list[Cell]:
        """Expected station-days that are not valid: what an operator reads."""
        return [c for c in self.cells if c.roster == "expected" and c.fact != "valid"]

    @property
    def counted_unexpected(self) -> list[UnexpectedFile]:
        """Unexpected files an operator should look at (not ``not_in_fleet``)."""
        return [u for u in self.unexpected_files if u.reason != IGNORED_REASON]

    @property
    def ignored_files(self) -> list[UnexpectedFile]:
        return [u for u in self.unexpected_files if u.reason == IGNORED_REASON]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "checked_at": self.checked_at.isoformat(),
            "stream": self.roster.spec.stream,
            "source": str(self.source),
            "outcome": self.outcome,
            "error": self.error,
            "window": {
                "from": self.window[0].isoformat(),
                "to": self.window[-1].isoformat(),
                "days": len(self.window),
            },
            "roster": {
                "path": str(self.roster.path),
                "version": self.roster.version,
                "stations": len(self.roster.stations),
                "frame_label": self.roster.spec.frame_label,
                "session": self.roster.spec.session,
                **self.roster.snapshot(),
            },
            "summary": {
                "facts": self.fact_counts,
                "expected_not_valid": len(self.exceptions()),
                "unexpected_files": len(self.counted_unexpected),
                "ignored_files": len(self.ignored_files),
            },
            "files": [
                {
                    "station": f.station,
                    "path": str(f.path),
                    "file_fact": f.file_fact,
                    "file_code": f.file_code,
                    "sha256": f.sha256,
                    "size": f.size,
                }
                for f in self.files
            ],
            "cells": [c.to_json() for c in self.cells],
            "unexpected_files": [u.to_json() for u in self.unexpected_files],
            "other_products": self.other_products,
        }


def _product_of(rest: str) -> str | None:
    """``imo.24hr_itrf08.pos`` → ``imo.24hr_itrf08``; non-POS names → None."""
    for suffix in (".pos.gz", ".pos"):
        if rest.endswith(suffix):
            return rest[: -len(suffix)]
    return None


def _classify(
    names: list[str], roster: Roster, fleet: frozenset[str] | None = None
) -> tuple[dict[str, str], list[UnexpectedFile], dict[str, int]]:
    """Split directory entries into (station → file name, unexpected, others).

    A file for a station outside the roster is ``station_not_in_roster`` when
    the station is in ``fleet`` (or no fleet is given), else ``not_in_fleet``.
    """
    stream = roster.spec.stream
    legacy = set(LEGACY_IDS.get(stream, ()))
    wanted: dict[str, str] = {}
    unexpected: list[UnexpectedFile] = []
    others: Counter[str] = Counter()
    for name in sorted(names):
        m = _NAME_RE.match(name)
        if m is None:
            continue
        sid, rest = m["sid"], m["rest"]
        product = _product_of(rest)
        if product is None:
            continue  # csv, logs, warnings: not POS files
        if product == stream:
            if rest.endswith(".gz"):
                unexpected.append(UnexpectedFile(name, "compressed"))
            elif not is_station_id(sid):
                unexpected.append(UnexpectedFile(name, "bad_station_name"))
            elif sid not in roster.stations:
                ours = fleet is None or sid in fleet
                reason = "station_not_in_roster" if ours else IGNORED_REASON
                unexpected.append(UnexpectedFile(name, reason))
            else:
                wanted[sid] = name
        elif product in legacy:
            unexpected.append(UnexpectedFile(name, "legacy_alias"))
        else:
            others[product] += 1
    return wanted, unexpected, dict(sorted(others.items()))


def _cell(
    station: str, day: dt.date, roster: Roster, insp: PosInspection | None
) -> Cell:
    rfact = roster.fact(station, day)
    if insp is None:
        return Cell(station, day, rfact, "absent", None, None)
    if insp.file_fact != "ok":
        kind: CellFactKind = (
            "unverifiable" if insp.file_fact == "unverifiable" else "invalid"
        )
        return Cell(station, day, rfact, kind, insp.file_code, None)
    fact = insp.days.get(day)
    if fact is None:
        return Cell(station, day, rfact, "absent", None, None)
    return Cell(station, day, rfact, fact.fact, fact.code, fact.fingerprint)


def run_check(
    source: Path,
    roster: Roster,
    now: dt.datetime,
    *,
    window_days: int = WINDOW_DAYS,
    run_id: str | None = None,
    fleet: frozenset[str] | None = None,
) -> CheckReport:
    """Check ``source`` against ``roster`` for the window ending before ``now``.

    ``now`` must be timezone-aware; it is the only clock this function uses.
    ``fleet`` (fleet.py) turns files of stations outside it into ignored
    ``not_in_fleet`` entries; ``None`` counts every such file as unexpected.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(dt.UTC)
    window = check_window(now.date(), window_days)
    rid = run_id or str(uuid.uuid4())

    def report(
        outcome: Outcome,
        error: str | None,
        files: list[PosInspection],
        cells: list[Cell],
        unexpected: list[UnexpectedFile],
        others: dict[str, int],
    ) -> CheckReport:
        return CheckReport(
            rid, now, source, roster, window, outcome, error, files, cells,
            unexpected, others,
        )  # fmt: skip

    try:
        names = os.listdir(source)
    except OSError as exc:
        # A lost mount or a permission change must read as "unknown", never as
        # 270 stations x 45 days of absence.
        return report("source_unavailable", f"{source}: {exc.strerror}", [], [], [], {})

    wanted, unexpected, others = _classify(names, roster, fleet)
    inspections = {
        sid: inspect_pos(source / name, sid, roster.spec)
        for sid, name in sorted(wanted.items())
    }
    cells = [
        _cell(sid, day, roster, inspections.get(sid))
        for sid in sorted(roster.stations)
        for day in window
    ]
    return report("ok", None, list(inspections.values()), cells, unexpected, others)
