"""Time range calculation utilities for download operations.

Thin adapters over ``gtimes.timefunc`` that translate receivers-specific
session vocabulary (``'15s_24hr'`` → daily, everything else → hourly) into
generic ``period`` timedeltas. The actual time math (previous-complete-period
alignment, datetime list generation, period iteration) lives in gtimes where
other projects in the ecosystem can reuse it.

See ``gtimes.timefunc.generate_time_range`` for the canonical implementation.
"""

from datetime import datetime, timedelta
from typing import List, Tuple

from gtimes.timefunc import (
    generate_datetime_list as _gt_generate_datetime_list,
)
from gtimes.timefunc import (
    generate_period_ranges as _gt_generate_period_ranges,
)
from gtimes.timefunc import (
    generate_time_range as _gt_generate_time_range,
)


def _session_period(session_type: str) -> timedelta:
    """Map a receivers session_type to a generic period timedelta."""
    if session_type == "15s_24hr":
        return timedelta(days=1)
    return timedelta(hours=1)


def calculate_download_time_range(
    session_type: str,
    lookback_periods: int,
) -> Tuple[datetime, datetime]:
    """Calculate download time range for a session type.

    Ends at the start of the most recently completed period (so an in-progress
    file on the receiver is not pulled mid-write). See
    ``gtimes.timefunc.generate_time_range`` for the generic version.

    Args:
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr).
        lookback_periods: Number of complete periods to include.

    Returns:
        ``(start, end)`` — ``end`` is exclusive.
    """
    return _gt_generate_time_range(_session_period(session_type), lookback_periods)


def generate_download_datetimes(
    session_type: str,
    lookback_periods: int,
    reverse_chronological: bool = False,
) -> List[datetime]:
    """Generate list of datetimes to download.

    Args:
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr).
        lookback_periods: Number of periods to include.
        reverse_chronological: If True, newest-first (CLI -D behaviour).
    """
    period = _session_period(session_type)
    start, end = _gt_generate_time_range(period, lookback_periods)
    return _gt_generate_datetime_list(start, end, period, reverse=reverse_chronological)


def get_session_frequency(session_type: str) -> str:
    """Get pandas/gtimes-style frequency string for a session type."""
    if session_type == "15s_24hr":
        return "1D"
    return "1H"


def generate_period_ranges(
    start: datetime,
    end: datetime,
    session_type: str,
    reverse: bool = False,
) -> List[Tuple[datetime, datetime]]:
    """Generate ``(period_start, period_end)`` pairs for iteration.

    Used by network-first download ordering: iterate over periods and process
    all stations for each period before moving on.
    """
    return _gt_generate_period_ranges(
        start, end, _session_period(session_type), reverse=reverse
    )
