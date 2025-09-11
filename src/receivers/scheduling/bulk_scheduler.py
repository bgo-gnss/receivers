#!/usr/bin/env python3
"""
APScheduler-based bulk download system for GPS receivers.

Features:
- Distributed scheduling across time windows
- Complete manual operation compatibility  
- Production logging integration
- Email alert integration
- Performance monitoring
- Fault tolerance and recovery
"""

import logging
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
    import logging.handlers
    HAS_APSCHEDULER = True
except ImportError as e:
    HAS_APSCHEDULER = False
    _import_error = str(e)


@dataclass
class ScheduleConfig:
    """Configuration for scheduled downloads."""
    session_type: str
    schedule_minute: int  # Minute past the hour/day
    distribution_window: int  # Minutes to spread downloads across
    frequency: str  # 'daily' or 'hourly'
    enabled: bool = True
    max_concurrent: int = 3
    timeout_minutes: int = 30


class BulkDownloadScheduler:
    """APScheduler-based bulk download system with full manual compatibility."""
    
    def __init__(self, 
                 database_url: str = None,
                 log_dir: Path = None,
                 production_mode: bool = True,
                 max_workers: int = 5,
                 station_filter: List[str] = None,
                 max_stations_per_session: int = None):
        
        if not HAS_APSCHEDULER:
            raise ImportError("APScheduler not available. Install with: pip install apscheduler")
        
        self.database_url = database_url or f"sqlite:///{Path.home()}/.cache/gps_receivers/scheduler.db"
        self.log_dir = log_dir or Path.home() / '.cache' / 'gps_receivers' / 'logs'
        self.production_mode = production_mode
        self.max_workers = max_workers
        self.station_filter = [s.upper() for s in station_filter] if station_filter else None
        self.max_stations_per_session = max_stations_per_session
        
        # Set up logging
        self._setup_logging()
        
        # Initialize scheduler with persistent job store
        self._setup_scheduler()
        
        # Default schedule configurations
        self.schedule_configs = {
            '15s_24hr': ScheduleConfig(
                session_type='15s_24hr',
                schedule_minute=10,  # 00:10 daily
                distribution_window=10,  # 00:10-00:19
                frequency='daily',
                max_concurrent=3,
                timeout_minutes=45
            ),
            '1Hz_1hr': ScheduleConfig(
                session_type='1Hz_1hr', 
                schedule_minute=15,  # XX:15 hourly
                distribution_window=10,  # XX:15-XX:24
                frequency='hourly',
                max_concurrent=4,
                timeout_minutes=30
            ),
            'status_1hr': ScheduleConfig(
                session_type='status_1hr',
                schedule_minute=25,  # XX:25 hourly  
                distribution_window=5,  # XX:25-XX:29
                frequency='hourly',
                max_concurrent=5,
                timeout_minutes=15
            )
        }
        
        # Load station configurations
        self.stations = self._load_station_configs()
        
        # Track running jobs
        self.running_jobs = {}
        
    def _setup_logging(self):
        """Set up scheduler logging."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.logger = logging.getLogger('gps_scheduler')
        self.logger.setLevel(logging.INFO)
        
        # Remove existing handlers
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
        
        # File handler for scheduler logs
        log_file = self.log_dir / 'scheduler.log'
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10*1024*1024, backupCount=3
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        self.logger.addHandler(file_handler)
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s'
        ))
        self.logger.addHandler(console_handler)
        
    def _setup_scheduler(self):
        """Initialize APScheduler with persistent storage."""
        
        # Job store configuration
        jobstores = {
            'default': SQLAlchemyJobStore(url=self.database_url)
        }
        
        # Executor configuration  
        executors = {
            'default': ThreadPoolExecutor(self.max_workers),
        }
        
        # Job defaults
        job_defaults = {
            'coalesce': False,  # Don't combine missed jobs
            'max_instances': 1,  # Only one instance per job
            'misfire_grace_time': 300  # 5 minute grace period
        }
        
        # Initialize scheduler
        self.scheduler = BackgroundScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults
        )
        
        # Add event listeners
        self.scheduler.add_listener(self._job_executed, EVENT_JOB_EXECUTED)
        self.scheduler.add_listener(self._job_error, EVENT_JOB_ERROR)
        
    def _load_station_configs(self) -> Dict[str, Dict[str, Any]]:
        """Load station configurations from gps_parser."""
        stations = {}
        
        try:
            # Use the existing station loading from CLI  
            from ..cli.main import get_all_station_configs
            all_stations = get_all_station_configs()
            
            for station_id, config in all_stations.items():
                # Extract relevant configuration
                stations[station_id] = {
                    'station_id': station_id,
                    'receiver_type': config.get('receiver_type', 'unknown'),
                    'ip_number': config.get('ip_number', ''),
                    'ip_port': config.get('ip_port', 21),
                    'enabled': config.get('enabled', True),
                    'timeout_category': config.get('timeout_category', 'default')
                }
                
        except Exception as e:
            self.logger.error(f"Failed to load station configurations: {e}")
            # Fallback: empty station list
            stations = {}
            
        self.logger.info(f"Loaded {len(stations)} station configurations")
        return stations
        
    def schedule_all_sessions(self):
        """Schedule all configured download sessions."""
        
        for session_type, config in self.schedule_configs.items():
            if not config.enabled:
                self.logger.info(f"Skipping disabled session: {session_type}")
                continue
                
            stations_for_session = self._get_stations_for_session(session_type)
            if not stations_for_session:
                self.logger.warning(f"No stations configured for session: {session_type}")
                continue
                
            self._schedule_session_downloads(session_type, config, stations_for_session)
            
        self.logger.info(f"Scheduled downloads for {len(self.schedule_configs)} session types")
        
    def _get_stations_for_session(self, session_type: str) -> List[str]:
        """Get list of stations that support a specific session type."""
        
        stations = []
        
        for station_id, config in self.stations.items():
            if not config.get('enabled', True):
                continue
                
            # Apply station filter if specified
            if self.station_filter and station_id not in self.station_filter:
                continue
                
            stations.append(station_id)
        
        # Apply max stations limit if specified
        if self.max_stations_per_session and len(stations) > self.max_stations_per_session:
            stations = stations[:self.max_stations_per_session]
            self.logger.info(f"Limited {session_type} to {self.max_stations_per_session} stations for testing")
                
        return stations
        
    def _schedule_session_downloads(self, 
                                  session_type: str, 
                                  config: ScheduleConfig, 
                                  stations: List[str]):
        """Schedule downloads for a specific session type."""
        
        # Distribute stations across the time window
        stations_per_minute = len(stations) / config.distribution_window
        
        for i, station_id in enumerate(stations):
            # Calculate minute offset within distribution window
            minute_offset = int(i / stations_per_minute)
            schedule_minute = config.schedule_minute + minute_offset
            
            # Create job ID
            job_id = f"{session_type}_{station_id}"
            
            # Schedule based on frequency
            if config.frequency == 'daily':
                self.scheduler.add_job(
                    func=self._download_station_data,
                    trigger='cron',
                    args=[station_id, session_type],
                    hour=0,
                    minute=schedule_minute,
                    id=job_id,
                    replace_existing=True,
                    max_instances=1
                )
                
            elif config.frequency == 'hourly':
                self.scheduler.add_job(
                    func=self._download_station_data,
                    trigger='cron',
                    args=[station_id, session_type],
                    minute=schedule_minute,
                    id=job_id,
                    replace_existing=True,
                    max_instances=1
                )
                
        self.logger.info(f"Scheduled {len(stations)} stations for {session_type} "
                        f"({config.frequency} at {config.schedule_minute:02d}:XX)")
        
    def _download_station_data(self, station_id: str, session_type: str):
        """Download data for a single station (job function)."""
        
        job_id = f"{session_type}_{station_id}"
        start_time = datetime.utcnow()
        
        try:
            self.logger.info(f"Starting download: {station_id} ({session_type})")
            self.running_jobs[job_id] = start_time
            
            # Import receiver management here to avoid circular imports
            from ..cli.main import get_station_config, create_receiver
            from ..base.production_logging import setup_production_logging
            
            # Set up production logging
            if self.production_mode:
                prod_config = setup_production_logging(json_output=False, verbose=False)
                logger = prod_config.create_station_logger(station_id)
                audit_logger = prod_config.get_audit_logger()
            else:
                logger = logging.getLogger(f'receiver.{station_id}')
                audit_logger = None
                
            # Get station configuration
            station_config = get_station_config(station_id)
            if not station_config:
                raise ValueError(f"No configuration found for station {station_id}")
                
            # Create receiver instance
            receiver = create_receiver(station_id, station_config, logger)
            
            # Determine time range based on session type
            if session_type == '15s_24hr':
                # Daily data - get yesterday's data
                end_time = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                start_time = end_time - timedelta(days=1)
                frequency = '1D'
            else:
                # Hourly data - get previous hour's data
                end_time = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
                start_time = end_time - timedelta(hours=1)
                frequency = '1H'
                
            # Download data with all our enhanced features
            result = receiver.download_data(
                start=start_time,
                end=end_time,
                session=session_type,
                ffrequency=frequency,
                sync=True,  # Always sync in scheduled mode
                archive=True,  # Always archive
                immediate_archive=True,  # Use fault-tolerant immediate archiving
                clean_tmp=True,
                compression='.gz',
                loglevel=logging.INFO
            )
            
            # Log results to audit trail
            if audit_logger:
                audit_logger.log_download_session(station_id, {
                    'session': session_type,
                    'status': result.get('status', 'completed'),
                    'duration': result.get('duration', 0),
                    'files_downloaded': result.get('files_downloaded', 0),
                    'bytes_downloaded': result.get('total_bytes', 0),
                    'errors': result.get('errors', 0),
                    'scheduled': True,
                    'start_time': start_time.isoformat(),
                    'end_time': end_time.isoformat()
                })
                
            # Report success
            files_downloaded = result.get('files_downloaded', 0)
            duration = result.get('duration', 0)
            self.logger.info(f"Completed: {station_id} ({session_type}) - "
                           f"{files_downloaded} files in {duration:.1f}s")
            
        except Exception as e:
            self.logger.error(f"Download failed: {station_id} ({session_type}) - {e}")
            
            # Log failure to audit trail
            if 'audit_logger' in locals() and audit_logger:
                audit_logger.log_failure_event(station_id, {
                    'session': session_type,
                    'error_type': type(e).__name__,
                    'error_message': str(e),
                    'scheduled': True
                })
                
        finally:
            # Clean up
            if job_id in self.running_jobs:
                del self.running_jobs[job_id]
                
    def _job_executed(self, event):
        """Handle successful job execution."""
        self.logger.debug(f"Job executed: {event.job_id}")
        
    def _job_error(self, event):
        """Handle job execution errors.""" 
        self.logger.error(f"Job error: {event.job_id} - {event.exception}")
        
    def start(self):
        """Start the scheduler."""
        try:
            self.scheduler.start()
            self.logger.info("Scheduler started successfully")
        except Exception as e:
            self.logger.error(f"Failed to start scheduler: {e}")
            raise
            
    def stop(self):
        """Stop the scheduler."""
        try:
            self.scheduler.shutdown(wait=True)
            self.logger.info("Scheduler stopped")
        except Exception as e:
            self.logger.error(f"Error stopping scheduler: {e}")
            
    def get_scheduled_jobs(self) -> List[Dict[str, Any]]:
        """Get list of all scheduled jobs."""
        jobs = []
        
        for job in self.scheduler.get_jobs():
            # Handle different APScheduler versions
            next_run = getattr(job, 'next_run_time', None)
            if next_run is None:
                next_run = getattr(job, 'next_run', None)
                
            jobs.append({
                'id': job.id,
                'name': getattr(job, 'name', job.id),
                'trigger': str(job.trigger),
                'next_run': next_run.isoformat() if next_run else None,
                'args': getattr(job, 'args', [])
            })
            
        return jobs
        
    def get_job_status(self) -> Dict[str, Any]:
        """Get scheduler and job status."""
        return {
            'scheduler_running': self.scheduler.running,
            'total_jobs': len(self.scheduler.get_jobs()),
            'running_jobs': len(self.running_jobs),
            'current_jobs': list(self.running_jobs.keys())
        }


def create_scheduler_config() -> Path:
    """Create default scheduler configuration file."""
    
    config_dir = Path.home() / '.config' / 'gps_receivers'
    config_dir.mkdir(parents=True, exist_ok=True)
    
    config_file = config_dir / 'scheduler.json'
    
    default_config = {
        "database_url": "sqlite:///~/.cache/gps_receivers/scheduler.db",
        "log_dir": "~/.cache/gps_receivers/logs",
        "production_mode": True,
        "max_workers": 5,
        "sessions": {
            "15s_24hr": {
                "enabled": True,
                "schedule_minute": 10,
                "distribution_window": 10,
                "frequency": "daily",
                "max_concurrent": 3,
                "timeout_minutes": 45
            },
            "1Hz_1hr": {
                "enabled": True,
                "schedule_minute": 15,
                "distribution_window": 10, 
                "frequency": "hourly",
                "max_concurrent": 4,
                "timeout_minutes": 30
            },
            "status_1hr": {
                "enabled": True,
                "schedule_minute": 25,
                "distribution_window": 5,
                "frequency": "hourly", 
                "max_concurrent": 5,
                "timeout_minutes": 15
            }
        }
    }
    
    with open(config_file, 'w') as f:
        json.dump(default_config, f, indent=2)
        
    return config_file


# Example usage and testing
if __name__ == "__main__":
    
    if not HAS_APSCHEDULER:
        print("APScheduler not available. Install with: pip install apscheduler")
        sys.exit(1)
        
    # Create scheduler
    scheduler = BulkDownloadScheduler(production_mode=True)
    
    # Schedule all sessions
    scheduler.schedule_all_sessions()
    
    # Show scheduled jobs
    jobs = scheduler.get_scheduled_jobs()
    print(f"Scheduled {len(jobs)} jobs:")
    for job in jobs[:5]:  # Show first 5
        print(f"  {job['id']}: {job['trigger']}")
    
    print(f"\nScheduler status: {scheduler.get_job_status()}")
    
    # Note: In production, you would call scheduler.start() and keep the process running