"""Unit tests for BulkDownloadScheduler job execution with mocks.

Tests that scheduled jobs execute correctly, call download methods properly,
handle errors, and integrate with production logging - all using mocks.
"""

import pytest
import logging
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call

# Check if APScheduler is available
try:
    from receivers.scheduling.bulk_scheduler import (
        BulkDownloadScheduler,
        HAS_APSCHEDULER
    )
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False
    pytestmark = pytest.mark.skip(reason="APScheduler not installed")


@pytest.mark.unit
@pytest.mark.scheduler
class TestSchedulerDownloadExecution:
    """Test download execution logic with mocks."""

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    @patch('receivers.scheduling.bulk_scheduler.get_station_config')
    @patch('receivers.scheduling.bulk_scheduler.create_receiver')
    @patch('receivers.scheduling.bulk_scheduler.setup_production_logging')
    def test_download_station_data_basic(
        self,
        mock_prod_logging,
        mock_create_receiver,
        mock_get_station,
        mock_get_all_stations
    ):
        """Test basic download execution with mocked receiver."""
        # Setup mocks
        mock_get_all_stations.return_value = {
            'TEST1': {'receiver_type': 'polarx5', 'enabled': True}
        }
        mock_get_station.return_value = {'station_id': 'TEST1'}

        # Mock receiver
        mock_receiver = Mock()
        mock_receiver.download_data.return_value = {
            'status': 'completed',
            'files_downloaded': 2,
            'total_bytes': 10000,
            'duration': 5.0,
            'errors': 0
        }
        mock_create_receiver.return_value = mock_receiver

        # Mock production logging
        mock_logger = Mock(spec=logging.Logger)
        mock_audit = Mock()
        mock_prod_config = Mock()
        mock_prod_config.create_station_logger.return_value = mock_logger
        mock_prod_config.get_audit_logger.return_value = mock_audit
        mock_prod_logging.return_value = mock_prod_config

        # Create scheduler and execute download
        scheduler = BulkDownloadScheduler(production_mode=True, max_workers=2)
        scheduler._download_station_data('TEST1', '1Hz_1hr')

        # Verify receiver.download_data was called
        assert mock_receiver.download_data.called
        call_kwargs = mock_receiver.download_data.call_args.kwargs

        # Verify download parameters
        assert call_kwargs['session'] == '1Hz_1hr'
        assert call_kwargs['sync'] is True
        assert call_kwargs['archive'] is True
        assert call_kwargs['immediate_archive'] is True
        assert call_kwargs['clean_tmp'] is True

        # Verify time parameters for hourly session
        start_time = call_kwargs['start']
        end_time = call_kwargs['end']
        assert isinstance(start_time, datetime)
        assert isinstance(end_time, datetime)
        assert (end_time - start_time) == timedelta(hours=1)

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    @patch('receivers.scheduling.bulk_scheduler.get_station_config')
    @patch('receivers.scheduling.bulk_scheduler.create_receiver')
    @patch('receivers.scheduling.bulk_scheduler.setup_production_logging')
    def test_download_daily_session_time_params(
        self,
        mock_prod_logging,
        mock_create_receiver,
        mock_get_station,
        mock_get_all_stations
    ):
        """Test that daily session (15s_24hr) gets correct time parameters."""
        # Setup mocks
        mock_get_all_stations.return_value = {
            'TEST1': {'receiver_type': 'polarx5', 'enabled': True}
        }
        mock_get_station.return_value = {'station_id': 'TEST1'}

        mock_receiver = Mock()
        mock_receiver.download_data.return_value = {
            'status': 'completed',
            'files_downloaded': 1,
            'duration': 10.0
        }
        mock_create_receiver.return_value = mock_receiver

        # Mock logging
        mock_prod_config = Mock()
        mock_prod_config.create_station_logger.return_value = Mock()
        mock_prod_config.get_audit_logger.return_value = Mock()
        mock_prod_logging.return_value = mock_prod_config

        # Execute daily download
        scheduler = BulkDownloadScheduler(production_mode=True)
        scheduler._download_station_data('TEST1', '15s_24hr')

        # Verify time parameters for daily session
        call_kwargs = mock_receiver.download_data.call_args.kwargs
        start_time = call_kwargs['start']
        end_time = call_kwargs['end']

        # Should be 1 day difference
        assert (end_time - start_time) == timedelta(days=1)
        # Frequency should be daily
        assert call_kwargs['ffrequency'] == '1D'

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    @patch('receivers.scheduling.bulk_scheduler.get_station_config')
    @patch('receivers.scheduling.bulk_scheduler.create_receiver')
    @patch('receivers.scheduling.bulk_scheduler.setup_production_logging')
    def test_download_error_handling(
        self,
        mock_prod_logging,
        mock_create_receiver,
        mock_get_station,
        mock_get_all_stations
    ):
        """Test that download errors are handled properly."""
        # Setup mocks
        mock_get_all_stations.return_value = {
            'TEST1': {'receiver_type': 'polarx5', 'enabled': True}
        }
        mock_get_station.return_value = {'station_id': 'TEST1'}

        # Mock receiver that raises error
        mock_receiver = Mock()
        mock_receiver.download_data.side_effect = ConnectionError("Connection failed")
        mock_create_receiver.return_value = mock_receiver

        # Mock logging
        mock_audit = Mock()
        mock_prod_config = Mock()
        mock_prod_config.create_station_logger.return_value = Mock()
        mock_prod_config.get_audit_logger.return_value = mock_audit
        mock_prod_logging.return_value = mock_prod_config

        # Execute download - should not raise
        scheduler = BulkDownloadScheduler(production_mode=True)
        scheduler._download_station_data('TEST1', '1Hz_1hr')

        # Verify audit logger recorded failure
        assert mock_audit.log_failure_event.called
        failure_args = mock_audit.log_failure_event.call_args
        assert failure_args[0][0] == 'TEST1'  # station_id
        assert 'ConnectionError' in failure_args[0][1]['error_type']

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    @patch('receivers.scheduling.bulk_scheduler.get_station_config')
    @patch('receivers.scheduling.bulk_scheduler.create_receiver')
    @patch('receivers.scheduling.bulk_scheduler.setup_production_logging')
    def test_download_audit_logging(
        self,
        mock_prod_logging,
        mock_create_receiver,
        mock_get_station,
        mock_get_all_stations
    ):
        """Test that successful downloads are logged to audit trail."""
        # Setup mocks
        mock_get_all_stations.return_value = {
            'TEST1': {'receiver_type': 'polarx5', 'enabled': True}
        }
        mock_get_station.return_value = {'station_id': 'TEST1'}

        mock_receiver = Mock()
        mock_receiver.download_data.return_value = {
            'status': 'completed',
            'files_downloaded': 3,
            'total_bytes': 50000,
            'duration': 15.5,
            'errors': 0
        }
        mock_create_receiver.return_value = mock_receiver

        # Mock logging
        mock_audit = Mock()
        mock_prod_config = Mock()
        mock_prod_config.create_station_logger.return_value = Mock()
        mock_prod_config.get_audit_logger.return_value = mock_audit
        mock_prod_logging.return_value = mock_prod_config

        # Execute download
        scheduler = BulkDownloadScheduler(production_mode=True)
        scheduler._download_station_data('TEST1', 'status_1hr')

        # Verify audit logging
        assert mock_audit.log_download_session.called
        audit_args = mock_audit.log_download_session.call_args
        assert audit_args[0][0] == 'TEST1'  # station_id

        session_data = audit_args[0][1]
        assert session_data['session'] == 'status_1hr'
        assert session_data['status'] == 'completed'
        assert session_data['files_downloaded'] == 3
        assert session_data['bytes_downloaded'] == 50000
        assert session_data['scheduled'] is True

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_running_jobs_tracking(self, mock_get_all_stations):
        """Test that running jobs are tracked correctly."""
        mock_get_all_stations.return_value = {
            'TEST1': {'receiver_type': 'polarx5', 'enabled': True}
        }

        scheduler = BulkDownloadScheduler(production_mode=False)

        # Initially no running jobs
        assert len(scheduler.running_jobs) == 0

        # Simulate job start
        job_id = '1Hz_1hr_TEST1'
        scheduler.running_jobs[job_id] = datetime.utcnow()

        # Verify job is tracked
        assert len(scheduler.running_jobs) == 1
        assert job_id in scheduler.running_jobs

        # Simulate job completion
        del scheduler.running_jobs[job_id]
        assert len(scheduler.running_jobs) == 0


