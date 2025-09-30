"""Unit tests for BulkDownloadScheduler basic functionality.

Tests scheduler initialization, configuration, job scheduling logic,
and time distribution without executing actual downloads.
"""

import pytest
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

# Check if APScheduler is available
try:
    from receivers.scheduling.bulk_scheduler import (
        BulkDownloadScheduler,
        ScheduleConfig,
        create_scheduler_config,
        HAS_APSCHEDULER
    )
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False
    pytestmark = pytest.mark.skip(reason="APScheduler not installed")


@pytest.mark.unit
@pytest.mark.scheduler
class TestScheduleConfig:
    """Test ScheduleConfig dataclass."""

    def test_schedule_config_creation(self):
        """Test creating schedule configuration."""
        config = ScheduleConfig(
            session_type='1Hz_1hr',
            schedule_minute=15,
            distribution_window=10,
            frequency='hourly',
            enabled=True,
            max_concurrent=4,
            timeout_minutes=30
        )

        assert config.session_type == '1Hz_1hr'
        assert config.schedule_minute == 15
        assert config.distribution_window == 10
        assert config.frequency == 'hourly'
        assert config.enabled is True
        assert config.max_concurrent == 4
        assert config.timeout_minutes == 30

    def test_schedule_config_defaults(self):
        """Test schedule configuration with defaults."""
        config = ScheduleConfig(
            session_type='15s_24hr',
            schedule_minute=10,
            distribution_window=10,
            frequency='daily'
        )

        # Defaults
        assert config.enabled is True
        assert config.max_concurrent == 3
        assert config.timeout_minutes == 30


@pytest.mark.unit
@pytest.mark.scheduler
class TestBulkDownloadSchedulerInit:
    """Test BulkDownloadScheduler initialization."""

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_scheduler_initialization(self, mock_get_stations):
        """Test basic scheduler initialization."""
        # Mock station configs
        mock_get_stations.return_value = {
            'TEST1': {'receiver_type': 'polarx5', 'enabled': True},
            'TEST2': {'receiver_type': 'netr9', 'enabled': True},
        }

        scheduler = BulkDownloadScheduler(
            production_mode=False,
            max_workers=2
        )

        assert scheduler.max_workers == 2
        assert scheduler.production_mode is False
        assert len(scheduler.stations) == 2
        assert 'TEST1' in scheduler.stations
        assert 'TEST2' in scheduler.stations

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_scheduler_with_station_filter(self, mock_get_stations):
        """Test scheduler with station filter."""
        mock_get_stations.return_value = {
            'ELDC': {'receiver_type': 'polarx5', 'enabled': True},
            'ORFC': {'receiver_type': 'polarx5', 'enabled': True},
            'THOB': {'receiver_type': 'netr9', 'enabled': True},
        }

        scheduler = BulkDownloadScheduler(
            production_mode=False,
            station_filter=['ELDC', 'orfc']  # Test case insensitivity
        )

        # Should filter to only specified stations
        assert scheduler.station_filter == ['ELDC', 'ORFC']  # Uppercased

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_scheduler_with_max_stations(self, mock_get_stations):
        """Test scheduler with max stations limit."""
        mock_get_stations.return_value = {
            f'TEST{i}': {'receiver_type': 'polarx5', 'enabled': True}
            for i in range(10)
        }

        scheduler = BulkDownloadScheduler(
            production_mode=False,
            max_stations_per_session=3
        )

        assert scheduler.max_stations_per_session == 3

    def test_scheduler_default_configs(self):
        """Test scheduler has correct default session configs."""
        with patch('receivers.scheduling.bulk_scheduler.get_all_station_configs', return_value={}):
            scheduler = BulkDownloadScheduler(production_mode=False)

            # Should have 3 default session types
            assert len(scheduler.schedule_configs) == 3
            assert '15s_24hr' in scheduler.schedule_configs
            assert '1Hz_1hr' in scheduler.schedule_configs
            assert 'status_1hr' in scheduler.schedule_configs

            # Check 15s_24hr config
            daily_config = scheduler.schedule_configs['15s_24hr']
            assert daily_config.frequency == 'daily'
            assert daily_config.schedule_minute == 10
            assert daily_config.distribution_window == 10

            # Check 1Hz_1hr config
            hourly_config = scheduler.schedule_configs['1Hz_1hr']
            assert hourly_config.frequency == 'hourly'
            assert hourly_config.schedule_minute == 15
            assert hourly_config.distribution_window == 10

            # Check status_1hr config
            status_config = scheduler.schedule_configs['status_1hr']
            assert status_config.frequency == 'hourly'
            assert status_config.schedule_minute == 25
            assert status_config.distribution_window == 5


