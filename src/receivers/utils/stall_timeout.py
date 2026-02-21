"""Per-station stall timeout resolver and download performance recorder.

Provides:
    - get_stall_timeout(): Resolve effective stall timeout for a station
    - record_download(): Log every download attempt to download_log table

Timeout priority:
    1. DB stations.stall_timeout_override (per-station)
    2. receivers.cfg [receiver_type] stall_timeout
    3. receivers.cfg [receiver_defaults] stall_timeout
    4. Hardcoded fallback (300s)
"""

import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Module-level cache for DB overrides: {station_id: timeout_seconds}
_override_cache: Optional[Dict[str, int]] = None
_cache_loaded_at: float = 0.0
_CACHE_TTL = 300.0  # Reload every 5 minutes


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
    global _override_cache, _cache_loaded_at
    _override_cache = None
    _cache_loaded_at = 0.0


def get_stall_timeout(
    station_id: str, receiver_type: str, default: int = 300
) -> int:
    """Resolve the effective stall timeout for a station.

    Priority:
        1. DB stations.stall_timeout_override (per-station)
        2. receivers.cfg [receiver_type] stall_timeout
        3. receivers.cfg [receiver_defaults] stall_timeout
        4. ``default`` argument (fallback)

    Args:
        station_id: Station identifier (e.g. 'LAHC').
        receiver_type: Receiver type key (e.g. 'netr9', 'polarx5').
        default: Hardcoded fallback if nothing else is configured.

    Returns:
        Timeout in seconds.
    """
    station_id = station_id.upper()

    # 1. Per-station DB override
    overrides = _get_overrides()
    if station_id in overrides:
        timeout = overrides[station_id]
        logger.debug(
            "Station %s: using DB stall_timeout_override = %ds", station_id, timeout
        )
        return timeout

    # 2-3. receivers.cfg: [receiver_type] stall_timeout -> [receiver_defaults] stall_timeout
    try:
        from ..config.receivers_config import get_receivers_config

        cfg = get_receivers_config()
        receiver_cfg = cfg.get_receiver_config(receiver_type)
        timeout = receiver_cfg.get("stall_timeout", default)
        if isinstance(timeout, (int, float)):
            return int(timeout)
    except Exception as e:
        logger.debug("Could not read stall_timeout from receivers.cfg: %s", e)

    # 4. Hardcoded fallback
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
