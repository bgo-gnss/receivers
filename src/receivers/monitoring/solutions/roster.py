"""The explicit daily-processing roster (``daily_solutions.yaml``).

Expectations are *never* inferred from the files present or from active
receivers: yang holds 385 ``imo.24hr_itrf14`` files but only 270 stations are
maintained. The roster says, per station, from when to when a daily solution
is expected, and which periods are explicitly excluded.

Schema (version 1)::

    version: 1
    stream: imo.24hr_itrf14
    frame_label: IGb14        # optional; ProductSpec default otherwise
    session: 24hrs            # optional
    reviewed:                 # the period someone has actually reviewed
      from: 2026-10-01
      to: null                # inclusive; null = open
    stations:
      AFST:
        expected:
          - {from: 2021-03-04}            # to omitted = open
        excluded:
          - {from: 2026-11-01, to: 2026-11-03, reason: "antenna swap"}

All dates are inclusive UTC calendar dates. Per day, a station is
``unreviewed`` outside ``reviewed``, ``excluded`` inside an excluded interval,
``expected`` inside an expected interval, and ``not_expected`` otherwise.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml

from .pos_reader import ProductSpec, is_station_id

RosterFact = Literal["unreviewed", "excluded", "expected", "not_expected"]
SUPPORTED_VERSION = 1


class RosterError(ValueError):
    """The roster file is missing, unparsable, or breaks the schema."""


@dataclasses.dataclass(frozen=True)
class Interval:
    start: dt.date
    end: dt.date | None  # inclusive; None = open
    reason: str | None = None

    def contains(self, day: dt.date) -> bool:
        return self.start <= day and (self.end is None or day <= self.end)


@dataclasses.dataclass(frozen=True)
class StationRoster:
    expected: tuple[Interval, ...]
    excluded: tuple[Interval, ...]


@dataclasses.dataclass(frozen=True)
class Roster:
    spec: ProductSpec
    reviewed: Interval
    stations: dict[str, StationRoster]
    #: sha256 of the roster file bytes; stored with every check run
    version: str
    path: Path

    def fact(self, station: str, day: dt.date) -> RosterFact:
        if not self.reviewed.contains(day):
            return "unreviewed"
        entry = self.stations.get(station)
        if entry is None:
            return "not_expected"
        if any(iv.contains(day) for iv in entry.excluded):
            return "excluded"
        if any(iv.contains(day) for iv in entry.expected):
            return "expected"
        return "not_expected"


def _date(value: Any, where: str) -> dt.date:
    # PyYAML already turns an unquoted 2026-10-01 into a date.
    if isinstance(value, dt.datetime):
        raise RosterError(f"{where}: expected a date, got a timestamp {value!r}")
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            pass
    raise RosterError(f"{where}: expected a YYYY-MM-DD date, got {value!r}")


def _interval(raw: Any, where: str) -> Interval:
    if not isinstance(raw, dict):
        raise RosterError(f"{where}: expected a mapping with 'from'")
    unknown = set(raw) - {"from", "to", "reason"}
    if unknown:
        raise RosterError(f"{where}: unknown keys {sorted(unknown)}")
    if "from" not in raw:
        raise RosterError(f"{where}: 'from' is required")
    start = _date(raw["from"], f"{where}.from")
    end = None if raw.get("to") is None else _date(raw["to"], f"{where}.to")
    if end is not None and end < start:
        raise RosterError(f"{where}: 'to' {end} is before 'from' {start}")
    reason = raw.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise RosterError(f"{where}.reason: expected text")
    return Interval(start, end, reason)


def _intervals(raw: Any, where: str) -> tuple[Interval, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RosterError(f"{where}: expected a list of intervals")
    return tuple(_interval(item, f"{where}[{k}]") for k, item in enumerate(raw))


def load_roster(path: Path) -> Roster:
    """Load and validate a roster; raise :class:`RosterError` on any defect."""
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise RosterError(f"{path}: cannot read roster ({exc.strerror})") from exc
    try:
        doc = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise RosterError(f"{path}: not valid YAML ({exc})") from exc
    except ValueError as exc:
        # PyYAML builds dates itself; an impossible one (2026-13-01) escapes
        # as a bare ValueError, not a YAMLError.
        raise RosterError(f"{path}: invalid date in roster ({exc})") from exc
    if not isinstance(doc, dict):
        raise RosterError(f"{path}: the roster must be a mapping")
    unknown = set(doc) - {
        "version",
        "stream",
        "frame_label",
        "session",
        "reviewed",
        "stations",
    }
    if unknown:
        raise RosterError(f"{path}: unknown keys {sorted(unknown)}")
    if doc.get("version") != SUPPORTED_VERSION:
        raise RosterError(
            f"{path}: version must be {SUPPORTED_VERSION}, got {doc.get('version')!r}"
        )
    stream = doc.get("stream")
    if not isinstance(stream, str) or not stream:
        raise RosterError(f"{path}: 'stream' is required")

    defaults = ProductSpec()
    if stream != defaults.stream and ("frame_label" not in doc or "session" not in doc):
        # The defaults describe imo.24hr_itrf14 only; never let another
        # stream silently inherit that product's frame and session.
        raise RosterError(
            f"{path}: stream {stream!r} must state frame_label and session"
        )
    spec = ProductSpec(
        stream=stream,
        frame_label=str(doc.get("frame_label", defaults.frame_label)),
        session=str(doc.get("session", defaults.session)),
    )

    reviewed = _interval(doc.get("reviewed"), "reviewed")

    stations_raw = doc.get("stations")
    if not isinstance(stations_raw, dict) or not stations_raw:
        raise RosterError(f"{path}: 'stations' must be a non-empty mapping")
    stations: dict[str, StationRoster] = {}
    for sid, entry in stations_raw.items():
        if not isinstance(sid, str) or not is_station_id(sid):
            raise RosterError(f"stations: {sid!r} is not a 4-character upper-case id")
        where = f"stations.{sid}"
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            raise RosterError(f"{where}: expected a mapping")
        unknown = set(entry) - {"expected", "excluded"}
        if unknown:
            raise RosterError(f"{where}: unknown keys {sorted(unknown)}")
        stations[sid] = StationRoster(
            expected=_intervals(entry.get("expected"), f"{where}.expected"),
            excluded=_intervals(entry.get("excluded"), f"{where}.excluded"),
        )

    return Roster(
        spec=spec,
        reviewed=reviewed,
        stations=stations,
        version=hashlib.sha256(raw_bytes).hexdigest(),
        path=path,
    )
