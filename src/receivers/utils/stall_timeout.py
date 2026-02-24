"""Per-station stall timeout resolver and download performance recorder.

Provides:
    - get_stall_timeout(): Resolve effective stall timeout for a station
    - compute_adaptive_timeout(): Data-driven timeout from download_log history
    - record_download(): Log every download attempt to download_log table

Timeout priority:
    1. DB stations.stall_timeout_override (per-station)
    2. Adaptive timeout from download_log history (data-driven)
    3. receivers.cfg [receiver_type] stall_timeout
    4. receivers.cfg [receiver_defaults] stall_timeout
    5. Hardcoded fallback (300s)
"""

import logging
import math
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Module-level cache for DB overrides: {station_id: timeout_seconds}
_override_cache: Optional[Dict[str, int]] = None
_cache_loaded_at: float = 0.0
_CACHE_TTL = 300.0  # Reload every 5 minutes

# Module-level cache for adaptive timeouts: {(station_id, session_type): (timeout, loaded_at)}
_adaptive_cache: Dict[Tuple[str, str], Tuple[int, float]] = {}


def _load_overrides() -> Dict[str, int]:
    """Load per-station stall_timeout_override values from the database.

    Returns:
        Dict mapping station_id -> timeout in seconds.
        Empty dict if DB unavailable.
    """
    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sid, stall_timeout_override FROM stations "
                    "WHERE stall_timeout_override IS NOT NULL"
                )
                return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        logger.debug("Could not load stall_timeout overrides from DB: %s", e)
        return {}


def _get_overrides() -> Dict[str, int]:
    """Get cached overrides, refreshing if stale."""
    global _override_cache, _cache_loaded_at

    now = time.monotonic()
    if _override_cache is None or (now - _cache_loaded_at) > _CACHE_TTL:
        _override_cache = _load_overrides()
        _cache_loaded_at = now
        if _override_cache:
            logger.debug(
                "Loaded %d stall_timeout overrides: %s",
                len(_override_cache),
                ", ".join(f"{k}={v}s" for k, v in _override_cache.items()),
            )

    return _override_cache


def invalidate_cache() -> None:
    """Force reload of overrides on next call to get_stall_timeout()."""
    global _override_cache, _cache_loaded_at, _adaptive_cache
    _override_cache = None
    _cache_loaded_at = 0.0
    _adaptive_cache.clear()


