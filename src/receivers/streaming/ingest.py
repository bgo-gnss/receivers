"""Ingest BNC stream-capture RINEX into the data archive + file_tracking.

Port of the legacy ``sort-lmi-1Hz.sh``: BNC writes hourly 1 Hz RINEX obs files —
either RINEX 2 short names (``SSSSDDDH.YYO``) or RINEX 3 long names
(``SSSSMRCCC_S_YYYYDDDHHMM_01H_MO.rnx``) — into ``~/tmp/RT-rinex/<SID>/``. This
module moves each *completed* hourly file (the in-progress current hour is skipped)
into the archive at ``<base>/<YYYY>/<mon>/<SID>/1Hz_1hr/rinex/`` under the fleet's
canonical short, lowercase Hatanaka name (``SSSSDDDH.YYd.Z``) — matching the
SBF/sbf2rin product — regardless of the source RINEX version, and records it via an
injectable file-tracking callback so stream stations show up in the dashboards like
file-download stations.

External commands (RNX2CRX, compress) go through one injectable ``runner``; the
file-tracking integration is an injectable ``tracker`` seam. Both make the
scan/parse/skip/place logic fully unit-testable without tools or a database.
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

# RINEX3 long obs name (BNC stream RINEX 3 output), e.g.
#   GONH00ISL_S_20261670700_01H_MO.rnx
# = SSSS MR CCC _ <src> _ YYYYDDDHHMM _ <period> [_ <sample>] _ <typ>O.rnx
_RINEX3_OBS_RE = re.compile(
    r"^(?P<sta>[A-Z0-9]{4})\d{2}[A-Z]{3}_[A-Z]_"
    r"(?P<yyyy>\d{4})(?P<doy>\d{3})(?P<hh>\d{2})\d{2}_"
    r"\d+[A-Z](?:_\d+[A-Z])?_[A-Z]O\.rnx$"
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


def _hour_letter(hour: int) -> str:
    """0 → 'a' … 23 → 'x' (RINEX hour code)."""
    return chr(ord("a") + hour)


@dataclass(frozen=True)
class BncRinexFile:
    """A parsed BNC hourly RINEX obs filename (RINEX2 short or RINEX3 long)."""

    station: str
    doy: int
    hour: int
    year: int
    name: str

    @property
    def datetime(self) -> datetime:
        d = datetime.strptime(f"{self.year} {self.doy}", "%Y %j").replace(tzinfo=UTC)
        return d.replace(hour=self.hour)

    @property
    def short_obs_name(self) -> str:
        """Canonical short obs name (lowercase) — the RNX2CRX input.

        Derived from parsed fields, so a RINEX3 *long* BNC name normalizes to the
        fleet-standard short name carrying the (RINEX3) content.
        """
        return (
            f"{self.station}{self.doy:03d}{_hour_letter(self.hour)}"
            f".{self.year % 100:02d}o"
        )

    @property
    def hatanaka_name(self) -> str:
        """Canonical archive filename: short, lowercase Hatanaka ``SSSSDDDH.YYd.Z``.

        Matches the authoritative SBF/sbf2rin product naming so the stream and
        download 15s products share one convention regardless of RINEX version.
        """
        return (
            f"{self.station}{self.doy:03d}{_hour_letter(self.hour)}"
            f".{self.year % 100:02d}d.Z"
        )

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
    """Parse a BNC hourly obs filename (RINEX2 short or RINEX3 long), else None."""
    m = _RINEX2_OBS_RE.match(name)
    if m:
        yy = int(m.group("yy"))
        year = 2000 + yy if yy < 80 else 1900 + yy
        return BncRinexFile(
            station=m.group("sta"),
            doy=int(m.group("doy")),
            hour=_hour_to_int(m.group("hour")),
            year=year,
            name=name,
        )
    m = _RINEX3_OBS_RE.match(name)
    if m:
        return BncRinexFile(
            station=m.group("sta"),
            doy=int(m.group("doy")),
            hour=int(m.group("hh")),
            year=int(m.group("yyyy")),
            name=name,
        )
    return None


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

        # RINEX2 short obs (*.YYO) and RINEX3 long obs (*.rnx). Nav/other .rnx
        # files don't match parse_bnc_rinex_name and are skipped below.
        candidates = sorted(list(rt_dir.glob("*.??[Oo]")) + list(rt_dir.glob("*.rnx")))

        # Dedupe per hour: while a station is switching RINEX 2 -> 3, BOTH a short
        # .YYO and a long .rnx can exist for the same hour, and both normalize to
        # the same short archive name. Prefer the RINEX 3 (.rnx) so the archive
        # doesn't get silently overwritten with the RINEX 2 version.
        best: dict[tuple[int, int, int], tuple[Path, BncRinexFile]] = {}
        for path in candidates:
            parsed = parse_bnc_rinex_name(path.name)
            if parsed is None:
                continue
            key = (parsed.year, parsed.doy, parsed.hour)
            chosen = best.get(key)
            is_long = path.suffix == ".rnx"
            if chosen is None or (is_long and chosen[0].suffix != ".rnx"):
                best[key] = (path, parsed)

        for key in sorted(best):
            path, parsed = best[key]
            if key == cur:
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
        compressed = self._hatanaka_compress(parsed, obs_path)
        dest = parsed.archive_path(self.archive_base, self.session_type)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(compressed), str(dest))
        if self._track is not None:
            self._track(parsed.station, dest, parsed.datetime, self.session_type)

    def _hatanaka_compress(self, parsed: BncRinexFile, obs_path: Path) -> Path:
        """Normalize to the canonical short obs name, RNX2CRX, compress → ``.YYd.Z``.

        The BNC source may be a RINEX2 short name or a RINEX3 long ``.rnx``; both
        are copied to ``parsed.short_obs_name`` first so RNX2CRX yields the
        fleet-standard short Hatanaka name (lowercase) regardless of input form.
        """
        short_obs = obs_path.with_name(parsed.short_obs_name)
        copied = obs_path.resolve() != short_obs.resolve()
        if copied:
            shutil.copy(str(obs_path), str(short_obs))
        try:
            if self._run([self.rnx2crx, "-f", str(short_obs)], None) != 0:
                raise RuntimeError(f"RNX2CRX failed: {short_obs}")
            hatanaka = short_obs.with_name(_swap_obs_to_hatanaka(short_obs.name))
            compressed = short_obs.with_name(parsed.hatanaka_name)
            if self._run([*self.compressor, str(hatanaka)], compressed) != 0:
                raise RuntimeError(f"compress failed: {hatanaka}")
            hatanaka.unlink(missing_ok=True)
            return compressed
        finally:
            if copied:
                short_obs.unlink(missing_ok=True)
