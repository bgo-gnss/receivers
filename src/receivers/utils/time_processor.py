"""Time parameter processing utilities for receivers package.

This module provides unified time parameter handling extracted from receiver
implementations. It standardizes datetime parsing, validation, and normalization
across all receiver types.

Design for extensibility:
- Support for custom datetime formats via parser registry
- Configurable normalization rules
- Session-aware time range calculation
"""

import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Dict, List, Optional, Protocol, Tuple, Union


class TimestampNormalization(Enum):
    """Timestamp normalization strategies for different file frequencies."""

    MIDNIGHT = "midnight"  # Normalize to midnight (00:00:00)
    HOUR_BOUNDARY = "hour_boundary"  # Normalize to hour boundary (XX:00:00)
    MINUTE_BOUNDARY = "minute_boundary"  # Normalize to minute boundary (XX:XX:00)
    NO_NORMALIZATION = "none"  # Keep original timestamp


class DatetimeParser(Protocol):
    """Protocol for custom datetime parsers.

    Allows registering custom datetime format parsers for future flexibility.
    """

    def parse(self, dt_string: str) -> datetime:
        """Parse datetime string to datetime object.

        Args:
            dt_string: String representation of datetime

        Returns:
            Parsed datetime object

        Raises:
            ValueError: If parsing fails
        """
        ...

    def get_format_description(self) -> str:
        """Get human-readable description of this format."""
        ...