def compute_adaptive_timeout(
    station_id: str,
    session_type: str,
    expected_file_size: Optional[int] = None,
    safety_factor: float = 1.5,
    min_timeout: int = 300,
    max_timeout: int = 1800,
) -> Optional[int]:
    """Compute a data-driven timeout from download_log history.

    Queries the last 7 days of completed downloads for this station+session
    to determine average speed, then calculates how long a full download
    should take with a safety margin.

    Args:
        station_id: Station identifier.
        session_type: Session type (e.g. '15s_24hr', '1Hz_1hr').
        expected_file_size: Expected file size in bytes. If None, uses
            average file_size from history.
        safety_factor: Multiplier applied to estimated transfer time.
        min_timeout: Minimum timeout in seconds.
        max_timeout: Maximum timeout in seconds.

    Returns:
        Adaptive timeout in seconds, or None if insufficient data
        (fewer than 2 completed downloads in last 7 days).
    """
    station_id = station_id.upper()

    # Check cache first
    cache_key = (station_id, session_type)
    now = time.monotonic()
    if cache_key in _adaptive_cache:
        cached_timeout, cached_at = _adaptive_cache[cache_key]
        if (now - cached_at) < _CACHE_TTL:
            return cached_timeout

    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT AVG(avg_speed_bps), AVG(file_size), COUNT(*)
                       FROM download_log
                       WHERE sid = %s
                         AND session_type = %s
                         AND outcome = 'completed'
                         AND avg_speed_bps > 0
                         AND file_size > 0
                         AND ts > NOW() - INTERVAL '7 days'""",
                    (station_id, session_type),
                )
                row = cur.fetchone()

        if row is None or row[2] < 2:
            # Insufficient data
            return None

        avg_speed: float = row[0]
        avg_file_size: float = row[1]
        sample_count: int = row[2]

        file_size = expected_file_size if expected_file_size else avg_file_size
        if avg_speed <= 0:
            return None

        timeout = math.ceil(file_size / avg_speed * safety_factor)
        timeout = max(min_timeout, min(timeout, max_timeout))

        logger.debug(
            "Station %s (%s): adaptive timeout = %ds "
            "(avg_speed=%.0f B/s, file_size=%.0f, samples=%d)",
            station_id, session_type, timeout, avg_speed, file_size, sample_count,
        )

        # Cache the result
        _adaptive_cache[cache_key] = (timeout, now)
        return timeout

    except Exception as e:
        logger.debug("Could not compute adaptive timeout for %s/%s: %s",
                     station_id, session_type, e)
        return None


def get_stall_timeout(
    station_id: str,
    receiver_type: str,
    default: int = 300,
    session_type: Optional[str] = None,
    expected_file_size: Optional[int] = None,
) -> int:
    """Resolve the effective stall timeout for a station.

    Priority:
        1. DB stations.stall_timeout_override (per-station)
        2. Adaptive timeout from download_log history (data-driven)
        3. receivers.cfg [receiver_type] stall_timeout
        4. receivers.cfg [receiver_defaults] stall_timeout
        5. ``default`` argument (fallback)

    Args:
        station_id: Station identifier (e.g. 'LAHC').
        receiver_type: Receiver type key (e.g. 'netr9', 'polarx5').
        default: Hardcoded fallback if nothing else is configured.
        session_type: Session type for adaptive lookup (optional).
        expected_file_size: Expected file size for adaptive calculation (optional).

    Returns:
        Timeout in seconds.
    """
    station_id = station_id.upper()

    # 1. Per-station DB override (highest priority — manual control)
    overrides = _get_overrides()
    if station_id in overrides:
        timeout = overrides[station_id]
        logger.debug(
            "Station %s: using DB stall_timeout_override = %ds", station_id, timeout
        )
        return timeout

    # 2. Adaptive timeout from download_log history
    if session_type:
        adaptive = compute_adaptive_timeout(
            station_id, session_type, expected_file_size
        )
        if adaptive is not None:
            logger.debug(
                "Station %s: using adaptive timeout = %ds", station_id, adaptive
            )
            return adaptive

    # 3-4. receivers.cfg: [receiver_type] stall_timeout -> [receiver_defaults] stall_timeout
    try:
        from ..config.receivers_config import get_receivers_config

        cfg = get_receivers_config()
        receiver_cfg = cfg.get_receiver_config(receiver_type)
        timeout = receiver_cfg.get("stall_timeout", default)
        if isinstance(timeout, (int, float)):
            return int(timeout)
    except Exception as e:
        logger.debug("Could not read stall_timeout from receivers.cfg: %s", e)

    # 5. Hardcoded fallback
    return default


def record_download(
    station_id: str,
    session_type: str,
    outcome: str,
    *,
    file_date: Optional[object] = None,
    filename: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    bytes_downloaded: Optional[int] = None,
    file_size: Optional[int] = None,
    stall_timeout_used: Optional[int] = None,
    attempt: int = 1,
    message: Optional[str] = None,
) -> None:
    """Record a download attempt to the download_log table.

    Fire-and-forget: failures are logged at DEBUG level but never raised.

    Args:
        station_id: Station identifier.
        session_type: Session type (e.g. '15s_24hr').
        outcome: One of 'completed', 'stall_timeout', 'failed', 'unreachable'.
        file_date: Date of the data file (optional).
        filename: Downloaded filename (optional).
        duration_seconds: Wall-clock download time (optional).
        bytes_downloaded: Actual bytes received (optional).
        file_size: Expected/total file size (optional).
        stall_timeout_used: Effective timeout value used (optional).
        attempt: Retry attempt number, 1-based.
        message: Error message or context (optional).
    """
    # Compute avg_speed_bps
    avg_speed_bps: Optional[float] = None
    if (
        bytes_downloaded is not None
        and duration_seconds is not None
        and duration_seconds > 0
    ):
        avg_speed_bps = bytes_downloaded / duration_seconds

    try:
        from ..health.database_factory import DatabaseConnectionFactory

        with DatabaseConnectionFactory.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO download_log
                       (sid, session_type, outcome, file_date, filename,
                        duration_seconds, bytes_downloaded, file_size,
                        avg_speed_bps, stall_timeout_used, attempt, message)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        station_id.upper(),
                        session_type,
                        outcome,
                        file_date,
                        filename,
                        duration_seconds,
                        bytes_downloaded,
                        file_size,
                        avg_speed_bps,
                        stall_timeout_used,
                        attempt,
                        message,
                    ),
                )
    except Exception as e:
        logger.debug("Could not record download to download_log: %s", e)
