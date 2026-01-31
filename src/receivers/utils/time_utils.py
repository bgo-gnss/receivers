"""Time range calculation utilities for download operations.

This module provides single source of truth for time range calculations used by both
CLI and scheduler. Implements correct "previous complete period" logic for time-series
data processing.

TODO: Contribute this logic to gtimes package
    - This is general-purpose time-series logic, not GPS-specific
    - gtimes.datepathlist has a bug where it doesn't respect lfrequency for hourly data
      (hardcodes delta=timedelta(days=1) at line 496)
    - Consider adding gtimes.generate_time_range() and gtimes.generate_datetime_list()
    - Would benefit any time-series data processing, not just GPS receivers
    - See: gtimes/src/gtimes/timefunc.py:326-541 (datepathlist function)

Example:
    >>> # Calculate time range for last 24 hours
    >>> start, end = calculate_download_time_range('1Hz_1hr', lookback_periods=24)
    >>> # end is previous complete hour, start is 24 hours before that

    >>> # Generate datetime list in reverse chronological order (newest first)
    >>> datetimes = generate_download_datetimes('1Hz_1hr', 24, reverse_chronological=True)
"""

from datetime import datetime, timedelta, timezone
from typing import List, Tuple


def calculate_download_time_range(
    session_type: str,
    lookback_periods: int
) -> Tuple[datetime, datetime]:
    """Calculate download time range for a session type.

    Implements correct "previous complete period" logic:
    - For daily sessions: end at start of current day (00:00:00 today)
    - For hourly sessions: end at previous complete hour (not current incomplete hour)

    This ensures we only download complete data files that exist on the receiver.

    Args:
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr)
        lookback_periods: Number of periods to look back
            - For daily sessions: number of days
            - For hourly sessions: number of hours

    Returns:
        Tuple of (start_time, end_time) where:
            - start_time: Beginning of time range
            - end_time: End of time range (exclusive)

    Example:
        >>> # At 2025-10-09 22:41:00 UTC
        >>> start, end = calculate_download_time_range('1Hz_1hr', lookback_periods=24)
        >>> # start: 2025-10-08 22:00:00 (24 hours before current hour)
        >>> # end:   2025-10-09 22:00:00 (current hour start, excludes incomplete 22:00-23:00)

        >>> # Daily session
        >>> start, end = calculate_download_time_range('15s_24hr', lookback_periods=7)
        >>> # start: 2025-10-02 00:00:00 (7 days before today)
        >>> # end:   2025-10-09 00:00:00 (start of current day)
    """
    now = datetime.now(timezone.utc)

    if session_type == '15s_24hr':
        # Daily data - end at start of current day (00:00:00)
        # This gives us complete data for yesterday and before
        end_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_time = end_time - timedelta(days=lookback_periods)
    else:
        # Hourly data - end at START of CURRENT hour (excludes current incomplete hour)
        # At 20:55, current hour is 20:00-21:00 (file being written, named 2000)
        # Previous complete hour is 19:00-20:00 (complete, file named 1900)
        # end_time = 20:00 means range [start, 20:00) includes files up to 1900
        # With -D 2 at 20:55: download hours 18 and 19 (both complete)
        current_hour_start = now.replace(minute=0, second=0, microsecond=0)
        end_time = current_hour_start  # Current hour start (exclusive, so previous hour is last included)
        start_time = end_time - timedelta(hours=lookback_periods)  # Back from end_time

    return start_time, end_time


def generate_download_datetimes(
    session_type: str,
    lookback_periods: int,
    reverse_chronological: bool = False
) -> List[datetime]:
    """Generate list of datetimes to download in specified order.

    Creates a list of datetime objects representing each period to download.
    Useful for iteration over time periods in download operations.

    Args:
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr)
        lookback_periods: Number of periods to look back
        reverse_chronological: If True, return newest-first order (for CLI -D flag)
            - True: Most recent file downloaded first (fail-fast on connection issues)
            - False: Oldest file downloaded first (chronological backfill)

    Returns:
        List of datetime objects, one per period

    Example:
        >>> # Get last 3 hours, newest first (for CLI -D flag)
        >>> datetimes = generate_download_datetimes('1Hz_1hr', 3, reverse_chronological=True)
        >>> # [2025-10-09 21:00:00, 2025-10-09 20:00:00, 2025-10-09 19:00:00]

        >>> # Get last 7 days, oldest first (for scheduler backfill)
        >>> datetimes = generate_download_datetimes('15s_24hr', 7, reverse_chronological=False)
        >>> # [2025-10-02 00:00:00, 2025-10-03 00:00:00, ..., 2025-10-08 00:00:00]
    """
    start_time, end_time = calculate_download_time_range(session_type, lookback_periods)

    # Determine frequency
    if session_type == '15s_24hr':
        freq = timedelta(days=1)
    else:
        freq = timedelta(hours=1)

    # Generate datetime list
    datetimes = []
    current = start_time
    while current < end_time:  # Fixed: end_time is exclusive (don't include today)
        datetimes.append(current)
        current += freq

    # Reverse if newest-first requested (CLI -D flag behavior)
    if reverse_chronological:
        datetimes.reverse()

    return datetimes


def get_session_frequency(session_type: str) -> str:
    """Get frequency string for a session type.

    Returns the pandas/gtimes-compatible frequency string for a session type.

    Args:
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr)

    Returns:
        Frequency string ('1D' for daily, '1H' for hourly)

    Example:
        >>> get_session_frequency('15s_24hr')
        '1D'
        >>> get_session_frequency('1Hz_1hr')
        '1H'
    """
    if session_type == '15s_24hr':
        return '1D'
    else:
        return '1H'


def generate_period_ranges(
    start: datetime,
    end: datetime,
    session_type: str,
    reverse: bool = False,
) -> List[Tuple[datetime, datetime]]:
    """Generate (period_start, period_end) pairs for single-period iteration.

    Used by network-first download ordering: iterate over periods (days/hours)
    and process all stations for each period before moving to the next.

    Args:
        start: Start of the overall time range.
        end: End of the overall time range (exclusive).
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr).
        reverse: If True, return newest-first order (network-first mode).

    Returns:
        List of (period_start, period_end) tuples, one per period.

    Example:
        >>> from datetime import datetime
        >>> ranges = generate_period_ranges(
        ...     datetime(2026, 1, 28), datetime(2026, 1, 31),
        ...     '15s_24hr', reverse=True
        ... )
        >>> # [(2026-01-30, 2026-01-31), (2026-01-29, 2026-01-30), (2026-01-28, 2026-01-29)]
    """
    if session_type == '15s_24hr':
        freq = timedelta(days=1)
    else:
        freq = timedelta(hours=1)

    periods: List[Tuple[datetime, datetime]] = []
    current = start
    while current < end:
        period_end = min(current + freq, end)
        periods.append((current, period_end))
        current += freq

    if reverse:
        periods.reverse()

    return periods
