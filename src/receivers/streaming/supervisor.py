"""Supervise per-station BNC daemons (RTCM3 → RINEX stream capture).

Ports the legacy ``rtcm2rinex.sh`` watchdog: BNC runs as one persistent process
per stream station; this supervisor compares the set of configured stations
(``rtcm2rinex-<SID>.bnc`` files) against the BNC processes currently running and
(re)starts any that are missing. Intended to be driven periodically by the
scheduler.

Process listing and spawning are injectable so the supervision logic is fully
unit-testable without a live BNC binary.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .bnc_config import bnc_config_filename

logger = logging.getLogger(__name__)

#: Station id embedded in a BNC config path/cmdline, e.g. ``rtcm2rinex-GONH.bnc``.
_STATION_RE = re.compile(r"rtcm2rinex-([0-9A-Za-z]+)\.bnc")

ProcessLister = Callable[[], Sequence[str]]
Spawner = Callable[[Sequence[str]], None]


def _default_process_lister() -> List[str]:
    """Return command-line strings of running processes (best-effort)."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "args"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover - env
        logger.warning("Could not list processes: %s", e)
        return []


def _default_spawner(cmd: Sequence[str]) -> None:
    """Launch ``cmd`` as a detached daemon (nohup-equivalent)."""
    subprocess.Popen(  # noqa: S603 - cmd is built from trusted config paths
        list(cmd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


@dataclass
class SuperviseResult:
    """Outcome of a single supervision pass."""

    configured: List[str] = field(default_factory=list)
    running: List[str] = field(default_factory=list)
    started: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)

    @property
    def all_running(self) -> bool:
        return not self.started and not self.failed


class StreamSupervisor:
    """Keep one BNC daemon alive per configured stream station."""

    def __init__(
        self,
        bnc_path: str | Path,
        config_dir: str | Path,
        *,
        process_lister: Optional[ProcessLister] = None,
        spawner: Optional[Spawner] = None,
    ):
        self.bnc_path = Path(bnc_path)
        self.config_dir = Path(config_dir)
        self._list_cmdlines: ProcessLister = process_lister or _default_process_lister
        self._spawn: Spawner = spawner or _default_spawner

    def config_path(self, station_id: str) -> Path:
        """Path to the BNC config file for a station."""
        return self.config_dir / bnc_config_filename(station_id)

    def configured_stations(self) -> List[str]:
        """Stations with a ``rtcm2rinex-<SID>.bnc`` config present, sorted."""
        if not self.config_dir.is_dir():
            return []
        ids = set()
        for path in self.config_dir.glob("rtcm2rinex-*.bnc"):
            m = _STATION_RE.search(path.name)
            if m:
                ids.add(m.group(1))
        return sorted(ids)

    def running_stations(self) -> List[str]:
        """Stations whose BNC daemon is currently running, sorted."""
        ids = set()
        for cmdline in self._list_cmdlines():
            m = _STATION_RE.search(cmdline)
            if m:
                ids.add(m.group(1))
        return sorted(ids)

    def start_station(self, station_id: str) -> bool:
        """Start the BNC daemon for one station. Returns True on launch."""
        cfg = self.config_path(station_id)
        if not cfg.exists():
            logger.warning(
                "No BNC config for %s at %s — cannot start stream", station_id, cfg
            )
            return False
        cmd = [str(self.bnc_path), "--conf", str(cfg), "-nw"]
        try:
            self._spawn(cmd)
        except (OSError, subprocess.SubprocessError) as e:
            logger.error("Failed to start BNC for %s: %s", station_id, e)
            return False
        logger.info("Started BNC stream capture for %s", station_id)
        return True

    def supervise(self) -> SuperviseResult:
        """Start any configured station whose BNC daemon is not running."""
        configured = self.configured_stations()
        running = set(self.running_stations())
        result = SuperviseResult(configured=configured, running=sorted(running))
        for station_id in configured:
            if station_id in running:
                continue
            if self.start_station(station_id):
                result.started.append(station_id)
            else:
                result.failed.append(station_id)
        if result.started or result.failed:
            logger.info(
                "Stream supervise: %d configured, %d running, started %s%s",
                len(configured),
                len(running),
                result.started or "[]",
                f", FAILED {result.failed}" if result.failed else "",
            )
        return result
