"""Unit tests for TimeParameterProcessor utility.

These tests validate that the TimeParameterProcessor produces identical results
to the existing receiver implementations' time handling logic.
"""

from datetime import datetime, timedelta

import pytest

from receivers.utils.time_processor import (
    DatetimeParser,
    TimeParameterProcessor,
    TimestampNormalization,
)
from tests.fixtures.test_data import (
    TIME_PARAMETER_CASES,
    TIMESTAMP_NORMALIZATION_CASES,
    get_test_case_by_name,
)


class TestDatetimeParsing:
    """Test flexible datetime parsing."""

    def setup_method(self):
        """Set up test processor."""
        self.processor = TimeParameterProcessor()

    def test_datetime_object_passthrough(self):
        """Test that datetime objects pass through unchanged."""
        dt = datetime(2025, 9, 24, 14, 30, 0)
        result = self.processor.parse_datetime_flexible(dt)
        assert result == dt
        assert isinstance(result, datetime)

    def test_iso_format_parsing(self):
        """Test ISO format datetime string parsing."""
        dt_string = "2025-09-24T14:30:00"
        result = self.processor.parse_datetime_flexible(dt_string)
        assert result == datetime(2025, 9, 24, 14, 30, 0)

    def test_date_only_parsing(self):
        """Test date-only string parsing."""
        date_string = "2025-09-24"
        result = self.processor.parse_datetime_flexible(date_string)
        assert result == datetime(2025, 9, 24, 0, 0, 0)

    def test_datetime_with_space_parsing(self):
        """Test datetime string with space separator."""
        dt_string = "2025-09-24 14:30:00"
        result = self.processor.parse_datetime_flexible(dt_string)
        assert result == datetime(2025, 9, 24, 14, 30, 0)

    def test_compact_date_parsing(self):
        """Test compact date format (YYYYMMDD)."""
        date_string = "20250924"
        result = self.processor.parse_datetime_flexible(date_string)
        assert result == datetime(2025, 9, 24, 0, 0, 0)

    def test_compact_with_dash_parsing(self):
        """Test compact format with dash (YYYYMMDD-HHMM)."""
        dt_string = "20250924-1430"
        result = self.processor.parse_datetime_flexible(dt_string)
        assert result == datetime(2025, 9, 24, 14, 30, 0)

    def test_invalid_format_raises_error(self):
        """Test that invalid format raises ValueError."""
        with pytest.raises(ValueError, match="Could not parse datetime string"):
            self.processor.parse_datetime_flexible("invalid-date-format")

    def test_empty_string_raises_error(self):
        """Test that empty string raises ValueError."""
        with pytest.raises(ValueError):
            self.processor.parse_datetime_flexible("")


class TestTimestampNormalization:
    """Test timestamp normalization for different file frequencies."""

    def setup_method(self):
        """Set up test processor."""
        self.processor = TimeParameterProcessor()

    def test_daily_file_midnight_normalization(self):
        """Test that daily files normalize to midnight."""
        case = get_test_case_by_name(
            TIMESTAMP_NORMALIZATION_CASES, "daily_file_midnight_normalization"
        )
        result = self.processor.normalize_timestamp(
            case["input_datetime"], case["ffrequency"]
        )
        assert result == case["expected_normalized"]

    def test_hourly_file_hour_boundary(self):
        """Test that hourly files normalize to hour boundary."""
        case = get_test_case_by_name(
            TIMESTAMP_NORMALIZATION_CASES, "hourly_file_hour_boundary"
        )
        result = self.processor.normalize_timestamp(
            case["input_datetime"], case["ffrequency"]
        )
        assert result == case["expected_normalized"]

    def test_status_hourly_normalization(self):
        """Test status hourly file normalization."""
        case = get_test_case_by_name(
            TIMESTAMP_NORMALIZATION_CASES, "status_hourly_normalization"
        )
        result = self.processor.normalize_timestamp(
            case["input_datetime"], case["ffrequency"]
        )
        assert result == case["expected_normalized"]

    def test_normalize_timestamps_list(self):
        """Test normalizing list of timestamps."""
        dt_list = [
            datetime(2025, 9, 24, 15, 30, 45),
            datetime(2025, 9, 24, 16, 45, 30),
            datetime(2025, 9, 24, 17, 15, 10),
        ]

        result = self.processor.normalize_timestamps(dt_list, "1hr")

        expected = [
            datetime(2025, 9, 24, 15, 0, 0),
            datetime(2025, 9, 24, 16, 0, 0),
            datetime(2025, 9, 24, 17, 0, 0),
        ]

        assert result == expected

    def test_unknown_frequency_no_normalization(self):
        """Test that unknown frequency doesn't normalize."""
        dt = datetime(2025, 9, 24, 15, 30, 45)
        result = self.processor.normalize_timestamp(dt, "unknown_freq")
        assert result == dt  # No normalization applied


