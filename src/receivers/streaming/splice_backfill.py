"""Backfill 15s daily RINEX from archived 1 Hz stream hours.

The live stream cycle (:mod:`receivers.streaming.pipeline`) splices today's and
yesterday's hourly 1 Hz RINEX into a 15 s daily. Days outside that window were
never revisited, so the archive holds three kinds of stream-station day the
24-hour processing cannot use:

``missing``
    1 Hz hours exist, no daily at all.
``truncated``
    A BNC daily exists but ends before the hours do. The cycle built it while
    the day was filling and a size-only skip froze it (SEY9 2026: 60 dailies
    ending ~08:00 of a 24-hour day).
``stream_r3``
    A complete BNC daily, but RINEX 3.01 with 2-char obs codes; GAMIT discards
    RINEX 3, and these codes are not valid R3 anyway. Rebuilt as 2.11.

Never touched: a daily from any other program (``authoritative`` — sbf2rin
from a downloaded SBF, the legacy teqc splice), and days whose 15 s raw is on
the archive (``rerinex`` — convert that raw instead; it is the better source).

Only stream sources are spliced (``PGM`` = BNC). 1 Hz hours converted from a
receiver download carry real RINEX 3 codes, and the right repair for a missing
daily there is a re-rinex of the 15 s raw.

Archive writes follow the one round-trip: read over the read-only mount, build
into a staging tree, push through the rawdata gateway, reindex the catalog. This
module plans, builds and validates; the CLI pushes.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from .downsample import (
    DownsampleResult,
    HeaderHook,
    RinexDownsampler,
    is_stream_product,
    read_rinex_header,
)
from .pipeline import daily_15s_target, hourly_1hz_sources

logger = logging.getLogger(__name__)

#: Reasons that lead to a build. Everything else is reported and skipped.
BUILD_REASONS = ("missing", "truncated", "stream_r3")

HeaderReader = Callable[[Path], Dict[str, str]]

_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


@dataclass
class SpliceDay:
    """Classification of one station-day."""

    station: str
    day: date
    reason: str
    sources: List[Path] = field(default_factory=list)
    daily: Optional[Path] = None
    detail: str = ""

    @property
    def build(self) -> bool:
        return self.reason in BUILD_REASONS

    @property
    def last_source_hour(self) -> int:
        return max((_hour_of(p) for p in self.sources), default=-1)


def _hour_of(path: Path) -> int:
    """Hour 0-23 from an hourly short name's session letter (``SEY9182c`` -> 2)."""
    letter = path.name[7:8].lower()
    return ord(letter) - ord("a") if "a" <= letter <= "x" else -1


def _obs_hour(value: Optional[str]) -> int:
    """Hour of a whitespace-normalised ``TIME OF … OBS`` value, -1 if absent."""
    parts = (value or "").split()
    try:
        return int(parts[3])
    except (IndexError, ValueError):
        return -1


def _day_dir(root: Path, station: str, day: date, session: str, kind: str) -> Path:
    return root / f"{day.year}" / _MONTHS[day.month - 1] / station / session / kind


def find_daily(root: Path, station: str, day: date) -> Optional[Path]:
    """The archived 15 s daily RINEX for a station-day, any case/compression."""
    d = _day_dir(root, station, day, "15s_24hr", "rinex")
    if not d.is_dir():
        return None
    doy = day.timetuple().tm_yday
    stem = f"{station}{doy:03d}0.{day.year % 100:02d}"
    hits = sorted(
        p
        for p in d.iterdir()
        if p.is_file()
        and p.name.upper().startswith(stem.upper())
        and p.name[len(stem) : len(stem) + 1].lower() in ("d", "o")
    )
    return hits[0] if hits else None


def raw15_present(root: Path, station: str, day: date) -> bool:
    """True when the 15 s raw for the day is on the archive.

    Raw names carry either ``YYYYMMDD`` (``SEY9202609010000a.sbf.gz``) or the
    short ``<STA><DOY>`` stem (``SEY92440.26_.gz``).
    """
    d = _day_dir(root, station, day, "15s_24hr", "raw")
    if not d.is_dir():
        return False
    ymd = day.strftime("%Y%m%d")
    short = f"{station}{day.timetuple().tm_yday:03d}"
    return any(
        p.is_file() and (ymd in p.name or p.name.upper().startswith(short.upper()))
        for p in d.iterdir()
    )


