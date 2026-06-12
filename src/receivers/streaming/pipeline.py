"""Stream-capture pipeline orchestrator.

Composes the five stream-capture stages into one per-cycle run:

    supervise BNC daemons → ingest new hourly RINEX → downsample 1Hz→15s →
    detect gaps & fall back to file download

The orchestrator only sequences the stages and computes archive paths; every stage
and external seam (file_tracking recorder, downloader, tool runners) is injected via
the already-built module instances, so it is unit-testable with mocked stages. The
"composition root" that wires the real seams from config lives in
``receivers.scheduling.stream_scheduler``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from .downsample import DownsampleResult, RinexDownsampler
from .gap import Downloader, GapFiller, GapFillResult
from .ingest import IngestResult, StreamIngestor
from .supervisor import StreamSupervisor, SuperviseResult

logger = logging.getLogger(__name__)


def _month(dt: date) -> str:
    return dt.strftime("%b").lower()


def daily_15s_target(archive_base: Path | str, station_id: str, day: date) -> Path:
    """Archive path for a station's decimated daily (15s) RINEX file.

    Daily RINEX uses hour code ``0``: ``<STA><DOY>0.<yy>D.Z``.
    """
    doy = day.timetuple().tm_yday
    yy = day.year % 100
    name = f"{station_id}{doy:03d}0.{yy:02d}D.Z"
    return (
        Path(archive_base)
        / f"{day.year}"
        / _month(day)
        / station_id
        / "15s_24hr"
        / "rinex"
        / name
    )


def hourly_1hz_sources(archive_base: Path | str, station_id: str, day: date) -> List[Path]:
    """The ingested hourly 1Hz RINEX files for a station+day, sorted."""
    doy = day.timetuple().tm_yday
    yy = day.year % 100
    rinex_dir = (
        Path(archive_base)
        / f"{day.year}"
        / _month(day)
        / station_id
        / "1Hz_1hr"
        / "rinex"
    )
    if not rinex_dir.is_dir():
        return []
    # <STA><DOY>[a-x].<yy>*  (either hour-letter case)
    return sorted(rinex_dir.glob(f"{station_id}{doy:03d}[a-xA-X].{yy:02d}*"))


@dataclass
class StationCycleResult:
    station_id: str
    ingest: IngestResult
    downsample: Optional[DownsampleResult] = None
    gap: Optional[GapFillResult] = None


class StreamPipeline:
    """Sequence the stream-capture stages for a set of stations."""

    def __init__(
        self,
        *,
        supervisor: StreamSupervisor,
        ingestor: StreamIngestor,
        downsampler: RinexDownsampler,
        gap_filler: GapFiller,
        downloader: Downloader,
        rt_base: Path | str,
        archive_base: Path | str,
        workdir: Path | str,
    ):
        self.supervisor = supervisor
        self.ingestor = ingestor
        self.downsampler = downsampler
        self.gap_filler = gap_filler
        self.downloader = downloader
        self.rt_base = Path(rt_base)
        self.archive_base = Path(archive_base)
        self.workdir = Path(workdir)

    def supervise(self) -> SuperviseResult:
        """Ensure BNC daemons are alive for all configured stream stations."""
        return self.supervisor.supervise()

    def process_station(
        self, station_id: str, day: date, *, now: Optional[datetime] = None
    ) -> StationCycleResult:
        """Ingest → downsample → gap-fill one station for one day."""
        ingest = self.ingestor.ingest_dir(
            station_id, self.rt_base / station_id, now=now
        )
        target = daily_15s_target(self.archive_base, station_id, day)
        sources = hourly_1hz_sources(self.archive_base, station_id, day)
        downsample = self.downsampler.downsample_day(
            station_id, sources, target, self.workdir / station_id
        )
        gap = self.gap_filler.check_and_fill(
            station_id, day, downloader=self.downloader, now=now
        )
        return StationCycleResult(station_id, ingest, downsample, gap)

    def run(
        self, stations: List[str], day: date, *, now: Optional[datetime] = None
    ) -> List[StationCycleResult]:
        """Run a full supervise + per-station cycle."""
        self.supervise()
        results = []
        for station_id in stations:
            try:
                results.append(self.process_station(station_id, day, now=now))
            except Exception as e:  # noqa: BLE001 - isolate per-station failures
                logger.error("Stream pipeline failed for %s: %s", station_id, e)
        return results