class TestTimeParameterProcessing:
    """Test complete time parameter processing."""

    def setup_method(self):
        """Set up test processor."""
        self.processor = TimeParameterProcessor()

    def test_process_datetime_objects(self):
        """Test processing with datetime objects."""
        start = datetime(2025, 9, 24, 0, 0, 0)
        end = datetime(2025, 9, 25, 0, 0, 0)

        result_start, result_end = self.processor.process_time_parameters(
            start, end, "15s_24hr"
        )

        assert result_start == start
        assert result_end == end

    def test_process_string_parameters(self):
        """Test processing with string parameters."""
        result_start, result_end = self.processor.process_time_parameters(
            "2025-09-24", "2025-09-25", "15s_24hr"
        )

        assert result_start == datetime(2025, 9, 24, 0, 0, 0)
        assert result_end == datetime(2025, 9, 25, 0, 0, 0)

    def test_process_mixed_types(self):
        """Test processing with mixed datetime and string."""
        start = datetime(2025, 9, 24, 0, 0, 0)
        end_str = "2025-09-25"

        result_start, result_end = self.processor.process_time_parameters(
            start, end_str, "15s_24hr"
        )

        assert result_start == start
        assert result_end == datetime(2025, 9, 25, 0, 0, 0)

    def test_process_with_none_start_raises(self):
        """Test that None start raises ValueError."""
        with pytest.raises(ValueError, match="Start time is required"):
            self.processor.process_time_parameters(None, datetime.now(), "15s_24hr")

    def test_process_with_none_end_daily(self):
        """Test None end defaults to single day for daily session."""
        start = datetime(2025, 9, 24, 0, 0, 0)

        result_start, result_end = self.processor.process_time_parameters(
            start, None, "15s_24hr"
        )

        assert result_start == start
        assert result_end == start + timedelta(days=1)

    def test_process_with_none_end_hourly(self):
        """Test None end defaults to single hour for hourly session."""
        start = datetime(2025, 9, 24, 14, 0, 0)

        result_start, result_end = self.processor.process_time_parameters(
            start, None, "1Hz_1hr"
        )

        assert result_start == start
        # Should default to 1 minute after start (CLI behavior)
        assert result_end == start + timedelta(minutes=1)

    def test_process_validates_time_range(self):
        """Test that end before start raises ValueError."""
        start = datetime(2025, 9, 25, 0, 0, 0)
        end = datetime(2025, 9, 24, 0, 0, 0)  # Before start

        with pytest.raises(ValueError, match="End time .* is before start time"):
            self.processor.process_time_parameters(start, end, "15s_24hr")


class TestDefaultTimeRangeCalculation:
    """Test default time range calculation (-D parameter logic)."""

    def setup_method(self):
        """Set up test processor."""
        self.processor = TimeParameterProcessor()
        self.reference_time = datetime(2025, 9, 24, 15, 30, 0)

    def test_daily_session_days_back(self):
        """Test daily session calculates days back correctly."""
        start, end = self.processor.calculate_default_time_range(
            days_back=3, session="15s_24hr", reference_time=self.reference_time
        )

        # Should be 3 days back from reference
        expected_start = self.reference_time - timedelta(days=3)
        expected_end = self.reference_time - timedelta(days=1)

        assert start == expected_start
        assert end == expected_end

    def test_hourly_session_hours_back(self):
        """Test hourly session calculates complete hours back."""
        start, end = self.processor.calculate_default_time_range(
            days_back=4,  # Actually means 4 hours for hourly sessions
            session="1Hz_1hr",
            reference_time=self.reference_time,
        )

        # Last complete hour: 14:00
        # 4 hours back from 14:00: 11:00, 12:00, 13:00, 14:00
        expected_end = datetime(2025, 9, 24, 14, 0, 0)
        expected_start = datetime(2025, 9, 24, 11, 0, 0)

        assert start == expected_start
        assert end == expected_end

    def test_single_hour_back(self):
        """Test -D 1 for hourly session."""
        start, end = self.processor.calculate_default_time_range(
            days_back=1, session="1Hz_1hr", reference_time=self.reference_time
        )

        # Should be just the last complete hour
        expected_end = datetime(2025, 9, 24, 14, 0, 0)
        expected_start = datetime(2025, 9, 24, 14, 0, 0)

        assert start == expected_start
        assert end == expected_end


