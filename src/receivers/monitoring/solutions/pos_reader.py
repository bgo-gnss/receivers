"""Strict reader for PBO ``.pos`` daily solution files.

This is deliberately *not* ``geo_dataread.pos.read_pos``. That reader is built
for analysis and is forgiving: it falls back to the file name for the station
identity and accepts legacy product names. A monitor must not be forgiving,
because every fallback is a way to call a wrong file green. The two are tied
together by a drift guard (a contract fixture written by
``geo_dataread.pos.write_pos``), not by sharing code.

The reader reports facts per calendar day:

* ``valid``: exactly one well-formed row for that date;
* ``invalid``: a row (or the whole file) that is wrong; ``code`` says how;
* ``unverifiable``: the file could not be read, or a row could not be dated,
  so no claim about any day in it can be made.

A day with no row at all does not appear in :attr:`PosInspection.days`; the
caller decides what an absent day means.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import math
import re
from pathlib import Path
from typing import Literal

DayFactKind = Literal["valid", "invalid", "unverifiable"]
FileFactKind = Literal["ok", "invalid", "unverifiable"]

PBO_MAGIC = "PBO Station Position Time Series"
COLUMN_HEADER = "*YYYYMMDD"
_MJD0 = dt.date(1858, 11, 17)
_DATE_RE = re.compile(r"^\d{8}$")
_TIME_RE = re.compile(r"^\d{6}$")
_STATION_RE = re.compile(r"^[A-Z0-9]{4}$")
#: column indices (0-based) of X, Y, Z
_XYZ_COLS = (3, 4, 5)
#: numeric columns: mjd (2) to Reu (23)
_NUM_COLS = range(2, 24)


@dataclasses.dataclass(frozen=True)
class ProductSpec:
    """What a file of one stream must look like. Every field is pinned.

    The defaults describe ``imo.24hr_itrf14`` as written on yang-d01
    (inspected 2026-11-21): frame label ``IGb14``, solution column ``24hrs``,
    epoch ``11:59:00``. Whether the product id should say ``itrf14`` while the
    label says ``IGb14`` is owner gate G6; the label is configurable for that
    reason, never inferred.
    """

    stream: str = "imo.24hr_itrf14"
    frame_label: str = "IGb14"
    session: str = "24hrs"
    epoch_time: str = "115900"
    ncols: int = 25

    @property
    def filename_suffix(self) -> str:
        return f".{self.stream}.pos"


@dataclasses.dataclass(frozen=True)
class DayFact:
    """The fact for one calendar day that has at least one row in the file."""

    fact: DayFactKind
    code: str | None
    #: sha256 of the normalised row; ``None`` when the day has several rows
    fingerprint: str | None


@dataclasses.dataclass(frozen=True)
class PosInspection:
    """Everything the strict reader learned about one file."""

    path: Path
    station: str
    file_fact: FileFactKind
    #: why the file as a whole is invalid/unverifiable; ``None`` when ``ok``
    file_code: str | None
    sha256: str | None
    size: int | None
    #: per-day facts; empty unless ``file_fact == "ok"``
    days: dict[dt.date, DayFact]


def normalise_row(line: str) -> str:
    """A row with runs of whitespace collapsed: the fingerprinted form.

    Column alignment is presentation; a re-make that only re-pads a row is
    not a revision.
    """
    return " ".join(line.split())


def row_fingerprint(line: str) -> str:
    return hashlib.sha256(normalise_row(line).encode("ascii")).hexdigest()


def _parse_date(token: str) -> dt.date | None:
    if not _DATE_RE.match(token):
        return None
    try:
        return dt.date(int(token[:4]), int(token[4:6]), int(token[6:8]))
    except ValueError:
        return None


def _row_problem(tokens: list[str], day: dt.date, spec: ProductSpec) -> str | None:
    """The first thing wrong with a dated row, or ``None``."""
    if len(tokens) != spec.ncols:
        return "bad_column_count"
    time_tok = tokens[1]
    if not _TIME_RE.match(time_tok):
        return "bad_epoch_time"
    if time_tok != spec.epoch_time:
        return "unexpected_epoch_time"
    for col in _NUM_COLS:
        tok = tokens[col]
        if tok.startswith("*"):
            # Fortran overflow: tolerated only outside X/Y/Z (tssum writes it
            # for |dN/dE| >= 1e4 m when the reference is far from the site).
            if col in _XYZ_COLS:
                return "nonfinite_xyz"
            if col == 2:
                return "bad_number"  # the MJD dates the row; it must be real
            continue
        try:
            value = float(tok)
        except ValueError:
            return "bad_number"
        if not math.isfinite(value):
            return "nonfinite_xyz" if col in _XYZ_COLS else "nonfinite_value"
    seconds = int(time_tok[:2]) * 3600 + int(time_tok[2:4]) * 60 + int(time_tok[4:6])
    expected_mjd = (day - _MJD0).days + seconds / 86400.0
    if abs(float(tokens[2]) - expected_mjd) > 5e-4:
        return "mjd_mismatch"
    if tokens[24] != spec.session:
        return "session_mismatch"
    return None


def _frame_label(first_line: str) -> str | None:
    _, sep, rest = first_line.partition("Reference Frame")
    if not sep:
        return None
    label = rest.lstrip().removeprefix(":").strip()
    return label or None


def _whole_file(
    path: Path,
    station: str,
    fact: FileFactKind,
    code: str,
    sha: str | None = None,
    size: int | None = None,
) -> PosInspection:
    return PosInspection(path, station, fact, code, sha, size, {})


def inspect_pos(path: Path, station: str, spec: ProductSpec) -> PosInspection:
    """Strictly inspect ``path``, which must hold ``station``'s ``spec`` series.

    ``station`` is what the file *should* contain (from its name); the header
    must say the same, and is never filled in from the name.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return _whole_file(path, station, "unverifiable", "unreadable")
    sha = hashlib.sha256(raw).hexdigest()
    size = len(raw)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return _whole_file(path, station, "invalid", "not_ascii", sha, size)

    lines = text.splitlines()
    if not lines or not lines[0].startswith(PBO_MAGIC):
        return _whole_file(path, station, "invalid", "not_pbo", sha, size)

    header: dict[str, str] = {}
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith(COLUMN_HEADER):
            start = i + 1
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() and not line.startswith(" "):
            header.setdefault(key.strip(), value.strip())
    if start is None:
        return _whole_file(path, station, "invalid", "no_column_header", sha, size)

    if _frame_label(lines[0]) != spec.frame_label:
        return _whole_file(path, station, "invalid", "frame_mismatch", sha, size)
    header_id = header.get("4-character ID", "").strip()
    if not header_id:
        return _whole_file(path, station, "invalid", "missing_station_id", sha, size)
    if header_id != station:
        return _whole_file(path, station, "invalid", "station_mismatch", sha, size)

    by_day: dict[dt.date, list[tuple[list[str], str]]] = {}
    for line in lines[start:]:
        tokens = line.split()
        if not tokens:
            continue
        day = _parse_date(tokens[0])
        if day is None:
            # A row we cannot date could be a second copy of any day, so no
            # day in this file can be certified.
            return _whole_file(
                path, station, "unverifiable", "undatable_row", sha, size
            )
        by_day.setdefault(day, []).append((tokens, line))

    days: dict[dt.date, DayFact] = {}
    for day, rows in by_day.items():
        if len(rows) > 1:
            # Even identical duplicates: the producer contract is one row/day.
            days[day] = DayFact("invalid", "duplicate_date", None)
            continue
        tokens, line = rows[0]
        problem = _row_problem(tokens, day, spec)
        fingerprint = row_fingerprint(line)
        if problem is None:
            days[day] = DayFact("valid", None, fingerprint)
        else:
            days[day] = DayFact("invalid", problem, fingerprint)
    return PosInspection(path, station, "ok", None, sha, size, days)


def is_station_id(token: str) -> bool:
    return bool(_STATION_RE.match(token))