class TimeParameterProcessor:
    """Unified time parameter processing for receivers.

    This class consolidates time handling logic that was duplicated across
    receiver implementations. It provides:
    - Flexible datetime parsing with multiple format support
    - Session-aware timestamp normalization
    - Default time range calculation
    - Extensible parser registry

    Design considerations:
    - Uses strategy pattern for normalization rules
    - Supports plugin-based datetime parsers
    - Configuration-driven defaults
    - Preserves existing CLI behavior exactly
    """

    # Standard datetime format parsers (in priority order)
    _STANDARD_FORMATS = [
        ("%Y-%m-%d %H:%M:%S", "Standard datetime: YYYY-MM-DD HH:MM:SS"),
        ("%Y%m%d-%H%M", "Compact with dash: YYYYMMDD-HHMM"),
        ("%Y-%m-%d", "Date only: YYYY-MM-DD"),
        ("%Y%m%d", "Compact date: YYYYMMDD"),
    ]

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize time parameter processor.

        Args:
            logger: Optional logger instance
        """
        self.logger = logger or logging.getLogger(__name__)

        # Custom parser registry for extensibility
        self._custom_parsers: List[DatetimeParser] = []

        # Normalization strategy mapping
        self._normalization_strategies: Dict[str, TimestampNormalization] = {
            "24hr": TimestampNormalization.MIDNIGHT,
            "1hr": TimestampNormalization.HOUR_BOUNDARY,
            # Future: '15min': TimestampNormalization.MINUTE_BOUNDARY,
        }

    def register_parser(self, parser: DatetimeParser) -> None:
        """Register custom datetime parser.

        Custom parsers are tried before standard format parsers, allowing
        support for domain-specific datetime formats.

        Args:
            parser: Custom datetime parser instance
        """
        self._custom_parsers.append(parser)
        self.logger.debug(
            f"Registered custom parser: {parser.get_format_description()}"
        )

    def register_normalization_strategy(
        self, ffrequency: str, strategy: TimestampNormalization
    ) -> None:
        """Register custom normalization strategy for a file frequency.

        Args:
            ffrequency: File frequency identifier (e.g., '24hr', '1hr', '15min')
            strategy: Normalization strategy to use
        """
        self._normalization_strategies[ffrequency] = strategy
        self.logger.debug(
            f"Registered normalization strategy for {ffrequency}: {strategy.value}"
        )

    def parse_datetime_flexible(self, dt_input: Union[datetime, str]) -> datetime:
        """Parse datetime from various formats.

        Delegates to ``gtimes.timefunc.parse_datetime_flexible`` for the
        common cases (ISO, YYYYMMDD, YYYYMMDD-HHMM, etc.). If the caller has
        registered domain-specific parsers via :meth:`register_parser`, they
        are tried first.

        Args:
            dt_input: Datetime object or string representation.

        Returns:
            Parsed datetime object.

        Raises:
            ValueError: If no parser succeeds.
        """
        if isinstance(dt_input, datetime):
            return dt_input

        for parser in self._custom_parsers:
            try:
                result = parser.parse(dt_input)
                self.logger.debug(
                    f"Parsed '{dt_input}' using custom parser: "
                    f"{parser.get_format_description()}"
                )
                return result
            except ValueError:
                continue

        from gtimes.timefunc import parse_datetime_flexible as _gt_parse

        return _gt_parse(dt_input)

    def normalize_timestamp(self, dt: datetime, ffrequency: str) -> datetime:
        """Normalize timestamp based on file frequency.

        Different file frequencies require different timestamp normalization:
        - Daily files (24hr): normalize to midnight
        - Hourly files (1hr): normalize to hour boundary
        - Future: 15-minute files: normalize to 15-minute boundary

        Args:
            dt: Datetime to normalize
            ffrequency: File frequency (e.g., '24hr', '1hr')

        Returns:
            Normalized datetime
        """
        strategy = self._normalization_strategies.get(
            ffrequency, TimestampNormalization.NO_NORMALIZATION
        )

        if strategy == TimestampNormalization.MIDNIGHT:
            # Daily files: normalize to midnight
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)

        elif strategy == TimestampNormalization.HOUR_BOUNDARY:
            # Hourly files: normalize to hour boundary
            return dt.replace(minute=0, second=0, microsecond=0)

        elif strategy == TimestampNormalization.MINUTE_BOUNDARY:
            # Minute-based files: normalize to minute boundary
            return dt.replace(second=0, microsecond=0)

        else:
            # No normalization
            return dt

    def normalize_timestamps(
        self, dt_list: List[datetime], ffrequency: str
    ) -> List[datetime]:
        """Normalize list of timestamps based on file frequency.

        Args:
            dt_list: List of datetimes to normalize
            ffrequency: File frequency

        Returns:
            List of normalized datetimes
        """
        return [self.normalize_timestamp(dt, ffrequency) for dt in dt_list]

    def process_time_parameters(
        self,
        start: Union[datetime, str, None],
        end: Union[datetime, str, None],
        session: str,
    ) -> Tuple[datetime, datetime]:
        """Process and validate time parameters.

        This is the main method that consolidates time parameter processing
        from all receiver implementations. It handles:
        - String to datetime conversion
        - Default time range calculation
        - Session-aware time boundary adjustment

        Args:
            start: Start time (datetime object, string, or None for default)
            end: End time (datetime object, string, or None for default)
            session: Session type (e.g., '15s_24hr', '1Hz_1hr')

        Returns:
            Tuple of (start_datetime, end_datetime)

        Raises:
            ValueError: If time parameters are invalid
        """
        # Parse start time
        if start is not None:
            if isinstance(start, datetime):
                start_dt = start
            else:
                start_dt = self.parse_datetime_flexible(start)
        else:
            # Default start time logic would go here
            # For now, require explicit start time
            raise ValueError("Start time is required")

        # Parse end time
        if end is not None:
            if isinstance(end, datetime):
                end_dt = end
            else:
                end_dt = self.parse_datetime_flexible(end)
        else:
            # Default end time based on start time and session
            # This preserves existing behavior from CLI
            ffrequency = session.split("_")[1] if "_" in session else "24hr"
            if ffrequency == "1hr":
                # For hourly sessions, default to single hour
                end_dt = start_dt + timedelta(minutes=1)
            else:
                # For daily sessions, default to single day
                end_dt = start_dt + timedelta(days=1)

        # Validate time range
        if end_dt < start_dt:
            raise ValueError(f"End time {end_dt} is before start time {start_dt}")

        self.logger.debug(
            f"Processed time parameters: {start_dt} to {end_dt} for session {session}"
        )

        return start_dt, end_dt

    def calculate_default_time_range(
        self, days_back: int, session: str, reference_time: Optional[datetime] = None
    ) -> Tuple[datetime, datetime]:
        """Calculate default time range based on days-back parameter.

        This implements the CLI -D parameter logic that varies by session type.

        Args:
            days_back: Number of periods back (days for daily, hours for hourly)
            session: Session type
            reference_time: Reference time (default: now)

        Returns:
            Tuple of (start, end) datetimes
        """
        if reference_time is None:
            reference_time = datetime.now()

        ffrequency = session.split("_")[1] if "_" in session else "24hr"

        if ffrequency == "1hr":
            # For hourly sessions, -D N means N complete hours back
            # Example: -D 1 at 15:24 means download 14:00 file
            last_complete_hour = reference_time.replace(
                minute=0, second=0, microsecond=0
            ) - timedelta(hours=1)
            start = last_complete_hour - timedelta(hours=days_back - 1)
            end = last_complete_hour
        else:
            # For daily sessions, -D N means N days back
            start = reference_time - timedelta(days=days_back)
            end = reference_time - timedelta(days=1)

        return start, end

    def adjust_time_for_session(
        self, dt: datetime, session: str, adjustment: str = "start"
    ) -> datetime:
        """Adjust datetime to appropriate boundary for session type.

        Useful for aligning download times with file generation times.

        Args:
            dt: Datetime to adjust
            session: Session type
            adjustment: 'start' for start boundary, 'end' for end boundary

        Returns:
            Adjusted datetime
        """
        ffrequency = session.split("_")[1] if "_" in session else "24hr"

        if adjustment == "start":
            return self.normalize_timestamp(dt, ffrequency)
        else:
            # End adjustment: typically add one period
            normalized = self.normalize_timestamp(dt, ffrequency)
            if ffrequency == "1hr":
                return normalized + timedelta(hours=1)
            else:
                return normalized + timedelta(days=1)

    def get_time_range_description(
        self, start: datetime, end: datetime, session: str
    ) -> str:
        """Get human-readable description of time range.

        Useful for logging and user feedback.

        Args:
            start: Start datetime
            end: End datetime
            session: Session type

        Returns:
            Human-readable description
        """
        duration = end - start
        ffrequency = session.split("_")[1] if "_" in session else "24hr"

        if ffrequency == "1hr":
            hours = int(duration.total_seconds() / 3600)
            return (
                f"{hours} hour(s) from {start:%Y-%m-%d %H:00} to {end:%Y-%m-%d %H:00}"
            )
        else:
            days = duration.days
            return f"{days} day(s) from {start:%Y-%m-%d} to {end:%Y-%m-%d}"