def classify_day(
    root: Path,
    station: str,
    day: date,
    *,
    convert_r3: bool = True,
    header_reader: HeaderReader = read_rinex_header,
) -> SpliceDay:
    """Decide what to do with one station-day (see the module docstring)."""
    root = Path(root)
    sources = hourly_1hz_sources(root, station, day)
    if not sources:
        return SpliceDay(station, day, "no_source")
    src_pgm = header_reader(sources[0]).get("pgm")
    if not is_stream_product(src_pgm):
        return SpliceDay(
            station, day, "non_stream_sources", sources, detail=f"PGM {src_pgm!r}"
        )
    daily = find_daily(root, station, day)
    if daily is None:
        if raw15_present(root, station, day):
            return SpliceDay(station, day, "rerinex", sources)
        return SpliceDay(station, day, "missing", sources)

    hdr = header_reader(daily)
    pgm = hdr.get("pgm")
    if not is_stream_product(pgm):
        return SpliceDay(
            station, day, "authoritative", sources, daily, detail=f"PGM {pgm!r}"
        )
    last_hour = _obs_hour(hdr.get("last_obs"))
    plan = SpliceDay(station, day, "complete", sources, daily)
    if last_hour < plan.last_source_hour:
        plan.reason = "truncated"
        plan.detail = (
            f"daily ends hour {last_hour}, hours run to {plan.last_source_hour}"
        )
    elif convert_r3 and not hdr.get("version", "").startswith("2"):
        plan.reason = "stream_r3"
        plan.detail = f"RINEX {hdr.get('version')}"
    return plan


def stream_days(
    root: Path, station: str, start: Optional[date], end: Optional[date]
) -> List[date]:
    """Every day in ``[start, end]`` (inclusive) with archived 1 Hz hours."""
    root = Path(root)
    found = set()
    pat = re.compile(rf"^{re.escape(station)}(\d{{3}})[a-xA-X]\.(\d{{2}})", re.I)
    for ydir in sorted(root.glob("[12][0-9][0-9][0-9]")):
        year = int(ydir.name)
        if (start and year < start.year) or (end and year > end.year):
            continue
        for mon in _MONTHS:
            d = ydir / mon / station / "1Hz_1hr" / "rinex"
            if not d.is_dir():
                continue
            for p in d.iterdir():
                m = pat.match(p.name)
                if not m or 2000 + int(m.group(2)) != year:
                    continue
                day = date(year, 1, 1) + timedelta(days=int(m.group(1)) - 1)
                if (start is None or day >= start) and (end is None or day <= end):
                    found.add(day)
    return sorted(found)


def plan_station(
    root: Path,
    station: str,
    start: Optional[date] = None,
    end: Optional[date] = None,
    *,
    today: Optional[date] = None,
    convert_r3: bool = True,
    header_reader: HeaderReader = read_rinex_header,
) -> List[SpliceDay]:
    """Classify every stream day of a station.

    Today and yesterday are left to the live cycle (``days_back: 1``): its hours
    are still arriving, and two writers on one daily would race.
    """
    today = today or datetime.utcnow().date()
    live_from = today - timedelta(days=1)
    plans = []
    for day in stream_days(root, station, start, end):
        if day >= live_from:
            plans.append(SpliceDay(station, day, "live_window"))
            continue
        plans.append(
            classify_day(
                root, station, day, convert_r3=convert_r3, header_reader=header_reader
            )
        )
    return plans


def count_epochs_with_sats(path: Path, crx2rnx: str = "CRX2RNX") -> int:
    """Epochs carrying at least one satellite, in a .Z/.gz Hatanaka RINEX 2 or 3.

    Epoch count alone is not data: SKDA 2017-07-20 had 4,008 epochs and zero
    satellites in every one. A RINEX 2 epoch line has the satellite count in
    columns 30-32; a RINEX 3 epoch line starts with ``>`` and has it in 33-35.
    """
    cmd = f"gzip -dc '{path}' | {crx2rnx} -"
    proc = subprocess.run(cmd, shell=True, capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"cannot decode {path.name}")
    lines = proc.stdout.decode("latin-1").splitlines()
    try:
        body = lines[
            next(i for i, ln in enumerate(lines) if "END OF HEADER" in ln) + 1 :
        ]
    except StopIteration:
        raise RuntimeError(f"no END OF HEADER in {path.name}") from None
    # (1X,I2,4(1X,I2),F11.7,2X,I1,I3): flag at col 29, satellite count 30-32
    r2_epoch = re.compile(r"^ [ \d]\d(?: [ \d]\d){4}[ \d.]{11}  [0-6]")
    n = 0
    for ln in body:
        if ln.startswith(">"):
            sats = ln[32:35]
        elif r2_epoch.match(ln):
            sats = ln[29:32]
        else:
            continue
        try:
            if int(sats) > 0:
                n += 1
        except ValueError:
            continue
    return n


