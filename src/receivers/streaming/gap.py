"""Detect gaps in stream-captured RINEX and fall back to file download.

Real-time RTCM3 streams drop out (radio/network), leaving holes in the hourly
1 Hz archive that BNC can never backfill. When a station's gaps exceed a policy
threshold, this module triggers a *file* download for the gap span from the
receiver's on-disk log (the Phase-1 mosaic/PolaRX5 download path) — the receiver
keeps logging to disk even while the stream is down, so the files can fill the holes.

Both the "is this hourly slot present in the archive?" check and the actual
downloader are injectable, so the detection + policy logic is unit-testable without
a filesystem archive or a live receiver. The default slot checker globs the archive
(tolerant of hour-letter case and compression-suffix variants); the real downloader
is wired in the scheduler-integration step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: slot_present(station_id, hour_dt) -> True if that hourly file is in the archive.
SlotChecker = Callable[[str, datetime], bool]
#: downloader(station_id, start, end) -> result; fetch files for [start, end].
Downloader = Callable[[str, datetime, datetime], object]


def _hour_letter(hour: int) -> str:
    """0 → 'a' … 23 → 'x' (RINEX hour code)."""
    return chr(ord("a") + hour)


def make_archive_slot_checker(
    archive_base: str | Path, session_type: str = "1Hz_1hr"
) -> SlotChecker:
    """Return a SlotChecker that globs the archive for an hourly RINEX file.

    Matches either hour-letter case and any compression suffix
    (``GONH162a.26D.Z``, ``GONH162A.26d.gz``, …).
    """
    base = Path(archive_base)

    def present(station_id: str, dt: datetime) -> bool:
        rinex_dir = (
            base
            / f"{dt.year}"
            / dt.strftime("%b").lower()
            / station_id
            / session_type
            / "rinex"
        )
        if not rinex_dir.is_dir():
            return False
        doy = dt.timetuple().tm_yday
        yy = dt.year % 100
        letter = _hour_letter(dt.hour)
        pattern = f"{station_id}{doy:03d}[{letter}{letter.upper()}].{yy:02d}*"
        return any(rinex_dir.glob(pattern))

    return present


def find_missing_hours(
    station_id: str,
    day: date,
    slot_present: SlotChecker,
    *,
    up_to: Optional[datetime] = None,
    hours: Sequence[int] = range(24),
) -> List[datetime]:
    """Return UTC datetimes of hourly slots on ``day`` that are missing.

    ``up_to`` excludes hours at/after a cutoff (don't flag the future or the most
    recent, still-arriving hours as gaps).
    """
    missing = []
    for h in hours:
        dt = datetime.combine(day, time(hour=h), tzinfo=UTC)
        if up_to is not None and dt > up_to:
            continue
        if not slot_present(station_id, dt):
            missing.append(dt)
    return missing


@dataclass(frozen=True)
class GapPolicy:
    """When to fall back to file download."""

    min_missing_to_fill: int = 2
    """Number of missing hours that triggers a download (smaller gaps tolerated)."""

    recent_grace_hours: int = 2
    """Don't treat the most recent N hours as gaps (stream/ingest may still lag)."""


@dataclass
class GapFillResult:
    station_id: str
    missing_hours: List[datetime] = field(default_factory=list)
    status: str = "complete"  # complete | below_threshold | filled | download_failed
    downloaded_span: Optional[Tuple[datetime, datetime]] = None
    error: Optional[str] = None

    @property
    def attempted_download(self) -> bool:
        return self.status in ("filled", "download_failed")


class GapFiller:
    """Detect archive gaps for a stream station and fall back to file download."""

    def __init__(
        self,
        slot_present: SlotChecker,
        *,
        policy: Optional[GapPolicy] = None,
    ):
        self._present = slot_present
        self.policy = policy or GapPolicy()

    def check_and_fill(
        self,
        station_id: str,
        day: date,
        *,
        downloader: Downloader,
        now: Optional[datetime] = None,
    ) -> GapFillResult:
        """Find gaps on ``day``; if they exceed the policy, download the gap span."""
        up_to = None
        if now is not None:
            up_to = now - timedelta(hours=self.policy.recent_grace_hours)
        missing = find_missing_hours(station_id, day, self._present, up_to=up_to)
        result = GapFillResult(station_id, missing_hours=missing)

        if len(missing) < self.policy.min_missing_to_fill:
            result.status = "below_threshold" if missing else "complete"
            return result

        start, end = min(missing), max(missing)
        logger.info(
            "%s: %d missing hours on %s — downloading %s..%s",
            station_id,
            len(missing),
            day,
            start,
            end,
        )
        try:
            downloader(station_id, start, end)
        except Exception as e:  # noqa: BLE001 - report download failure as a result
            logger.error("Gap-fill download failed for %s: %s", station_id, e)
            result.status = "download_failed"
            result.error = str(e)
            return result
        result.status = "filled"
        result.downloaded_span = (start, end)
        return result
