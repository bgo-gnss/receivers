"""Parse flexible schedule formats into APScheduler triggers.

Supports multiple schedule formats:
- "HH:MM" → Daily at specific time (e.g., "00:10")
- ["HH:MM", "HH:MM", ...] → Multiple times per day (e.g., ["00:10", "08:10", "16:10"])
- ":MM" → Every hour at minute MM (e.g., ":15")
- "Nh" → Every N hours (e.g., "6h", "12h")
- "Nm" → Every N minutes (e.g., "45m", "30m")
- "cron: * * * * *" → Raw cron expression
- Legacy: {schedule_minute: 10, frequency: "daily"} → Backward compatible
"""

import re
from typing import Dict, Any, Union, List, Tuple
from dataclasses import dataclass


@dataclass
class ScheduleTrigger:
    """Parsed schedule configuration for APScheduler."""
    trigger_type: str  # 'cron' or 'interval'
    trigger_kwargs: Dict[str, Any]  # Arguments for add_job()
    description: str  # Human-readable description


def parse_schedule(schedule: Union[str, List[str], Dict[str, Any]]) -> ScheduleTrigger:
    """Parse flexible schedule format into APScheduler trigger.

    Args:
        schedule: Schedule specification in various formats

    Returns:
        ScheduleTrigger with trigger_type and kwargs for APScheduler

    Examples:
        >>> parse_schedule("00:10")
        ScheduleTrigger(trigger_type='cron', trigger_kwargs={'hour': 0, 'minute': 10}, ...)

        >>> parse_schedule([" 00:10", "08:10", "16:10"])
        ScheduleTrigger(trigger_type='cron', trigger_kwargs={'hour': '0,8,16', 'minute': 10}, ...)

        >>> parse_schedule(":15")
        ScheduleTrigger(trigger_type='cron', trigger_kwargs={'minute': 15}, ...)

        >>> parse_schedule("6h")
        ScheduleTrigger(trigger_type='interval', trigger_kwargs={'hours': 6}, ...)

        >>> parse_schedule("45m")
        ScheduleTrigger(trigger_type='interval', trigger_kwargs={'minutes': 45}, ...)

        >>> parse_schedule("cron: */6 * * * *")
        ScheduleTrigger(trigger_type='cron', trigger_kwargs={'minute': '*/6'}, ...)
    """
    # Handle legacy dict format {schedule_minute: X, frequency: "daily/hourly"}
    if isinstance(schedule, dict):
        return _parse_legacy_format(schedule)

    # Handle list of times (multiple times per day)
    if isinstance(schedule, list):
        return _parse_time_list(schedule)

    # Handle string formats
    if isinstance(schedule, str):
        schedule = schedule.strip()

        # Raw cron expression
        if schedule.startswith("cron:"):
            return _parse_cron_expression(schedule[5:].strip())

        # Interval: "6h", "45m"
        if re.match(r'^\d+[hm]$', schedule):
            return _parse_interval(schedule)

        # Every hour at minute: ":15"
        if schedule.startswith(":"):
            return _parse_hourly_minute(schedule)

        # Single time: "00:10", "08:30"
        if re.match(r'^\d{1,2}:\d{2}$', schedule):
            return _parse_single_time(schedule)

    raise ValueError(f"Invalid schedule format: {schedule}")


def _parse_legacy_format(config: Dict[str, Any]) -> ScheduleTrigger:
    """Parse legacy {schedule_minute, frequency} format."""
    minute = config.get('schedule_minute', 0)
    frequency = config.get('frequency', 'daily')

    if frequency == 'daily':
        return ScheduleTrigger(
            trigger_type='cron',
            trigger_kwargs={'hour': 0, 'minute': minute},
            description=f"Daily at 00:{minute:02d}"
        )
    elif frequency == 'hourly':
        return ScheduleTrigger(
            trigger_type='cron',
            trigger_kwargs={'minute': minute},
            description=f"Hourly at :{minute:02d}"
        )
    else:
        raise ValueError(f"Invalid frequency: {frequency}")


def _parse_time_list(times: List[str]) -> ScheduleTrigger:
    """Parse list of times into multiple-times-per-day schedule.

    Args:
        times: List of "HH:MM" strings

    Returns:
        Cron trigger with comma-separated hours
    """
    hours = []
    minutes = set()

    for time_str in times:
        time_str = time_str.strip()
        match = re.match(r'^(\d{1,2}):(\d{2})$', time_str)
        if not match:
            raise ValueError(f"Invalid time format: {time_str} (expected HH:MM)")

        hour = int(match.group(1))
        minute = int(match.group(2))

        if not (0 <= hour <= 23):
            raise ValueError(f"Invalid hour: {hour} (must be 0-23)")
        if not (0 <= minute <= 59):
            raise ValueError(f"Invalid minute: {minute} (must be 0-59)")

        hours.append(hour)
        minutes.add(minute)

    if len(minutes) > 1:
        raise ValueError(f"All times must have same minute: {times}")

    minute = minutes.pop()
    hour_str = ','.join(str(h) for h in sorted(hours))

    return ScheduleTrigger(
        trigger_type='cron',
        trigger_kwargs={'hour': hour_str, 'minute': minute},
        description=f"{len(hours)} times daily at {hour_str}:{minute:02d}"
    )