@dataclass
class BuildOutcome:
    """A built day, ready to push (``ok``) or refused with a reason."""

    plan: SpliceDay
    staged: Path
    result: Optional[DownsampleResult] = None
    epochs: int = 0
    old_epochs: Optional[int] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def build_day(
    plan: SpliceDay,
    work_dir: Path,
    downsampler: RinexDownsampler,
    *,
    header_hook: Optional[HeaderHook] = None,
    epoch_counter: Callable[[Path], int] = count_epochs_with_sats,
) -> BuildOutcome:
    """Splice one planned day into the staging mirror and validate it.

    Validation, in order: the build succeeded; the product has epochs with
    satellites; and when it replaces a daily, it has at least as many of them
    as the old one. The last is what makes a replacement safe to push without
    raw: the old daily was itself made from these hours, so the new one can
    only add data. Anything else is refused and the staged file removed.
    """
    staged = daily_15s_target(work_dir, plan.station, plan.day)
    out = BuildOutcome(plan, staged)
    scratch = Path(work_dir) / ".splice_scratch"
    out.result = downsampler.downsample_day(
        plan.station,
        plan.sources,
        staged,
        scratch,
        skip_if_exists=False,
        header_hook=header_hook,
    )
    if out.result.status != "created":
        out.error = out.result.error or out.result.status
        staged.unlink(missing_ok=True)
        return out
    try:
        out.epochs = epoch_counter(staged)
        if out.epochs == 0:
            out.error = "no epoch carries a satellite"
        elif plan.daily is not None:
            out.old_epochs = epoch_counter(plan.daily)
            if out.epochs < out.old_epochs:
                out.error = (
                    f"fewer epochs with satellites than the daily it replaces "
                    f"({out.epochs} < {out.old_epochs})"
                )
    except Exception as exc:  # noqa: BLE001 - a failed check refuses the day
        out.error = f"validation failed: {exc}"
    if out.error:
        staged.unlink(missing_ok=True)
    return out


def make_tos_header_hook(
    station: str,
    day: date,
    *,
    station_config: Optional[dict] = None,
    tos_metadata_cache: Optional[dict] = None,
) -> HeaderHook:
    """Header correction for a spliced daily, the same call the converter makes.

    BNC writes its headers from a skeleton (``MARKER NUMBER`` = the 4-char id,
    ``ANT #`` = ``0000``, receiver frozen at whatever the skeleton held), so a
    splice inherits all of it. ``correct_rinex_from_tos`` rewrites the full set
    for the day (station config past ``rinex_config_valid_from``, TOS before),
    and applies the DOMES-only ``MARKER NUMBER`` rule. ``OBSERVER / AGENCY`` is
    injected as the converter does, since the TOS path does not emit it. Share
    one ``tos_metadata_cache`` across a station's days: one TOS fetch, not one
    per day.
    """

    def hook(obs: Path) -> None:
        from tostools.rinex import correct_rinex_from_tos

        extra = None
        rinex_cfg = (station_config or {}).get("rinex", {}) or {}
        observer = str(rinex_cfg.get("observer") or "").strip()
        agency = str(rinex_cfg.get("agency") or "").strip()
        if observer or agency:
            extra = {"OBSERVER / AGENCY": [observer, agency]}
        out = correct_rinex_from_tos(
            rinex_file=obs,
            station_id=station,
            observation_date=datetime(day.year, day.month, day.day),
            output_file=obs,
            station_config=station_config,
            extra_corrections=extra,
            tos_metadata_cache=tos_metadata_cache,
        )
        if out is None:
            raise RuntimeError("TOS header correction returned nothing")

    return hook


def summarise(plans: Iterable[SpliceDay]) -> Dict[str, int]:
    """Count plans per reason."""
    counts: Dict[str, int] = {}
    for p in plans:
        counts[p.reason] = counts.get(p.reason, 0) + 1
    return counts
