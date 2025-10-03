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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Union
from dataclasses import dataclass

from .schedule_parser import parse_schedule, apply_distribution_window


# Module-level download function for APScheduler serialization
def _download_station_data_job(station_id: str, session_type: str, production_mode: bool = False, lookback_periods: int = 1):
    """Download data for a single station (standalone job function for APScheduler).

    This is a module-level function to allow APScheduler to serialize it to the database.
    Instance methods cannot be serialized when the instance contains non-serializable
    objects like schedulers.

    Args:
        station_id: Station identifier
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr)
        production_mode: Whether to use production logging
        lookback_periods: Number of periods to check (1=last period only, 2=last 2 periods, etc.)
    """
    job_id = f"{session_type}_{station_id}"
    exec_start_time = datetime.now(timezone.utc)

    # Set up logging
    logger = logging.getLogger(f'gps_scheduler.job.{station_id}')

    try:
        logger.info(f"Starting download: {station_id} ({session_type})")

        # Import receiver management here to avoid circular imports
        from ..cli.main import get_station_config, create_receiver
        from ..base.production_logging import setup_production_logging

        # Set up production logging
        if production_mode:
            prod_config = setup_production_logging(json_output=False, verbose=False)
            recv_logger = prod_config.create_station_logger(station_id)
            audit_logger = prod_config.get_audit_logger()
        else:
            recv_logger = logging.getLogger(f'receiver.{station_id}')
            audit_logger = None

        # Get station configuration
        station_config = get_station_config(station_id)
        if not station_config:
            raise ValueError(f"No configuration found for station {station_id}")

        # Create receiver instance
        receiver = create_receiver(station_id, station_config)

        # Determine time range based on session type and lookback_periods
        if session_type == '15s_24hr':
            # Daily data - get yesterday's data (or multiple days with lookback)
            end_time = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
            start_time = end_time - timedelta(days=lookback_periods - 1)
            frequency = '1D'
        else:
            # Hourly data - get previous complete hour's data (or multiple hours with lookback)
            # At 00:15, we want 23:00 (end) and 22:00 (start) with lookback_periods=2
            now = datetime.now(timezone.utc)
            end_time = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
            start_time = end_time - timedelta(hours=lookback_periods - 1)
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
        logger.info(f"Completed: {station_id} ({session_type}) - "
                   f"{files_downloaded} files in {duration:.1f}s")

    except Exception as e:
        logger.error(f"Download failed: {station_id} ({session_type}) - {e}")

        # Log failure to audit trail
        if 'audit_logger' in locals() and audit_logger:
            audit_logger.log_failure_event(station_id, {
                'session': session_type,
                'error_type': type(e).__name__,
                'error_message': str(e),
                'scheduled': True
            })

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
    """Configuration for scheduled downloads.

    Supports both legacy format (schedule_minute + frequency) and new flexible format (schedule).

    New flexible schedule formats:
    - Single time: "00:10" (daily at 00:10)
    - Hourly minute: ":15" (every hour at :15)
    - Interval: "6h", "45m" (every N hours/minutes)
    - Multiple times: ["00:10", "08:10", "16:10"]
    - Cron expression: "cron: */15 * * * *"

    Legacy format (still supported):
    - schedule_minute + frequency: "daily" or "hourly"
    """
    session_type: str
    distribution_window: int  # Minutes to spread downloads across
    enabled: bool = True
    max_concurrent: int = 3
    timeout_minutes: int = 30
    lookback_periods: int = 1  # Number of periods to check (1=last period only, 2=last 2 periods, etc.)

    # New flexible schedule format (preferred)
    schedule: Optional[Union[str, List[str], Dict[str, Any]]] = None

    # Legacy format fields (for backward compatibility)
    schedule_minute: Optional[int] = None
    frequency: Optional[str] = None

    def __post_init__(self):
        """Convert legacy format to new format if needed."""
        if self.schedule is None:
            # No new format specified, check for legacy format
            if self.schedule_minute is not None and self.frequency is not None:
                # Convert legacy to dict format for parsing
                self.schedule = {
                    'schedule_minute': self.schedule_minute,
                    'frequency': self.frequency
                }
            else:
                raise ValueError(
                    f"Session {self.session_type}: Must specify either 'schedule' or "
                    f"both 'schedule_minute' and 'frequency'"
                )