@pytest.mark.unit
@pytest.mark.scheduler
class TestSchedulerStationFiltering:
    """Test station filtering logic."""

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_get_stations_for_session_no_filter(self, mock_get_stations):
        """Test getting stations without filter."""
        mock_get_stations.return_value = {
            'ELDC': {'receiver_type': 'polarx5', 'enabled': True},
            'ORFC': {'receiver_type': 'polarx5', 'enabled': True},
            'THOB': {'receiver_type': 'netr9', 'enabled': False},  # Disabled
        }

        scheduler = BulkDownloadScheduler(production_mode=False)
        stations = scheduler._get_stations_for_session('1Hz_1hr')

        # Should return enabled stations
        assert len(stations) == 2
        assert 'ELDC' in stations
        assert 'ORFC' in stations
        assert 'THOB' not in stations  # Disabled

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_get_stations_with_filter(self, mock_get_stations):
        """Test getting stations with filter applied."""
        mock_get_stations.return_value = {
            'ELDC': {'receiver_type': 'polarx5', 'enabled': True},
            'ORFC': {'receiver_type': 'polarx5', 'enabled': True},
            'THOB': {'receiver_type': 'netr9', 'enabled': True},
        }

        scheduler = BulkDownloadScheduler(
            production_mode=False,
            station_filter=['ELDC', 'THOB']
        )
        stations = scheduler._get_stations_for_session('1Hz_1hr')

        # Should return only filtered stations
        assert len(stations) == 2
        assert 'ELDC' in stations
        assert 'THOB' in stations
        assert 'ORFC' not in stations

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_get_stations_with_max_limit(self, mock_get_stations):
        """Test getting stations with max limit."""
        mock_get_stations.return_value = {
            f'TEST{i}': {'receiver_type': 'polarx5', 'enabled': True}
            for i in range(10)
        }

        scheduler = BulkDownloadScheduler(
            production_mode=False,
            max_stations_per_session=3
        )
        stations = scheduler._get_stations_for_session('1Hz_1hr')

        # Should limit to max_stations
        assert len(stations) == 3