def _parse_single_time(time_str: str) -> ScheduleTrigger:
    """Parse single time "HH:MM" into daily schedule."""
    match = re.match(r'^(\d{1,2}):(\d{2})$', time_str)
    if not match:
        raise ValueError(f"Invalid time format: {time_str}")

    hour = int(match.group(1))
    minute = int(match.group(2))

    if not (0 <= hour <= 23):
        raise ValueError(f"Invalid hour: {hour}")
    if not (0 <= minute <= 59):
        raise ValueError(f"Invalid minute: {minute}")

    return ScheduleTrigger(
        trigger_type='cron',
        trigger_kwargs={'hour': hour, 'minute': minute},
        description=f"Daily at {hour:02d}:{minute:02d}"
    )


def _parse_hourly_minute(minute_str: str) -> ScheduleTrigger:
    """Parse ":MM" into every-hour-at-minute schedule."""
    minute_str = minute_str.strip()
    if not minute_str.startswith(':'):
        raise ValueError(f"Invalid format: {minute_str}")

    minute = int(minute_str[1:])
    if not (0 <= minute <= 59):
        raise ValueError(f"Invalid minute: {minute}")

    return ScheduleTrigger(
        trigger_type='cron',
        trigger_kwargs={'minute': minute},
        description=f"Hourly at :{minute:02d}"
    )


def _parse_interval(interval_str: str) -> ScheduleTrigger:
    """Parse "Nh" or "Nm" into interval schedule."""
    match = re.match(r'^(\d+)([hm])$', interval_str)
    if not match:
        raise ValueError(f"Invalid interval format: {interval_str}")

    value = int(match.group(1))
    unit = match.group(2)

    if unit == 'h':
        return ScheduleTrigger(
            trigger_type='interval',
            trigger_kwargs={'hours': value},
            description=f"Every {value} hour{'s' if value > 1 else ''}"
        )
    else:  # 'm'
        return ScheduleTrigger(
            trigger_type='interval',
            trigger_kwargs={'minutes': value},
            description=f"Every {value} minute{'s' if value > 1 else ''}"
        )


def _parse_cron_expression(cron_str: str) -> ScheduleTrigger:
    """Parse raw cron expression "minute hour day month day_of_week".

    APScheduler uses named parameters instead of positional cron fields.
    """
    parts = cron_str.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"Invalid cron expression: {cron_str} "
            "(expected: minute hour day month day_of_week)"
        )

    minute, hour, day, month, day_of_week = parts
    kwargs = {}

    if minute != '*':
        kwargs['minute'] = minute
    if hour != '*':
        kwargs['hour'] = hour
    if day != '*':
        kwargs['day'] = day
    if month != '*':
        kwargs['month'] = month
    if day_of_week != '*':
        kwargs['day_of_week'] = day_of_week

    return ScheduleTrigger(
        trigger_type='cron',
        trigger_kwargs=kwargs,
        description=f"Cron: {cron_str}"
    )


def apply_distribution_window(
    trigger: ScheduleTrigger,
    station_index: int,
    total_stations: int,
    window_minutes: int
) -> Tuple[str, Dict[str, Any]]:
    """Apply distribution window to spread stations across time.

    Args:
        trigger: Base schedule trigger
        station_index: Index of this station (0-based)
        total_stations: Total number of stations
        window_minutes: Distribution window in minutes

    Returns:
        Tuple of (trigger_type, modified_kwargs)

    Note:
        For interval triggers, distribution window is not applied.
    """
    if trigger.trigger_type != 'cron':
        # Interval triggers don't support distribution
        return trigger.trigger_type, trigger.trigger_kwargs.copy()

    # Calculate minute offset for this station
    if total_stations <= 1 or window_minutes <= 0:
        # No distribution: single station or window_minutes=0 (run all at once)
        minute_offset = 0
    else:
        stations_per_minute = total_stations / window_minutes
        minute_offset = int(station_index / stations_per_minute)

    # Apply offset to minute field
    kwargs = trigger.trigger_kwargs.copy()
    base_minute = kwargs.get('minute', 0)

    # Handle different minute formats
    if isinstance(base_minute, int):
        kwargs['minute'] = (base_minute + minute_offset) % 60
    elif isinstance(base_minute, str):
        # For complex minute expressions (*/5, 0,30), don't apply offset
        # This is a limitation - user should avoid distribution with complex cron
        pass

    return trigger.trigger_type, kwargs