class BulkDownloadScheduler:
    """APScheduler-based bulk download system with full manual compatibility."""

    def __init__(self,
                 database_url: str = None,
                 log_dir: Path = None,
                 production_mode: bool = True,
                 max_workers: int = None,
                 station_filter: List[str] = None,
                 max_stations_per_session: int = None,
                 config_path: Path = None):

        if not HAS_APSCHEDULER:
            raise ImportError("APScheduler not available. Install with: pip install apscheduler")

        # Load YAML configuration (with fallback to defaults)
        from .config_loader import load_scheduler_config, get_session_config
        self.yaml_config = load_scheduler_config(config_path)

        # Apply configuration (CLI args override YAML)
        scheduler_cfg = self.yaml_config['scheduler']

        # Expand ~ in database path from YAML
        db_path = scheduler_cfg.get('database', f"{Path.home()}/.cache/gps_receivers/scheduler.db")
        if isinstance(db_path, str):
            db_path = str(Path(db_path).expanduser())
        self.database_url = database_url or f"sqlite:///{db_path}"

        # Expand ~ in log_dir path from YAML
        log_path = scheduler_cfg.get('log_dir', Path.home() / '.cache' / 'gps_receivers' / 'logs')
        if isinstance(log_path, str):
            log_path = Path(log_path).expanduser()
        self.log_dir = log_dir or log_path
        self.production_mode = production_mode
        self.max_workers = max_workers if max_workers is not None else scheduler_cfg.get('max_workers', 5)
        self.station_filter = [s.upper() for s in station_filter] if station_filter else None
        self.max_stations_per_session = max_stations_per_session

        # Set up logging
        self._setup_logging()

        # Initialize scheduler with persistent job store
        self._setup_scheduler()

        # Load schedule configurations from YAML (with defaults as fallback)
        self.schedule_configs = {}
        for session_type in ['15s_24hr', '1Hz_1hr', 'status_1hr']:
            session_cfg = self.yaml_config['sessions'].get(session_type, {})

            # Check for new flexible 'schedule' field first
            schedule = session_cfg.get('schedule')

            # If no new schedule field, use legacy format (schedule_minute + frequency)
            if schedule is None:
                schedule_minute = session_cfg.get('schedule_minute',
                    10 if session_type == '15s_24hr' else 15 if session_type == '1Hz_1hr' else 25)
                frequency = session_cfg.get('frequency',
                    'daily' if session_type == '15s_24hr' else 'hourly')

                self.schedule_configs[session_type] = ScheduleConfig(
                    session_type=session_type,
                    schedule_minute=schedule_minute,
                    frequency=frequency,
                    distribution_window=session_cfg.get('distribution_window',
                        10 if session_type != 'status_1hr' else 5),
                    enabled=session_cfg.get('enabled', True),
                    max_concurrent=session_cfg.get('max_concurrent',
                        3 if session_type == '15s_24hr' else 4 if session_type == '1Hz_1hr' else 5),
                    timeout_minutes=session_cfg.get('timeout_minutes',
                        45 if session_type == '15s_24hr' else 30 if session_type == '1Hz_1hr' else 15),
                    lookback_periods=session_cfg.get('lookback_periods', 1)
                )
            else:
                # New flexible schedule format
                self.schedule_configs[session_type] = ScheduleConfig(
                    session_type=session_type,
                    schedule=schedule,
                    distribution_window=session_cfg.get('distribution_window',
                        10 if session_type != 'status_1hr' else 5),
                    enabled=session_cfg.get('enabled', True),
                    max_concurrent=session_cfg.get('max_concurrent',
                        3 if session_type == '15s_24hr' else 4 if session_type == '1Hz_1hr' else 5),
                    timeout_minutes=session_cfg.get('timeout_minutes',
                        45 if session_type == '15s_24hr' else 30 if session_type == '1Hz_1hr' else 15),
                    lookback_periods=session_cfg.get('lookback_periods', 1)
                )

        # Load station configurations
        self.stations = self._load_station_configs()

        # Load receiver session capabilities from receivers.cfg
        self.receiver_sessions = self._load_receiver_session_capabilities()

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

    def _load_receiver_session_capabilities(self) -> Dict[str, List[str]]:
        """Load session capabilities for each receiver type from receivers.cfg.

        Returns:
            Dict mapping receiver_type (lowercase) to list of supported sessions
            Example: {'polarx5': ['15s_24hr', '1Hz_1hr', 'status_1hr'],
                     'netr9': ['15s_24hr', '1Hz_1hr']}
        """
        import configparser
        from pathlib import Path

        capabilities = {}

        try:
            # Find receivers.cfg using gps_parser (respects GPS_CONFIG_PATH)
            try:
                import gps_parser
                parser_config = gps_parser.ConfigParser()
                gps_config_dir = parser_config.config_path
                config_path = Path(gps_config_dir) / 'receivers.cfg'
            except (ImportError, Exception) as e:
                self.logger.debug(f"Could not get config dir from gps_parser: {e}")
                # Fallback to standard location
                config_path = Path.home() / '.config' / 'gpsconfig' / 'receivers.cfg'

            if not config_path.exists():
                self.logger.warning(f"receivers.cfg not found at {config_path}, all sessions will be attempted")
                return {}

            config = configparser.ConfigParser()
            config.read(config_path)

            # Check each receiver type section
            for receiver_type in ['polarx5', 'netr9', 'netrs', 'g10']:
                if receiver_type not in config:
                    continue

                sessions = []
                # Check which session_map_* keys exist
                # Note: Check both exact case and lowercase since config keys may vary
                for session in ['15s_24hr', '1Hz_1hr', 'status_1hr']:
                    key = f'session_map_{session}'
                    if key in config[receiver_type]:
                        sessions.append(session)

                capabilities[receiver_type] = sessions
                self.logger.debug(f"Receiver {receiver_type} supports sessions: {sessions}")

            self.logger.info(f"Loaded session capabilities for {len(capabilities)} receiver types")

        except Exception as e:
            self.logger.error(f"Failed to load receiver session capabilities: {e}")

        return capabilities

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
        skipped = []

        for station_id, config in self.stations.items():
            if not config.get('enabled', True):
                continue

            # Apply station filter if specified
            if self.station_filter and station_id not in self.station_filter:
                continue

            # Check if receiver type supports this session
            receiver_type = config.get('receiver_type', '').lower()
            if self.receiver_sessions and receiver_type in self.receiver_sessions:
                supported_sessions = self.receiver_sessions[receiver_type]
                if session_type not in supported_sessions:
                    skipped.append(f"{station_id}({receiver_type})")
                    continue

            stations.append(station_id)

        # Log skipped stations
        if skipped:
            self.logger.info(f"Skipped {len(skipped)} stations for {session_type} (unsupported by receiver type): {', '.join(skipped[:5])}")

        # Apply max stations limit if specified
        if self.max_stations_per_session and len(stations) > self.max_stations_per_session:
            stations = stations[:self.max_stations_per_session]
            self.logger.info(f"Limited {session_type} to {self.max_stations_per_session} stations for testing")

        return stations
        
    def _schedule_session_downloads(self,
                                  session_type: str,
                                  config: ScheduleConfig,
                                  stations: List[str]):
        """Schedule downloads for a specific session type using flexible schedule format."""

        # Parse the schedule configuration
        base_trigger = parse_schedule(config.schedule)

        for i, station_id in enumerate(stations):
            # Apply distribution window to spread stations across time
            trigger_type, trigger_kwargs = apply_distribution_window(
                base_trigger, i, len(stations), config.distribution_window
            )

            # Create job ID
            job_id = f"{session_type}_{station_id}"

            # Schedule the job with parsed trigger
            self.scheduler.add_job(
                func=_download_station_data_job,
                trigger=trigger_type,
                args=[station_id, session_type, self.production_mode, config.lookback_periods],
                id=job_id,
                replace_existing=True,
                max_instances=1,
                **trigger_kwargs
            )

        self.logger.info(
            f"Scheduled {len(stations)} stations for {session_type} "
            f"({base_trigger.description})"
        )
        
    def _download_station_data(self, station_id: str, session_type: str):
        """Download data for a single station (wrapper for backward compatibility).

        This method wraps the module-level function for backward compatibility.
        Direct scheduling uses the module-level function to avoid serialization issues.
        """
        job_id = f"{session_type}_{station_id}"
        start_time = datetime.now(timezone.utc)

        try:
            self.running_jobs[job_id] = start_time
            _download_station_data_job(station_id, session_type, self.production_mode)
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