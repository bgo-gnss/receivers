"""Ingest BNC stream-capture RINEX into the data archive + file_tracking.

Port of the legacy ``sort-lmi-1Hz.sh``: BNC writes hourly 1 Hz RINEX obs files
(``SSSSDDDH.YYO``) into ``~/tmp/RT-rinex/<SID>/``. This module moves each *completed*
hourly file (the in-progress current hour is skipped) into the archive at
``<base>/<YYYY>/<mon>/<SID>/1Hz_1hr/rinex/`` in the fleet's Hatanaka-compressed
form (``.YYD.Z``), and records it via an injectable file-tracking callback so stream
stations show up in the dashboards like file-download stations.

External commands (RNX2CRX, compress) go through one injectable ``runner``; the
file-tracking integration is an injectable ``tracker`` seam. Both make the
scan/parse/skip/place logic fully unit-testable without tools or a database.

NOTE: the real file_tracking recorder is wired in the scheduler-integration step;
exact tool flags / .Z format need validation on rek-d01 (see RinexDownsampler).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .downsample import _swap_obs_to_hatanaka

logger = logging.getLogger(__name__)

Runner = Callable[[Sequence[str], Optional[Path]], int]
#: tracker(station_id, archive_path, file_date, session_type) -> None
Tracker = Callable[[str, Path, datetime, str], None]

# RINEX2 short obs name: SSSS DDD H .YY O   (H = hour letter a-x, or 0 for daily)
_RINEX2_OBS_RE = re.compile(
    r"^(?P<sta>[A-Z0-9]{4})(?P<doy>\d{3})(?P<hour>[a-xA-X0])\.(?P<yy>\d{2})[Oo]$"
)


def _default_runner(cmd: Sequence[str], stdout_path: Optional[Path] = None) -> int:
    if stdout_path is not None:
        with open(stdout_path, "wb") as fh:
            return subprocess.run(list(cmd), stdout=fh, timeout=300).returncode
    return subprocess.run(list(cmd), timeout=300).returncode


def _hour_to_int(hour_char: str) -> int:
    """RINEX hour letter → 0-23 ('a'/'A'=0). '0' (daily) → 0."""
    if hour_char in ("0",):
        return 0
    return ord(hour_char.lower()) - ord("a")


@dataclass(frozen=True)
class BncRinexFile:
    """A parsed BNC hourly RINEX obs filename."""

    station: str
    doy: int
    hour: int
    year: int
    name: str

    @property
    def datetime(self) -> datetime:
        d = datetime.strptime(f"{self.year} {self.doy}", "%Y %j").replace(
            tzinfo=UTC
        )
        return d.replace(hour=self.hour)

    @property
    def hatanaka_name(self) -> str:
        """Archive filename: ``...O`` → ``...D.Z``."""
        return _swap_obs_to_hatanaka(self.name) + ".Z"

    def archive_path(self, base: Path | str, session_type: str = "1Hz_1hr") -> Path:
        dt = self.datetime
        return (
            Path(base)
            / f"{dt.year}"
            / dt.strftime("%b").lower()
            / self.station
            / session_type
            / "rinex"
            / self.hatanaka_name
        )


def parse_bnc_rinex_name(name: str) -> Optional[BncRinexFile]:
    """Parse a BNC RINEX2 hourly obs filename, or None if it doesn't match."""
    m = _RINEX2_OBS_RE.match(name)
    if not m:
        return None
    yy = int(m.group("yy"))
    year = 2000 + yy if yy < 80 else 1900 + yy
    return BncRinexFile(
        station=m.group("sta"),
        doy=int(m.group("doy")),
        hour=_hour_to_int(m.group("hour")),
        year=year,
        name=name,
    )


@dataclass
class IngestResult:
    station_id: str
    ingested: List[str] = field(default_factory=list)
    skipped_current: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)


class StreamIngestor:
    """Move completed BNC hourly RINEX into the archive + record in file_tracking."""

    def __init__(
        self,
        *,
        archive_base: str | Path,
        session_type: str = "1Hz_1hr",
        rnx2crx: str = "RNX2CRX",
        compressor: Optional[Sequence[str]] = None,
        runner: Optional[Runner] = None,
        tracker: Optional[Tracker] = None,
    ):
        self.archive_base = Path(archive_base)
        self.session_type = session_type
        self.rnx2crx = rnx2crx
        self.compressor = list(compressor) if compressor else ["gzip", "-c"]
        self._run: Runner = runner or _default_runner
        self._track: Optional[Tracker] = tracker

    def ingest_dir(
        self, station_id: str, rt_dir: str | Path, *, now: Optional[datetime] = None
    ) -> IngestResult:
        """Ingest all completed hourly obs files in a station's RT-rinex dir."""
        rt_dir = Path(rt_dir)
        now = now or datetime.now(UTC)
        cur = (now.year, now.timetuple().tm_yday, now.hour)
        result = IngestResult(station_id)
        if not rt_dir.is_dir():
            return result

        for path in sorted(rt_dir.glob("*.??[Oo]")):
            parsed = parse_bnc_rinex_name(path.name)
            if parsed is None:
                continue
            if (parsed.year, parsed.doy, parsed.hour) == cur:
                result.skipped_current.append(path.name)  # still being written
                continue
            try:
                self._ingest_one(parsed, path)
                result.ingested.append(path.name)
            except Exception as e:  # noqa: BLE001 - record per-file failure
                logger.error("Ingest failed for %s: %s", path.name, e)
                result.failed.append(path.name)

        if result.ingested or result.failed:
            logger.info(
                "Ingest %s: %d ingested, %d skipped (current), %d failed",
                station_id,
                len(result.ingested),
                len(result.skipped_current),
                len(result.failed),
            )
        return result

    def _ingest_one(self, parsed: BncRinexFile, obs_path: Path) -> None:
        compressed = self._hatanaka_compress(obs_path)
        dest = parsed.archive_path(self.archive_base, self.session_type)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(compressed), str(dest))
        if self._track is not None:
            self._track(parsed.station, dest, parsed.datetime, self.session_type)

    def _hatanaka_compress(self, obs_path: Path) -> Path:
        """RNX2CRX the obs file then compress → ``.??d.Z`` (in the same dir)."""
        if self._run([self.rnx2crx, "-f", str(obs_path)], None) != 0:
            raise RuntimeError(f"RNX2CRX failed: {obs_path}")
        hatanaka = obs_path.with_name(_swap_obs_to_hatanaka(obs_path.name))
        compressed = obs_path.with_name(hatanaka.name + ".Z")
        if self._run([*self.compressor, str(hatanaka)], compressed) != 0:
            raise RuntimeError(f"compress failed: {hatanaka}")
        return compressed