class TestCustomParsers:
    """Test custom parser registration."""

    def test_register_custom_parser(self):
        """Test registering and using custom parser."""
        processor = TimeParameterProcessor()

        # Create custom parser for GPS week:DOY format
        class GPSWeekDOYParser:
            def parse(self, dt_string):
                if ":" not in dt_string:
                    raise ValueError("Invalid format")
                week_str, doy_str = dt_string.split(":")
                # Simplified - just for testing
                return datetime(2025, 9, 24, 0, 0, 0)

            def get_format_description(self):
                return "GPS Week:DOY"

        custom_parser = GPSWeekDOYParser()
        processor.register_parser(custom_parser)

        # Should use custom parser
        result = processor.parse_datetime_flexible("2250:267")
        assert result == datetime(2025, 9, 24, 0, 0, 0)


class TestNormalizationStrategies:
    """Test custom normalization strategy registration."""

    def test_register_custom_strategy(self):
        """Test registering custom normalization strategy."""
        processor = TimeParameterProcessor()

        # Register 15-minute normalization
        processor.register_normalization_strategy(
            "15min", TimestampNormalization.MINUTE_BOUNDARY
        )

        # Should normalize to minute boundary
        dt = datetime(2025, 9, 24, 15, 30, 45)
        result = processor.normalize_timestamp(dt, "15min")
        assert result == datetime(2025, 9, 24, 15, 30, 0)


class TestTimeRangeDescriptions:
    """Test human-readable time range descriptions."""

    def setup_method(self):
        """Set up test processor."""
        self.processor = TimeParameterProcessor()

    def test_daily_range_description(self):
        """Test description for daily range."""
        start = datetime(2025, 9, 24, 0, 0, 0)
        end = datetime(2025, 9, 26, 0, 0, 0)

        description = self.processor.get_time_range_description(start, end, "15s_24hr")

        assert "2 day(s)" in description
        assert "2025-09-24" in description
        assert "2025-09-26" in description

    def test_hourly_range_description(self):
        """Test description for hourly range."""
        start = datetime(2025, 9, 24, 10, 0, 0)
        end = datetime(2025, 9, 24, 14, 0, 0)

        description = self.processor.get_time_range_description(start, end, "1Hz_1hr")

        assert "4 hour(s)" in description
        assert "10:00" in description
        assert "14:00" in description


class TestTimeAdjustment:
    """Test time adjustment for session boundaries."""

    def setup_method(self):
        """Set up test processor."""
        self.processor = TimeParameterProcessor()

    def test_adjust_to_start_boundary_daily(self):
        """Test adjusting to start boundary for daily."""
        dt = datetime(2025, 9, 24, 15, 30, 45)
        result = self.processor.adjust_time_for_session(
            dt, "15s_24hr", adjustment="start"
        )
        assert result == datetime(2025, 9, 24, 0, 0, 0)

    def test_adjust_to_end_boundary_daily(self):
        """Test adjusting to end boundary for daily."""
        dt = datetime(2025, 9, 24, 15, 30, 45)
        result = self.processor.adjust_time_for_session(
            dt, "15s_24hr", adjustment="end"
        )
        # Should add one day to normalized start
        assert result == datetime(2025, 9, 25, 0, 0, 0)

    def test_adjust_to_start_boundary_hourly(self):
        """Test adjusting to start boundary for hourly."""
        dt = datetime(2025, 9, 24, 15, 30, 45)
        result = self.processor.adjust_time_for_session(
            dt, "1Hz_1hr", adjustment="start"
        )
        assert result == datetime(2025, 9, 24, 15, 0, 0)

    def test_adjust_to_end_boundary_hourly(self):
        """Test adjusting to end boundary for hourly."""
        dt = datetime(2025, 9, 24, 15, 30, 45)
        result = self.processor.adjust_time_for_session(dt, "1Hz_1hr", adjustment="end")
        # Should add one hour to normalized start
        assert result == datetime(2025, 9, 24, 16, 0, 0)
