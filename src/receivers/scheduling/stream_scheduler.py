"""Composition root + scheduler job functions for stream capture.

Wires the dependency-injected ``receivers.streaming`` modules to the real seams
(config-driven paths/tools, the file_tracking recorder, and the file downloader)
and exposes module-level job functions for APScheduler (must be importable /
picklable for the SQLite jobstore).

Two jobs:
  * ``_run_stream_supervise_job`` — keep BNC daemons alive (frequent).
  * ``_run_stream_pipeline_job``  — ingest → downsample → gap-fill (hourly).

Registration lives in ``bulk_scheduler._schedule_stream_capture`` and is gated
behind ``stream_capture.enabled`` (default off) so it cannot disturb a running
scheduler until explicitly enabled and BNC is deployed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from ..streaming import (
    GapFiller,
    RinexDownsampler,
    StreamIngestor,
    StreamPipeline,
    StreamSupervisor,
    get_acquisition_mode,
    make_archive_slot_checker,
)
from ..streaming.config import AcquisitionMode
from ..streaming.gap import GapPolicy

logger = logging.getLogger("receivers.scheduler.stream")


def enumerate_stream_stations(station_configs: Dict[str, Dict[str, Any]]) -> List[str]:
    """Return sorted station ids whose acquisition_mode is ``stream``."""
    return sorted(
        sid
        for sid, cfg in station_configs.items()
        if get_acquisition_mode(cfg) == AcquisitionMode.STREAM
    )


@dataclass
class StreamSettings:
    """Resolved paths/tools for the stream pipeline (from receivers.cfg)."""

    archive_base: str
    rt_base: str
    workdir: str
    bnc_path: str = "bnc"
    bnc_config_dir: str = "~/.config/BKG"
    crx2rnx: str = "CRX2RNX"
    rnx2crx: str = "RNX2CRX"
    teqc: str = "teqc"
    interval: int = 15
    min_missing_to_fill: int = 2
    recent_grace_hours: int = 2

    @staticmethod
    def _expand(p: str) -> str:
        return str(Path(p).expanduser())


def load_stream_settings() -> StreamSettings:
    """Read stream settings from receivers.cfg ``[streaming]`` (with defaults)."""
    from ..config.receivers_config import get_receivers_config

    rc = get_receivers_config()
    archive_base = rc.get_data_prepath()
    workdir = str(Path(rc.get_tmp_dir()) / "stream_downsample")

    def _get(key: str, default: str) -> str:
        try:
            return rc.config.get("streaming", key, fallback=default)  # type: ignore[attr-defined]
        except Exception:
            return default

    return StreamSettings(
        archive_base=archive_base,
        rt_base=StreamSettings._expand(_get("rt_base", "~/tmp/RT-rinex")),
        workdir=workdir,
        bnc_path=StreamSettings._expand(_get("bnc_path", "bnc")),
        bnc_config_dir=StreamSettings._expand(_get("bnc_config_dir", "~/.config/BKG")),
        crx2rnx=_get("crx2rnx", "CRX2RNX"),
        rnx2crx=_get("rnx2crx", "RNX2CRX"),
        teqc=_get("teqc", "teqc"),
        interval=int(_get("interval", "15")),
        min_missing_to_fill=int(_get("min_missing_to_fill", "2")),
        recent_grace_hours=int(_get("recent_grace_hours", "2")),
    )


def _make_file_tracking_recorder(tracker):
    """Adapt a FileTracker into the StreamIngestor tracker seam."""

    def record(station_id: str, archive_path: Path, file_date: datetime, session_type: str):
        size = archive_path.stat().st_size if archive_path.exists() else None
        tracker.mark_file_archived(
            station_id,
            session_type,
            file_date.date(),
            file_hour=file_date.hour,
            filename=archive_path.name,
            file_size=size,
        )

    return record


def _download_gap(station_id: str, start: datetime, end: datetime):
    """Real downloader seam: fetch 1Hz files for [start, end] from the receiver."""
    from ..base.receiver_factory import create_receiver
    from ..config_utils import get_station_config

    cfg = get_station_config(station_id)
    if cfg is None:
        raise RuntimeError(f"no station config for {station_id}")
    receiver = create_receiver(station_id, cfg)
    return receiver.download_data(
        start=start, end=end, session="1Hz_1hr", sync=True, archive=True
    )


def build_stream_pipeline(settings: StreamSettings, *, tracker_recorder, downloader) -> StreamPipeline:
    """Construct a StreamPipeline from settings + real seams."""
    supervisor = StreamSupervisor(settings.bnc_path, settings.bnc_config_dir)
    ingestor = StreamIngestor(
        archive_base=settings.archive_base,
        rnx2crx=settings.rnx2crx,
        tracker=tracker_recorder,
    )
    downsampler = RinexDownsampler(
        interval=settings.interval,
        crx2rnx=settings.crx2rnx,
        rnx2crx=settings.rnx2crx,
        teqc=settings.teqc,
    )
    gap_filler = GapFiller(
        make_archive_slot_checker(settings.archive_base),
        policy=GapPolicy(
            min_missing_to_fill=settings.min_missing_to_fill,
            recent_grace_hours=settings.recent_grace_hours,
        ),
    )
    return StreamPipeline(
        supervisor=supervisor,
        ingestor=ingestor,
        downsampler=downsampler,
        gap_filler=gap_filler,
        downloader=downloader,
        rt_base=settings.rt_base,
        archive_base=settings.archive_base,
        workdir=settings.workdir,
    )


# -- module-level job functions (APScheduler) -------------------------------


def _run_stream_supervise_job() -> None:
    """Ensure BNC daemons are alive for all configured stream stations."""
    settings = load_stream_settings()
    supervisor = StreamSupervisor(settings.bnc_path, settings.bnc_config_dir)
    result = supervisor.supervise()
    if result.started or result.failed:
        logger.info(
            "Stream supervise: started %s, failed %s", result.started, result.failed
        )


def _run_stream_pipeline_job(days_back: int = 1) -> None:
    """Ingest → downsample → gap-fill for all stream stations (today + days_back)."""
    from ..cli.main import get_all_station_configs
    from ..health.file_tracker import FileTracker

    stations = enumerate_stream_stations(get_all_station_configs())
    if not stations:
        logger.debug("No stream stations configured")
        return

    settings = load_stream_settings()
    now = datetime.now(UTC)
    days = [(now - timedelta(days=d)).date() for d in range(days_back + 1)]

    tracker = FileTracker()
    try:
        pipeline = build_stream_pipeline(
            settings,
            tracker_recorder=_make_file_tracking_recorder(tracker),
            downloader=_download_gap,
        )
        pipeline.supervise()
        for day in days:
            for station_id in stations:
                try:
                    pipeline.process_station(station_id, day, now=now)
                except Exception as e:  # noqa: BLE001 - isolate per-station/day
                    logger.error("Stream pipeline %s %s: %s", station_id, day, e)
    finally:
        close = getattr(tracker, "close", None)
        if callable(close):
            close()