@pytest.mark.unit
@pytest.mark.scheduler
class TestSchedulerEventHandlers:
    """Test scheduler event handlers."""

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_job_executed_event(self, mock_get_all_stations):
        """Test job execution event handler."""
        mock_get_all_stations.return_value = {}
        scheduler = BulkDownloadScheduler(production_mode=False)

        # Create mock event
        mock_event = Mock()
        mock_event.job_id = '1Hz_1hr_TEST1'

        # Should not raise
        scheduler._job_executed(mock_event)

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_job_error_event(self, mock_get_all_stations):
        """Test job error event handler."""
        mock_get_all_stations.return_value = {}
        scheduler = BulkDownloadScheduler(production_mode=False)

        # Create mock event with error
        mock_event = Mock()
        mock_event.job_id = '1Hz_1hr_TEST1'
        mock_event.exception = ValueError("Test error")

        # Should not raise
        scheduler._job_error(mock_event)


@pytest.mark.unit
@pytest.mark.scheduler
@pytest.mark.concurrent
class TestSchedulerConcurrentExecution:
    """Test concurrent execution behavior (mocked)."""

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_max_instances_one_per_job(self, mock_get_all_stations):
        """Test that each job has max_instances=1."""
        mock_get_all_stations.return_value = {
            'TEST1': {'receiver_type': 'polarx5', 'enabled': True},
            'TEST2': {'receiver_type': 'polarx5', 'enabled': True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False)
        scheduler.schedule_all_sessions()

        # Check job configurations
        jobs = scheduler.scheduler.get_jobs()
        for job in jobs:
            # Each job should have max_instances=1 (from job_defaults)
            assert hasattr(job, 'max_instances')
            assert job.max_instances == 1

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_multiple_workers(self, mock_get_all_stations):
        """Test scheduler can be configured with multiple workers."""
        mock_get_all_stations.return_value = {
            f'TEST{i}': {'receiver_type': 'polarx5', 'enabled': True}
            for i in range(5)
        }

        # Create scheduler with 3 workers
        scheduler = BulkDownloadScheduler(production_mode=False, max_workers=3)

        # Verify executor configuration
        assert scheduler.max_workers == 3


@pytest.mark.unit
@pytest.mark.scheduler
class TestSchedulerConfiguration:
    """Test scheduler configuration management."""

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_disabled_session(self, mock_get_all_stations):
        """Test that disabled sessions are not scheduled."""
        mock_get_all_stations.return_value = {
            'TEST1': {'receiver_type': 'polarx5', 'enabled': True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False)

        # Disable status_1hr session
        scheduler.schedule_configs['status_1hr'].enabled = False

        # Schedule all
        scheduler.schedule_all_sessions()

        # Should only have 2 sessions scheduled (15s_24hr and 1Hz_1hr)
        jobs = scheduler.get_scheduled_jobs()
        session_types = set(job['id'].split('_')[0] for job in jobs)

        assert '15s_24hr' in session_types
        assert '1Hz_1hr' in session_types
        assert 'status_1hr' not in session_types  # Disabled

    @patch('receivers.scheduling.bulk_scheduler.get_all_station_configs')
    def test_custom_schedule_config(self, mock_get_all_stations):
        """Test custom schedule configuration."""
        mock_get_all_stations.return_value = {
            'TEST1': {'receiver_type': 'polarx5', 'enabled': True},
        }

        scheduler = BulkDownloadScheduler(production_mode=False)

        # Modify config
        scheduler.schedule_configs['1Hz_1hr'].schedule_minute = 30
        scheduler.schedule_configs['1Hz_1hr'].distribution_window = 20

        # Schedule
        scheduler.schedule_all_sessions()

        # Verify jobs scheduled with new config
        jobs = scheduler.get_scheduled_jobs()
        hz_jobs = [j for j in jobs if '1Hz_1hr' in j['id']]

        assert len(hz_jobs) > 0
        # Jobs should be scheduled (exact trigger verification would require parsing cron)
