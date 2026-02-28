"""Parallel download orchestrator with time-staggered batching and retry.

Splits stations into groups of N and submits them to a shared
ThreadPoolExecutor with staggered timing.  Group 2 starts group_delay
seconds after group 1 is *submitted* (not finished), so slow 3G stations
in earlier groups never block later groups.

Thread safety:
- Each worker creates its own receiver instance (independent FTP/HTTP connections)
- Temp directories are partitioned by station_id/session
- Python logging is thread-safe
- Config reads create fresh parser instances
"""

from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Per-station wall-clock timeout (seconds).  This is the absolute ceiling
# for a single station download including all retries.  If a thread hangs
# (zombie FTP socket, kernel-level recv block) this ensures the parallel
# orchestrator still makes progress.  The daemon thread is abandoned and
# cleaned up when the process exits.
_STATION_WALL_TIMEOUT = 2400  # 40 minutes

logger = logging.getLogger("receivers.parallel")


class RouterFailureCache:
    """Thread-safe cache of recently-failed router IPs.

    When a router is unreachable, all stations behind it will also fail.
    This cache lets subsequent stations skip the connectivity check
    for 5 minutes after the first failure.
    """

    _TTL = 300.0  # 5 minutes

    def __init__(self) -> None:
        self._failed: dict[str, float] = {}  # {router_ip: monotonic_timestamp}
        self._lock = threading.Lock()

    def mark_failed(self, router_ip: str) -> None:
        """Record a router as unreachable."""
        with self._lock:
            self._failed[router_ip] = time.monotonic()

    def is_failed(self, router_ip: str) -> bool:
        """Check if a router was recently marked as failed."""
        with self._lock:
            ts = self._failed.get(router_ip)
            if ts is None:
                return False
            if (time.monotonic() - ts) > self._TTL:
                del self._failed[router_ip]
                return False
            return True


# Module-level router cache shared across all parallel downloads
_router_cache = RouterFailureCache()


@dataclass
class StationResult:
    """Result of downloading data for a single station."""

    station_id: str
    status: str  # completed, up_to_date, unreachable, failed, skipped
    files_downloaded: int = 0
    duration: float = 0.0
    attempt: int = 1  # 1=first, 2=retry
    error_message: str | None = None

    def to_dict(self) -> dict:
        """Serialize to dict for JSON output."""
        d: dict = {
            "station_id": self.station_id,
            "status": self.status,
            "files_downloaded": self.files_downloaded,
            "duration": round(self.duration, 2),
            "attempt": self.attempt,
        }
        if self.error_message:
            d["error_message"] = self.error_message
        return d