@pytest.mark.unit
@pytest.mark.scheduler
class TestSchedulerTimeDistribution:
    """Test time distribution logic for scheduled downloads."""

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_time_distribution_calculation(self, mock_get_stations):
        """Test that stations are distributed across time window."""
        # Create 10 stations
        mock_get_stations.return_value = {
            f'TEST{i:02d}': {'receiver_type': 'polarx5', 'enabled': True}
            for i in range(10)
        }

        scheduler = BulkDownloadScheduler(production_mode=False)

        # Get config for 1Hz_1hr (minute 15, window 10)
        config = scheduler.schedule_configs['1Hz_1hr']
        stations = scheduler._get_stations_for_session('1Hz_1hr')

        # Calculate how stations should be distributed
        stations_per_minute = len(stations) / config.distribution_window  # 10 / 10 = 1

        # First station should be at minute 15
        minute_offset_0 = int(0 / stations_per_minute)  # 0
        assert minute_offset_0 == 0
        schedule_minute_0 = config.schedule_minute + minute_offset_0  # 15 + 0 = 15

        # Fifth station should be at minute 20 (halfway through)
        minute_offset_5 = int(5 / stations_per_minute)  # 5
        assert minute_offset_5 == 5
        schedule_minute_5 = config.schedule_minute + minute_offset_5  # 15 + 5 = 20

        # Last station should be at minute 24 (within window)
        minute_offset_9 = int(9 / stations_per_minute)  # 9
        assert minute_offset_9 == 9
        schedule_minute_9 = config.schedule_minute + minute_offset_9  # 15 + 9 = 24

        # Verify all within window
        assert schedule_minute_0 >= config.schedule_minute
        assert schedule_minute_9 < (config.schedule_minute + config.distribution_window)

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_many_stations_distribution(self, mock_get_stations):
        """Test distribution with many stations (more than window minutes)."""
        # Create 50 stations with 10-minute window
        mock_get_stations.return_value = {
            f'TEST{i:03d}': {'receiver_type': 'polarx5', 'enabled': True}
            for i in range(50)
        }

        scheduler = BulkDownloadScheduler(production_mode=False)
        config = scheduler.schedule_configs['1Hz_1hr']
        stations = scheduler._get_stations_for_session('1Hz_1hr')

        stations_per_minute = len(stations) / config.distribution_window  # 50 / 10 = 5

        # Multiple stations should get same minute
        minute_offset_0 = int(0 / stations_per_minute)  # 0
        minute_offset_4 = int(4 / stations_per_minute)  # 0 (both in first minute)
        assert minute_offset_0 == minute_offset_4

        minute_offset_5 = int(5 / stations_per_minute)  # 1
        assert minute_offset_5 == 1


@pytest.mark.unit
@pytest.mark.scheduler
class TestSchedulerJobScheduling:
    """Test job scheduling without execution."""

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_schedule_all_sessions(self, mock_get_stations):
        """Test scheduling all session types."""
        mock_get_stations.return_value = {
            'TEST1': {'receiver_type': 'polarx5', 'enabled': True},
            'TEST2': {'receiver_type': 'polarx5', 'enabled': True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False, max_workers=2)

        # Schedule all sessions
        scheduler.schedule_all_sessions()

        # Should have jobs scheduled
        jobs = scheduler.get_scheduled_jobs()

        # 2 stations × 3 session types = 6 jobs
        assert len(jobs) == 6

        # Check job IDs follow pattern: session_station
        job_ids = [job['id'] for job in jobs]
        assert '15s_24hr_TEST1' in job_ids
        assert '15s_24hr_TEST2' in job_ids
        assert '1Hz_1hr_TEST1' in job_ids
        assert '1Hz_1hr_TEST2' in job_ids
        assert 'status_1hr_TEST1' in job_ids
        assert 'status_1hr_TEST2' in job_ids

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_get_job_status(self, mock_get_stations):
        """Test getting scheduler status."""
        mock_get_stations.return_value = {
            'TEST1': {'receiver_type': 'polarx5', 'enabled': True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False)
        scheduler.schedule_all_sessions()

        status = scheduler.get_job_status()

        assert 'scheduler_running' in status
        assert 'total_jobs' in status
        assert 'running_jobs' in status
        assert 'current_jobs' in status
        assert status['total_jobs'] == 3  # 1 station × 3 sessions


@pytest.mark.unit
@pytest.mark.scheduler
def test_create_scheduler_config(tmp_path):
    """Test creating scheduler configuration file."""
    with patch('pathlib.Path.home', return_value=tmp_path):
        config_file = create_scheduler_config()

        assert config_file.exists()
        assert config_file.name == 'scheduler.json'

        # Read and verify config
        import json
        with open(config_file) as f:
            config = json.load(f)

        assert 'database_url' in config
        assert 'production_mode' in config
        assert 'max_workers' in config
        assert 'sessions' in config

        # Check session configs
        assert '15s_24hr' in config['sessions']
        assert '1Hz_1hr' in config['sessions']
        assert 'status_1hr' in config['sessions']

        # Verify session structure
        daily_session = config['sessions']['15s_24hr']
        assert daily_session['enabled'] is True
        assert daily_session['frequency'] == 'daily'
        assert daily_session['schedule_minute'] == 10
