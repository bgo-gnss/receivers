"""Background system metrics sampler for download experiments.

Collects CPU load, network throughput, TCP connection count, and memory
usage at regular intervals during a trial.  Runs in a daemon thread so
it stops automatically when the main process exits.

Reuses the /proc/net/dev parsing pattern from
``receivers.scheduling.load_monitor.LoadMonitor``.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Sample:
    """Single point-in-time system snapshot."""

    elapsed_seconds: float = 0.0
    cpu_load_1m: float = 0.0
    cpu_load_5m: float = 0.0
    network_bytes_sec: float = 0.0
    open_tcp_connections: int = 0
    memory_rss_mb: float = 0.0


class SystemSampler:
    """Background thread that periodically records system metrics.

    Usage::

        sampler = SystemSampler(interval=2.0)
        sampler.start()
        # ... run experiment ...
        sampler.stop()
        for s in sampler.samples:
            print(s.elapsed_seconds, s.network_bytes_sec)
    """

    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self.samples: list[Sample] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_time: float = 0.0

        # Network delta tracking
        self._prev_net_bytes: Optional[int] = None
        self._prev_net_time: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin sampling in a daemon thread."""
        self._stop_event.clear()
        self._start_time = time.monotonic()
        self._prev_net_bytes = None
        self._prev_net_time = None
        with self._lock:
            self.samples.clear()

        self._thread = threading.Thread(
            target=self._run, name="system-sampler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the sampler to stop and wait for it to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)
            self._thread = None

    # ------------------------------------------------------------------
    # Sampling loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Sampling loop — runs in background thread."""
        while not self._stop_event.is_set():
            sample = self._take_sample()
            with self._lock:
                self.samples.append(sample)
            self._stop_event.wait(self.interval)

    def _take_sample(self) -> Sample:
        elapsed = time.monotonic() - self._start_time
        s = Sample(elapsed_seconds=round(elapsed, 2))

        # CPU load
        try:
            avg = os.getloadavg()
            s.cpu_load_1m = avg[0]
            s.cpu_load_5m = avg[1]
        except (OSError, AttributeError):
            pass

        # Network throughput
        s.network_bytes_sec = self._read_network_throughput()

        # Open TCP connections
        s.open_tcp_connections = self._count_tcp_connections()

        # Memory RSS
        s.memory_rss_mb = self._read_memory_rss()

        return s

    # ------------------------------------------------------------------
    # /proc readers
    # ------------------------------------------------------------------

    def _read_network_throughput(self) -> float:
        """Read /proc/net/dev and compute bytes/sec delta."""
        proc_path = Path("/proc/net/dev")
        try:
            total_bytes = 0
            lines = proc_path.read_text().splitlines()
            for line in lines[2:]:  # skip header
                parts = line.split()
                if len(parts) < 10:
                    continue
                iface = parts[0].rstrip(":")
                if iface == "lo":
                    continue
                total_bytes += int(parts[1]) + int(parts[9])  # rx + tx

            now = time.monotonic()
            if self._prev_net_bytes is not None and self._prev_net_time is not None:
                dt = now - self._prev_net_time
                bps = (total_bytes - self._prev_net_bytes) / dt if dt > 0 else 0.0
            else:
                bps = 0.0

            self._prev_net_bytes = total_bytes
            self._prev_net_time = now
            return max(0.0, bps)
        except Exception:
            return 0.0

    @staticmethod
    def _count_tcp_connections() -> int:
        """Count established TCP connections from /proc/net/tcp."""
        count = 0
        for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            try:
                lines = path.read_text().splitlines()
                for line in lines[1:]:  # skip header
                    parts = line.split()
                    if len(parts) >= 4:
                        # Column 3 (st) == 01 means ESTABLISHED
                        if parts[3] == "01":
                            count += 1
            except Exception:
                pass
        return count

    @staticmethod
    def _read_memory_rss() -> float:
        """Read current process RSS from /proc/self/status (MB)."""
        status_path = Path("/proc/self/status")
        try:
            for line in status_path.read_text().splitlines():
                if line.startswith("VmRSS:"):
                    # Format: "VmRSS:    12345 kB"
                    parts = line.split()
                    return int(parts[1]) / 1024.0  # kB → MB
        except Exception:
            pass
        return 0.0

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dicts(self) -> list[dict]:
        """Return all samples as a list of dicts (for DB insertion)."""
        with self._lock:
            snapshot = list(self.samples)
        return [
            {
                "elapsed_seconds": s.elapsed_seconds,
                "cpu_load_1m": round(s.cpu_load_1m, 2),
                "cpu_load_5m": round(s.cpu_load_5m, 2),
                "network_mbps": round(
                    (s.network_bytes_sec * 8) / (1024 * 1024), 3
                ),
                "open_connections": s.open_tcp_connections,
                "memory_rss_mb": round(s.memory_rss_mb, 1),
            }
            for s in snapshot
        ]
