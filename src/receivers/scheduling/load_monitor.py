"""System load monitoring for dynamic job throttling.

Monitors CPU load, network throughput, and active thread count to prevent
the scheduler from overwhelming the system. Jobs check the load monitor
before starting — if the system is overloaded, lower-priority jobs are
skipped (APScheduler will retry on the next trigger).

Priority-based thresholds:
- REALTIME (health monitoring): always proceeds
- STANDARD (live downloads): needs load < 80% of thresholds
- BACKFILL (gap filling): needs load < 60% of thresholds

No new dependencies — uses os.getloadavg(), /proc/net/dev, threading.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .task_interface import TaskPriority

logger = logging.getLogger(__name__)


@dataclass
class SystemLoad:
    """Snapshot of current system load."""

    cpu_load_1m: float = 0.0
    cpu_load_5m: float = 0.0
    cpu_load_15m: float = 0.0
    active_threads: int = 0
    network_bytes_sec: float = 0.0
    timestamp: float = 0.0


class LoadMonitor:
    """Monitor system load and gate job execution based on priority.

    Usage::

        monitor = LoadMonitor(config)
        if monitor.can_start_job(TaskPriority.STANDARD):
            # proceed with job
        else:
            logger.info("System overloaded, skipping")
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self.enabled = cfg.get("enabled", True)
        self.max_cpu_load = cfg.get("max_cpu_load", 8.0)
        self.max_network_mbps = cfg.get("max_network_mbps", 80)
        self.max_active_jobs = cfg.get("max_active_jobs", 80)

        # Priority thresholds: fraction of max allowed for each priority level
        thresholds = cfg.get("priority_thresholds", {})
        self._priority_thresholds = {
            TaskPriority.REALTIME: thresholds.get("realtime", 1.0),
            TaskPriority.STANDARD: thresholds.get("standard", 0.8),
            TaskPriority.BACKFILL: thresholds.get("backfill", 0.6),
            TaskPriority.MAINTENANCE: thresholds.get("maintenance", 0.4),
        }

        # Network sampling state
        self._last_net_bytes: Optional[int] = None
        self._last_net_time: Optional[float] = None

        # Cached load (avoid sampling on every call)
        self._cached_load: Optional[SystemLoad] = None
        self._cache_ttl = cfg.get("check_interval", 10)

    def get_load(self) -> SystemLoad:
        """Sample current system load.

        Results are cached for ``check_interval`` seconds to avoid
        excessive sampling when many jobs check simultaneously.
        """
        now = time.time()
        if (
            self._cached_load is not None
            and (now - self._cached_load.timestamp) < self._cache_ttl
        ):
            return self._cached_load

        load = SystemLoad(timestamp=now)

        # CPU load average
        try:
            avg = os.getloadavg()
            load.cpu_load_1m = avg[0]
            load.cpu_load_5m = avg[1]
            load.cpu_load_15m = avg[2]
        except (OSError, AttributeError):
            pass  # Not available on all platforms

        # Active threads
        load.active_threads = threading.active_count()

        # Network throughput (Linux only, via /proc/net/dev)
        load.network_bytes_sec = self._sample_network()

        self._cached_load = load
        return load

    def can_start_job(self, priority: TaskPriority = TaskPriority.STANDARD) -> bool:
        """Check whether a job of the given priority may start.

        REALTIME jobs always proceed. Lower priorities are gated by the
        fraction of max thresholds allowed for that priority level.
        """
        if not self.enabled:
            return True

        # REALTIME always runs
        threshold = self._priority_thresholds.get(priority, 0.8)
        if threshold >= 1.0:
            return True

        load = self.get_load()

        # Check CPU
        if self.max_cpu_load > 0 and load.cpu_load_1m > self.max_cpu_load * threshold:
            logger.debug(
                "Load gate: CPU %.1f > %.1f (%.0f%% of max %.1f) — blocking %s jobs",
                load.cpu_load_1m,
                self.max_cpu_load * threshold,
                threshold * 100,
                self.max_cpu_load,
                priority.name,
            )
            return False

        # Check active threads/jobs
        if (
            self.max_active_jobs > 0
            and load.active_threads > self.max_active_jobs * threshold
        ):
            logger.debug(
                "Load gate: %d threads > %d (%.0f%% of max %d) — blocking %s jobs",
                load.active_threads,
                int(self.max_active_jobs * threshold),
                threshold * 100,
                self.max_active_jobs,
                priority.name,
            )
            return False

        # Check network (if we have a valid sample)
        if self.max_network_mbps > 0 and load.network_bytes_sec > 0:
            network_mbps = (load.network_bytes_sec * 8) / (1024 * 1024)
            if network_mbps > self.max_network_mbps * threshold:
                logger.debug(
                    "Load gate: network %.1f Mbps > %.1f (%.0f%% of max %d) — "
                    "blocking %s jobs",
                    network_mbps,
                    self.max_network_mbps * threshold,
                    threshold * 100,
                    self.max_network_mbps,
                    priority.name,
                )
                return False

        return True

    def get_status(self) -> Dict[str, Any]:
        """Return a summary dict for CLI / logging."""
        load = self.get_load()
        network_mbps = (
            (load.network_bytes_sec * 8) / (1024 * 1024)
            if load.network_bytes_sec > 0
            else 0.0
        )

        return {
            "enabled": self.enabled,
            "cpu_load_1m": round(load.cpu_load_1m, 2),
            "cpu_load_5m": round(load.cpu_load_5m, 2),
            "active_threads": load.active_threads,
            "network_mbps": round(network_mbps, 2),
            "thresholds": {
                "max_cpu_load": self.max_cpu_load,
                "max_network_mbps": self.max_network_mbps,
                "max_active_jobs": self.max_active_jobs,
            },
            "can_start": {p.name: self.can_start_job(p) for p in TaskPriority},
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _sample_network(self) -> float:
        """Read /proc/net/dev and compute bytes/sec delta.

        Returns 0.0 if not on Linux or if this is the first sample.
        """
        proc_path = Path("/proc/net/dev")
        if not proc_path.exists():
            return 0.0

        try:
            total_bytes = 0
            lines = proc_path.read_text().splitlines()
            for line in lines[2:]:  # Skip header lines
                parts = line.split()
                if len(parts) < 10:
                    continue
                iface = parts[0].rstrip(":")
                if iface == "lo":
                    continue  # Skip loopback
                rx_bytes = int(parts[1])
                tx_bytes = int(parts[9])
                total_bytes += rx_bytes + tx_bytes

            now = time.time()
            if self._last_net_bytes is not None and self._last_net_time is not None:
                dt = now - self._last_net_time
                if dt > 0:
                    bytes_sec = (total_bytes - self._last_net_bytes) / dt
                else:
                    bytes_sec = 0.0
            else:
                bytes_sec = 0.0

            self._last_net_bytes = total_bytes
            self._last_net_time = now
            return max(0.0, bytes_sec)

        except Exception:
            return 0.0