@dataclass
class ParallelSummary:
    """Summary of a parallel download run."""

    total_stations: int = 0
    successful: int = 0
    unreachable: int = 0
    failed: int = 0
    skipped: int = 0
    total_files: int = 0
    total_duration: float = 0.0
    retried: int = 0
    retry_recovered: int = 0
    results: dict[str, StationResult] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize to dict for JSON output (experiment runner)."""
        return {
            "total_stations": self.total_stations,
            "successful": self.successful,
            "unreachable": self.unreachable,
            "failed": self.failed,
            "skipped": self.skipped,
            "total_files": self.total_files,
            "total_duration": round(self.total_duration, 2),
            "retried": self.retried,
            "retry_recovered": self.retry_recovered,
            "stations": {
                sid: r.to_dict() for sid, r in self.results.items()
            },
        }


def _record_parallel_outcome(
    station_id: str,
    args: Any,
    outcome: str,
    duration: float,
    attempt: int,
    message: str,
) -> None:
    """Record a download outcome to download_log (fire-and-forget)."""
    try:
        from ..utils.stall_timeout import record_download

        session_type = getattr(args, "session", None) or "15s_24hr"
        record_download(
            station_id,
            session_type,
            outcome,
            duration_seconds=duration,
            attempt=attempt,
            message=message,
        )
    except Exception:
        pass  # Fire-and-forget — DB issues must not crash parallel downloads


def _check_health_ping_online(station_id: str) -> bool | None:
    """Check health monitor's debounced ping status for a station.

    Uses station_connectivity.is_online from the health monitor (updated
    every 5 min, requires 2 consecutive failures to mark offline).
    Returns None if no recent data is available.
    """
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT is_online FROM station_connectivity
                    WHERE sid = %s AND last_check > NOW() - INTERVAL '10 minutes'
                    """,
                    (station_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


def _split_into_groups(items: list, group_size: int) -> list[list]:
    """Split a list into groups of at most group_size."""
    return [items[i : i + group_size] for i in range(0, len(items), group_size)]


def _download_one_station(
    station_id: str,
    args: Any,
    start_time: datetime,
    end_time: datetime,
    ffrequency: str,
    afrequency: str,
    reverse_chronological: bool,
    attempt: int = 1,
) -> StationResult:
    """Download data for a single station (runs in a thread worker).

    Creates its own receiver instance and logger for thread safety.
    Returns a StationResult — no shared mutable state.
    """
    station_id = station_id.upper()
    t0 = time.monotonic()

    # Lazy import to avoid circular dependencies
    from .main import _validate_station_for_download, _download_station_period

    worker_logger = logging.getLogger(f"receivers.parallel.{station_id}")

    # Validate station config (no network, fast)
    receiver = _validate_station_for_download(
        station_id, worker_logger, session=args.session
    )
    if receiver is None:
        _record_parallel_outcome(
            station_id, args, "failed", time.monotonic() - t0,
            attempt, "Station validation failed (config/session)",
        )
        return StationResult(
            station_id=station_id,
            status="skipped",
            duration=time.monotonic() - t0,
            attempt=attempt,
            error_message="Station validation failed (config/session)",
        )

    # --- Pre-download gates (skip known-bad stations quickly) ---
    session_type = getattr(args, "session", None)

    # Health gate: skip stations with hardware/config issues
    try:
        from ..utils.stall_timeout import check_station_health_gate

        health_skip = check_station_health_gate(station_id, session_type)
        if health_skip:
            msg = f"Health gate: {health_skip}"
            worker_logger.info(f"⏭️  {station_id}: {msg}")
            _record_parallel_outcome(
                station_id, args, "failed", time.monotonic() - t0,
                attempt, msg,
            )
            return StationResult(
                station_id=station_id,
                status="skipped",
                duration=time.monotonic() - t0,
                attempt=attempt,
                error_message=msg,
            )
    except Exception:
        pass  # Health gate is advisory — failures must not block downloads

    # Consecutive failure backoff: skip stations that keep failing
    # But override if health monitor or a quick ping shows the station is online
    # TODO: TANC shares battery with repeater → repeater dies first → station
    # flaps online/offline. Need "flapping station" detection that doesn't
    # penalize power-related connectivity issues.
    try:
        from ..utils.stall_timeout import should_skip_station, clear_backoff_cache

        if should_skip_station(station_id):
            # Prefer health monitor's debounced ping (2-consecutive-failure
            # threshold, updated every 5 min) over a single fresh ping
            online = _check_health_ping_online(station_id)
            if online is None:
                online = receiver._quick_ping()  # Fallback if no health data
            if not online:
                msg = "Consecutive failure backoff (last 5 attempts failed, still offline)"
                worker_logger.info(f"⏭️  {station_id}: {msg}")
                _record_parallel_outcome(
                    station_id, args, "failed", time.monotonic() - t0,
                    attempt, msg,
                )
                return StationResult(
                    station_id=station_id,
                    status="skipped",
                    duration=time.monotonic() - t0,
                    attempt=attempt,
                    error_message=msg,
                )
            else:
                source = "health monitor" if online is True else "ping"
                worker_logger.info(
                    f"🔄 {station_id}: Backoff overridden — station online ({source})"
                )
                clear_backoff_cache(station_id)
    except Exception:
        pass  # Backoff is advisory

    # Router group skip: if router was recently unreachable, skip all stations behind it
    router_ip = receiver.station_info.get("router", {}).get("ip")
    if router_ip and _router_cache.is_failed(router_ip):
        msg = f"Router {router_ip} group skip (recently unreachable)"
        worker_logger.info(f"⏭️  {station_id}: {msg}")
        _record_parallel_outcome(
            station_id, args, "unreachable", time.monotonic() - t0,
            attempt, msg,
        )
        return StationResult(
            station_id=station_id,
            status="unreachable",
            duration=time.monotonic() - t0,
            attempt=attempt,
            error_message=msg,
        )

    # Quick connectivity check before attempting download
    if not receiver._quick_ping():
        # Mark this router as failed so other stations behind it skip quickly
        if router_ip:
            _router_cache.mark_failed(router_ip)
        _record_parallel_outcome(
            station_id, args, "unreachable", time.monotonic() - t0,
            attempt, "Ping check failed",
        )
        return StationResult(
            station_id=station_id,
            status="unreachable",
            duration=time.monotonic() - t0,
            attempt=attempt,
            error_message="Ping check failed",
        )

    # Attempt download with a wall-clock timeout.
    # We run the download in a daemon thread so that if it hangs on a zombie
    # FTP socket we can abandon it after _STATION_WALL_TIMEOUT seconds.
    result_container: list[tuple[int, int, int] | Exception] = []

    def _do_download() -> None:
        try:
            result_container.append(
                _download_station_period(
                    receiver,
                    station_id,
                    start_time,
                    end_time,
                    args,
                    worker_logger,
                    audit_logger=None,
                    ffrequency=ffrequency,
                    afrequency=afrequency,
                    reverse_chronological=reverse_chronological,
                )
            )
        except Exception as exc:
            result_container.append(exc)

    dl_thread = threading.Thread(target=_do_download, daemon=True)
    dl_thread.start()
    dl_thread.join(timeout=_STATION_WALL_TIMEOUT)

    if dl_thread.is_alive():
        # Thread hung — abandon it (daemon=True means it dies with the process)
        duration = time.monotonic() - t0
        msg = f"Station download timed out after {_STATION_WALL_TIMEOUT}s (zombie connection)"
        worker_logger.error(f"⏰ {station_id}: {msg}")
        _record_parallel_outcome(
            station_id, args, "stall_timeout", duration, attempt, msg,
        )
        return StationResult(
            station_id=station_id,
            status="failed",
            duration=duration,
            attempt=attempt,
            error_message=msg,
        )

    # Thread completed — check result
    if not result_container:
        duration = time.monotonic() - t0
        msg = "Download returned no result"
        _record_parallel_outcome(
            station_id, args, "failed", duration, attempt, msg,
        )
        return StationResult(
            station_id=station_id,
            status="failed",
            duration=duration,
            attempt=attempt,
            error_message=msg,
        )

    result = result_container[0]
    if isinstance(result, Exception):
        duration = time.monotonic() - t0
        msg = f"{type(result).__name__}: {result}"
        _record_parallel_outcome(
            station_id, args, "failed", duration, attempt, msg,
        )
        return StationResult(
            station_id=station_id,
            status="failed",
            duration=duration,
            attempt=attempt,
            error_message=msg,
        )

    files_downloaded, errors, files_checked = result
    duration = time.monotonic() - t0

    if errors > 0:
        msg = f"{errors} error(s) during download"
        _record_parallel_outcome(
            station_id, args, "failed", duration, attempt, msg,
        )
        return StationResult(
            station_id=station_id,
            status="failed",
            files_downloaded=files_downloaded,
            duration=duration,
            attempt=attempt,
            error_message=msg,
        )

    status = "completed" if files_downloaded > 0 else "up_to_date"
    _record_parallel_outcome(
        station_id, args, status, duration, attempt,
        f"{files_downloaded} file(s), {files_checked} checked",
    )
    return StationResult(
        station_id=station_id,
        status=status,
        files_downloaded=files_downloaded,
        duration=duration,
        attempt=attempt,
    )


def _get_session_defaults(session_type: str) -> dict[str, Any]:
    """Read batches and distribution_window from scheduler.yaml for a session.

    Returns dict with 'batches' and 'distribution_window' keys, or empty dict
    if scheduler config is unavailable.
    """
    try:
        from ..scheduling.config_loader import load_scheduler_config
        config = load_scheduler_config()
        session_cfg = config.get("sessions", {}).get(session_type, {})
        return {
            "batches": session_cfg.get("batches"),
            "distribution_window": session_cfg.get("distribution_window"),
        }
    except Exception:
        return {}


def download_parallel(
    stations: list[str],
    args: Any,
    logger: logging.Logger,
    start_time: datetime,
    end_time: datetime,
    ffrequency: str,
    afrequency: str,
    reverse_chronological: bool,
    _audit_logger: Any = None,
) -> ParallelSummary:
    """Download data for multiple stations in parallel with grouped batching.

    Parameters are resolved in priority order:
    1. CLI flags (--batches, --distribution-window)
    2. scheduler.yaml session config (batches, distribution_window)
    3. Hardcoded defaults (10 batches, 10 minutes)

    From batches + distribution_window, we derive:
    - group_size = ceil(stations / batches)
    - group_delay = distribution_window * 60 / batches

    Algorithm:
    1. Split stations into groups of group_size
    2. Submit each group to a shared ThreadPoolExecutor with group_delay
       stagger between submissions (groups run concurrently — no blocking)
    3. Wait for all futures to complete
    4. Collect unreachable/failed stations
    5. Wait retry_delay seconds, retry them (same staggered approach)
    6. Return ParallelSummary

    Args:
        stations: List of station IDs
        args: Parsed CLI arguments (needs session, sync, archive, etc.)
        logger: Logger instance
        start_time: Download start time
        end_time: Download end time
        ffrequency: File frequency (e.g., "1D", "1H")
        afrequency: Acquisition frequency (e.g., "15s", "1Hz")
        reverse_chronological: Whether to download newest first
        _audit_logger: Audit logger (unused in parallel mode)

    Returns:
        ParallelSummary with results for all stations
    """
    # Resolve parameters: CLI > scheduler.yaml > defaults
    # Use explicit None checks — 0 is a valid value (no delay).
    session_defaults = _get_session_defaults(getattr(args, "session", "15s_24hr"))

    cli_batches = getattr(args, "batches", None)
    cli_window = getattr(args, "distribution_window", None)

    if cli_batches is not None:
        batches = cli_batches
    elif session_defaults.get("batches") is not None:
        batches = session_defaults["batches"]
    else:
        batches = 2

    if cli_window is not None:
        distribution_window = cli_window
    elif session_defaults.get("distribution_window") is not None:
        distribution_window = session_defaults["distribution_window"]
    else:
        distribution_window = 10

    retry_delay = getattr(args, "retry_delay", 90.0)

    # Normalize station IDs
    stations = [s.upper() for s in stations]
    summary = ParallelSummary(total_stations=len(stations))

    # Derive group_size and group_delay from batches + window
    group_size = math.ceil(len(stations) / batches)
    group_delay = (distribution_window * 60) / batches

    logger.info(
        f"Parallel download: {len(stations)} stations, "
        f"{batches} batches of ~{group_size}, "
        f"{distribution_window}min window ({group_delay:.0f}s between groups)"
    )

    # Split into groups
    groups = _split_into_groups(stations, group_size)
    logger.info(f"Split into {len(groups)} groups")

    t0 = time.monotonic()

    # First pass: process all groups
    results = _process_groups(
        groups, args, logger, start_time, end_time,
        ffrequency, afrequency, reverse_chronological,
        group_delay, attempt=1,
    )
    summary.results.update(results)

    # Collect unreachable and failed stations for retry.
    # Transient failures (FTP timeout, busy receiver, packet loss) often
    # succeed on a second attempt once the initial batch pressure subsides.
    retryable = [
        sid for sid, r in results.items()
        if r.status in ("unreachable", "failed")
    ]

    if retryable:
        summary.retried = len(retryable)
        logger.info(
            f"{len(retryable)} stations to retry "
            f"(unreachable/failed), waiting {retry_delay}s: "
            f"{' '.join(retryable)}"
        )
        time.sleep(retry_delay)

        retry_groups = _split_into_groups(retryable, group_size)
        retry_results = _process_groups(
            retry_groups, args, logger, start_time, end_time,
            ffrequency, afrequency, reverse_chronological,
            group_delay, attempt=2,
        )

        # Update results with retry outcomes
        for sid, result in retry_results.items():
            if result.status in ("completed", "up_to_date"):
                summary.retry_recovered += 1
            summary.results[sid] = result

    # Calculate summary totals
    summary.total_duration = time.monotonic() - t0
    for result in summary.results.values():
        if result.status in ("completed", "up_to_date"):
            summary.successful += 1
            summary.total_files += result.files_downloaded
        elif result.status == "unreachable":
            summary.unreachable += 1
        elif result.status == "skipped":
            summary.skipped += 1
        else:
            summary.failed += 1

    # Print summary
    _print_summary(summary, logger)

    # Emit structured JSON for experiment runner
    if getattr(args, "json_log", False):
        import json

        print(f"EXPERIMENT_RESULT:{json.dumps(summary.to_dict())}")

    return summary


def _process_groups(
    groups: list[list[str]],
    args: Any,
    logger: logging.Logger,
    start_time: datetime,
    end_time: datetime,
    ffrequency: str,
    afrequency: str,
    reverse_chronological: bool,
    group_delay: float,
    attempt: int,
) -> dict[str, StationResult]:
    """Process groups with time-staggered starts — groups don't wait for each other.

    All groups share a single ThreadPoolExecutor.  Each group is submitted
    after a ``group_delay`` sleep, but we never wait for the previous group
    to finish first.  This means slow stations (e.g. 3G links) in group 1
    do not block groups 2, 3, …  After every group has been submitted we
    wait for *all* futures to complete and collect results.
    """
    total_workers = sum(len(g) for g in groups)
    all_futures: dict[Future[StationResult], str] = {}
    results: dict[str, StationResult] = {}

    with ThreadPoolExecutor(max_workers=total_workers) as executor:
        # Submit groups with staggered timing
        for i, group in enumerate(groups):
            group_label = f"[{i + 1}/{len(groups)}]"
            logger.info(
                f"Group {group_label}: submitting {len(group)} stations — "
                f"{' '.join(group)}"
            )

            for sid in group:
                future = executor.submit(
                    _download_one_station,
                    sid,
                    args,
                    start_time,
                    end_time,
                    ffrequency,
                    afrequency,
                    reverse_chronological,
                    attempt,
                )
                all_futures[future] = sid

            # Stagger delay before next group (skip after last group)
            if i < len(groups) - 1:
                logger.info(
                    f"Stagger delay: {group_delay:.0f}s before next group"
                )
                time.sleep(group_delay)

        # Collect ALL results (groups run concurrently)
        for future in as_completed(all_futures):
            sid = all_futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = StationResult(
                    station_id=sid,
                    status="failed",
                    attempt=attempt,
                    error_message=f"Thread error: {e}",
                )
            results[sid] = result

            # Log individual result
            status_icon = {
                "completed": "OK",
                "up_to_date": "OK (up to date)",
                "unreachable": "UNREACHABLE",
                "failed": "FAILED",
                "skipped": "SKIPPED",
            }.get(result.status, result.status)

            msg = f"  {result.station_id}: {status_icon}"
            if result.files_downloaded > 0:
                msg += f" ({result.files_downloaded} files)"
            msg += f" [{result.duration:.1f}s]"
            if result.error_message:
                msg += f" — {result.error_message}"
            logger.info(msg)

    return results


def _print_summary(summary: ParallelSummary, logger: logging.Logger) -> None:
    """Print parallel download summary."""
    logger.info("=" * 60)
    logger.info("Parallel download summary:")
    logger.info(f"  Total stations: {summary.total_stations}")
    logger.info(
        f"  Successful: {summary.successful} "
        f"({summary.total_files} files)"
    )
    if summary.unreachable > 0:
        logger.info(f"  Unreachable: {summary.unreachable}")
    if summary.failed > 0:
        logger.info(f"  Failed: {summary.failed}")
    if summary.skipped > 0:
        logger.info(f"  Skipped: {summary.skipped}")
    if summary.retried > 0:
        logger.info(
            f"  Retried: {summary.retried}, "
            f"recovered: {summary.retry_recovered}"
        )
    logger.info(f"  Duration: {summary.total_duration:.1f}s")
    logger.info("=" * 60)
