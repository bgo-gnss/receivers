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

import fcntl
import logging
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Union
from dataclasses import dataclass

from .schedule_parser import parse_schedule, apply_distribution_window

# Module-level reference for config watcher job (APScheduler needs module-level functions)
_scheduler_instance: Optional["BulkDownloadScheduler"] = None


def _check_config_changes_job() -> None:
    """Check for stations.cfg changes (standalone job function for APScheduler)."""
    if _scheduler_instance is not None:
        _scheduler_instance._check_config_changes()


def _write_connectivity_status(station_id: str, health_data: Dict[str, Any], logger: logging.Logger) -> None:
    """Write ping and port status to database for Grafana dashboard.

    Delegates to shared ConnectivityWriter module. Uses health data timestamp
    instead of NOW() for consistent time alignment across block tables.

    Args:
        station_id: Station identifier
        health_data: Health data dictionary with connection info
        logger: Logger instance
    """
    from ..health.connectivity_writer import ConnectivityWriter

    writer = ConnectivityWriter(logger)
    writer.write_connectivity_status(station_id, health_data)


# Module-level download function for APScheduler serialization
def _download_station_data_job(station_id: str, session_type: str, production_mode: bool = False, lookback_periods: int = 1, timeout_minutes: int = 30, run_rinex: bool = False):
    """Download data for a single station (standalone job function for APScheduler).

    This is a module-level function to allow APScheduler to serialize it to the database.
    Instance methods cannot be serialized when the instance contains non-serializable
    objects like schedulers.

    Args:
        station_id: Station identifier
        session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr)
        production_mode: Whether to use production logging
        lookback_periods: Number of periods to check (1=last period only, 2=last 2 periods, etc.)
        timeout_minutes: Maximum job duration in minutes (for monitoring and eventual enforcement)
        run_rinex: Whether to run RINEX conversion after download
    """
    job_id = f"{session_type}_{station_id}"
    exec_start_time = datetime.now(timezone.utc)

    # Set up logging
    logger = logging.getLogger(f'gps_scheduler.job.{station_id}')

    try:
        # Track job start time for duration monitoring
        job_start_time = time.time()

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
        # Use time_utils for consistent time calculation between CLI and scheduler
        from ..utils.time_utils import calculate_download_time_range
        start_time, end_time = calculate_download_time_range(session_type, lookback_periods)

        if session_type == '15s_24hr':
            frequency = '1D'
        else:
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
            reverse_chronological=True,  # Prioritize latest data (like -D flag)
            loglevel=logging.INFO
        )

        # Check result status to determine success/failure
        status = result.get('status', 'completed')
        files_downloaded = result.get('files_downloaded', 0)
        duration = result.get('duration', 0)

        # Calculate total job duration for monitoring
        job_duration_seconds = time.time() - job_start_time
        job_duration_minutes = job_duration_seconds / 60

        # Monitor job duration relative to configured timeout
        timeout_threshold = timeout_minutes * 0.8  # 80% threshold for warnings
        if job_duration_minutes > timeout_threshold:
            percent_of_timeout = (job_duration_minutes / timeout_minutes) * 100
            logger.warning(
                f"⏱️  Long-running job: {station_id} ({session_type}) took {job_duration_minutes:.1f}min "
                f"({percent_of_timeout:.0f}% of {timeout_minutes}min timeout, {files_downloaded} files)"
            )

        # Log results to audit trail
        if audit_logger:
            audit_logger.log_download_session(station_id, {
                'session': session_type,
                'status': status,
                'duration': duration,
                'job_duration': job_duration_seconds,
                'files_downloaded': files_downloaded,
                'bytes_downloaded': result.get('total_bytes', 0),
                'errors': result.get('errors', 0),
                'scheduled': True,
                'start_time': start_time.isoformat(),
                'end_time': end_time.isoformat(),
                'timeout_minutes': timeout_minutes,
                'timeout_percent': (job_duration_minutes / timeout_minutes) * 100 if timeout_minutes > 0 else 0
            })

        # Report based on actual status with emoji-based logging style
        if status == 'failed':
            # Download returned failed status (connection error, timeout, etc.)
            error_msg = result.get('error_message', 'Unknown error')
            logger.error(f"❌ Failed: {station_id} ({session_type}) - {error_msg} ({duration:.1f}s)")
        elif status == 'up_to_date':
            # All files already synced - this is success
            logger.info(f"✅ Up-to-date: {station_id} ({session_type}) - {files_downloaded} files in {duration:.1f}s")
        else:
            # Completed with downloads or dry_run
            if files_downloaded > 0:
                logger.info(f"✅ Completed: {station_id} ({session_type}) - {files_downloaded} files in {duration:.1f}s")
            else:
                logger.info(f"✅ Completed: {station_id} ({session_type}) - 0 files (already synced) in {duration:.1f}s")

        # Run RINEX conversion if enabled and download was successful
        if run_rinex and status != 'failed':
            archived_files = result.get('archived_files', [])
            if archived_files:
                _run_rinex_conversion(station_id, session_type, archived_files, station_config, logger)

    except Exception as e:
        # Unexpected exception during download
        error_type = type(e).__name__
        logger.error(f"❌ Exception: {station_id} ({session_type}) - {error_type}: {e}")

        # Log failure to audit trail
        if 'audit_logger' in locals() and audit_logger:
            audit_logger.log_failure_event(station_id, {
                'session': session_type,
                'error_type': type(e).__name__,
                'error_message': str(e),
                'scheduled': True
            })


def _run_rinex_conversion(station_id: str, session_type: str, raw_files: List[str], station_config: Dict[str, Any], logger: logging.Logger):
    """Run RINEX conversion on downloaded files.

    Args:
        station_id: Station identifier
        session_type: Session type (15s_24hr, 1Hz_1hr)
        raw_files: List of paths to raw files to convert
        station_config: Station configuration dictionary
        logger: Logger instance
    """
    try:
        from .tasks.rinex_task import RINEXTask
        from .task_interface import TaskConfig, TaskType, TaskFrequency

        logger.info(f"🔄 Starting RINEX conversion: {station_id} ({len(raw_files)} files)")
        start_time = time.time()

        # Create task config
        config = TaskConfig(
            task_type=TaskType.RINEX,
            session_type=session_type,
            schedule_minute=0,
            distribution_window=10,
            frequency=TaskFrequency.HOURLY if session_type == '1Hz_1hr' else TaskFrequency.DAILY,
            lookback_periods=1,
            max_concurrent=1,
            timeout_minutes=30,
        )

        # Create and execute RINEX task
        task = RINEXTask(
            station_id=station_id,
            config=config,
            logger=logger,
            input_files=raw_files,
            rinex_version=3,
            apply_hatanaka=True,
            apply_header_corrections=True,
        )

        result = task.execute()
        duration = time.time() - start_time

        if result.success:
            files_converted = result.data.get('files_converted', 0)
            logger.info(f"✅ RINEX complete: {station_id} - {files_converted} files in {duration:.1f}s")
        else:
            logger.warning(f"⚠️  RINEX partial/failed: {station_id} - {result.message}")

    except ImportError as e:
        logger.warning(f"⚠️  RINEX not available: {e}")
    except Exception as e:
        logger.error(f"❌ RINEX failed: {station_id} - {type(e).__name__}: {e}")


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


# Module-level status check function for APScheduler serialization
def _status_check_job(station_id: str, send_to_db: bool = True, send_to_icinga: bool = True):
    """Run health status check for a station (standalone job function for APScheduler).

    Uses the same code path as: receivers health STATION --save-db --icinga
    by calling gather_comprehensive_health() rather than receiver.get_health_status()
    directly. This ensures NTRIP checks, file status, and power_type handling
    are included.

    Args:
        station_id: Station identifier
        send_to_db: Write health data to PostgreSQL
        send_to_icinga: Send passive checks to Icinga
    """
    logger = logging.getLogger(f'gps_scheduler.health.{station_id}')

    try:
        logger.info(f"Starting health check: {station_id}")
        start_time = time.time()

        # Import here to avoid circular imports
        from ..cli.main import get_station_config, create_receiver
        from ..health.live_health import gather_comprehensive_health

        # Get station configuration
        station_config = get_station_config(station_id)
        if not station_config:
            logger.error(f"❌ Health check failed: No config for {station_id}")
            return

        # Create receiver
        receiver = create_receiver(station_id, station_config)

        # Get comprehensive health — same as CLI 'receivers health' command
        try:
            health_data = gather_comprehensive_health(
                station_id, station_config, receiver,
                include_files=False,
                include_ntrip=True,
            )
        except Exception as e:
            logger.warning(f"Could not get live health from {station_id}: {e}")
            health_data = {'station_id': station_id, 'error': str(e)}

        # Write to PostgreSQL
        db_success = False
        if send_to_db and health_data:
            try:
                from ..health.db_writer import HealthDatabaseWriter
                writer = HealthDatabaseWriter()
                db_success = writer.write_health_data(health_data)
                if db_success:
                    logger.debug(f"Health data written to database for {station_id}")
                    # Also write ping and port status for Grafana dashboard
                    _write_connectivity_status(station_id, health_data, logger)
            except ImportError:
                logger.debug("PostgreSQL writer not available")
            except Exception as e:
                logger.warning(f"Database write failed for {station_id}: {e}")

        # Send to Icinga
        icinga_sent = 0
        if send_to_icinga and health_data:
            try:
                from ..monitoring.icinga_client import IcingaClient
                client = IcingaClient()
                results = client.send_health_from_json(health_data)
                icinga_sent = sum(1 for r in results.values() if r.get('success', False))
            except ImportError:
                logger.debug("Icinga client not available")
            except Exception as e:
                logger.warning(f"Icinga send failed for {station_id}: {e}")

        duration = time.time() - start_time
        status_parts = []
        if db_success:
            status_parts.append("DB")
        if icinga_sent > 0:
            status_parts.append(f"Icinga({icinga_sent})")

        status_str = ", ".join(status_parts) if status_parts else "no targets"
        logger.info(f"✅ Health check complete: {station_id} - {status_str} ({duration:.1f}s)")

    except Exception as e:
        logger.error(f"❌ Health check failed: {station_id} - {type(e).__name__}: {e}")


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
    rinex: bool = False  # Whether to run RINEX conversion after download

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
                 config_path: Path = None,
                 scheduler_types: List[str] = None):

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
        self.max_workers = max_workers if max_workers is not None else scheduler_cfg.get('max_workers', 15)
        self.station_filter = [s.upper() for s in station_filter] if station_filter else None
        self.max_stations_per_session = max_stations_per_session

        # Parse scheduler_types filter
        # Valid types: health, 15s_24hr, 1Hz_1hr, status_1hr, downloads (all download sessions), all
        self.scheduler_types = self._parse_scheduler_types(scheduler_types)

        # PID lock file to prevent duplicate instances
        lock_dir = Path(db_path).parent
        lock_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = lock_dir / "scheduler.lock"
        self._lock_fd = None

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
                    lookback_periods=session_cfg.get('lookback_periods', 1),
                    rinex=session_cfg.get('rinex', False)
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
                    lookback_periods=session_cfg.get('lookback_periods', 1),
                    rinex=session_cfg.get('rinex', False)
                )

        # Load station configurations
        self.stations = self._load_station_configs()

        # Load receiver session capabilities from receivers.cfg
        self.receiver_sessions = self._load_receiver_session_capabilities()

        # Track running jobs
        self.running_jobs = {}

        # Set module-level reference for config watcher job
        global _scheduler_instance
        _scheduler_instance = self

    def _parse_scheduler_types(self, scheduler_types: List[str] = None) -> dict:
        """Parse scheduler types filter into a structured dict.

        Args:
            scheduler_types: List of types like ['health', '15s_24hr'] or ['downloads', 'health']

        Returns:
            Dict with keys: health, 15s_24hr, 1Hz_1hr, status_1hr (all True/False)
        """
        # Default: all enabled
        result = {
            'health': True,
            '15s_24hr': True,
            '1Hz_1hr': True,
            'status_1hr': True,
        }

        if scheduler_types is None or 'all' in scheduler_types:
            return result

        # Start with all disabled
        result = {k: False for k in result}

        for stype in scheduler_types:
            stype = stype.lower().strip()

            if stype == 'health':
                result['health'] = True
            elif stype == 'downloads':
                # Enable all download sessions
                result['15s_24hr'] = True
                result['1Hz_1hr'] = True
                result['status_1hr'] = True
            elif stype in ['15s_24hr', '15s', 'daily']:
                result['15s_24hr'] = True
            elif stype in ['1hz_1hr', '1hz', 'hourly']:
                result['1Hz_1hr'] = True
            elif stype in ['status_1hr', 'status']:
                result['status_1hr'] = True

        return result

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
            'max_instances': 70,  # Allow 70 concurrent instances per job for stress testing
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
                station_status = config.get('station_status')
                health_check = config.get('health_check')
                receiver_type = config.get('receiver_type', 'unknown')

                # Auto-detect inactive stations: flag if receiver_type is genuinely absent
                if not station_status:
                    rx_missing = receiver_type.lower() in ('none', '', 'unknown')
                    if rx_missing:
                        station_status = 'inactive'

                stations[station_id] = {
                    'station_id': station_id,
                    'receiver_type': receiver_type,
                    'ip_number': config.get('ip_number', ''),
                    'ip_port': config.get('ip_port', 21),
                    'enabled': config.get('enabled', True),
                    'timeout_category': config.get('timeout_category', 'default'),
                    'station_status': station_status,
                    'health_check': health_check,
                }

        except Exception as e:
            self.logger.error(f"Failed to load station configurations: {e}")
            # Fallback: empty station list
            stations = {}

        self.logger.info(f"Loaded {len(stations)} station configurations")
        return stations

    def _sync_station_status_to_db(self) -> None:
        """Sync station_status and health_check values from config to the database.

        Two separate fields:
        - station_status: lifecycle (NULL=active, discontinued, inactive)
        - health_check: monitoring mode (NULL=active, passive)

        This runs at startup and when config file changes are detected.
        """
        try:
            from ..health.database_factory import DatabaseConnectionFactory

            with DatabaseConnectionFactory.connection() as conn:
                with conn.cursor() as cur:
                    status_synced = 0
                    hc_synced = 0
                    for station_id, config in self.stations.items():
                        station_status = config.get('station_status')
                        health_check = config.get('health_check')
                        cur.execute("""
                            UPDATE stations
                            SET station_status = %s, health_check = %s
                            WHERE sid = %s
                              AND (station_status IS DISTINCT FROM %s
                                OR health_check IS DISTINCT FROM %s)
                        """, (station_status, health_check, station_id,
                              station_status, health_check))
                        if cur.rowcount > 0:
                            if station_status:
                                status_synced += 1
                            if health_check:
                                hc_synced += 1

                    if status_synced or hc_synced:
                        self.logger.info(
                            f"Synced to DB: {status_synced} station_status, {hc_synced} health_check"
                        )
                    else:
                        self.logger.debug("station_status/health_check already in sync with DB")

        except ImportError:
            self.logger.debug("psycopg2 not available — skipping status sync")
        except Exception as e:
            self.logger.warning(f"Failed to sync station status to DB: {e}")

    def _get_stations_cfg_path(self) -> Optional[Path]:
        """Get the path to stations.cfg using gps_parser."""
        try:
            import gps_parser
            parser = gps_parser.ConfigParser()
            return Path(parser.get_stations_config_path())
        except Exception:
            return None

    def _check_config_changes(self) -> None:
        """Check if stations.cfg has been modified and reload if so.

        Scheduled as a periodic job. Compares file mtime to detect changes.
        When a change is found, reloads station configs and syncs to DB.
        """
        cfg_path = self._get_stations_cfg_path()
        if not cfg_path or not cfg_path.exists():
            return

        try:
            current_mtime = cfg_path.stat().st_mtime
        except OSError:
            return

        if not hasattr(self, '_config_mtime'):
            self._config_mtime = current_mtime
            return

        if current_mtime == self._config_mtime:
            return

        self.logger.info(
            f"stations.cfg changed (mtime {self._config_mtime:.0f} → {current_mtime:.0f}), "
            f"reloading station configs"
        )
        self._config_mtime = current_mtime

        old_stations = self.stations
        self.stations = self._load_station_configs()
        self._sync_station_status_to_db()

        # Log meaningful changes
        new_ids = set(self.stations) - set(old_stations)
        removed_ids = set(old_stations) - set(self.stations)
        changed = []
        for sid in set(self.stations) & set(old_stations):
            old_ss = old_stations[sid].get('station_status')
            new_ss = self.stations[sid].get('station_status')
            old_hc = old_stations[sid].get('health_check')
            new_hc = self.stations[sid].get('health_check')
            if old_ss != new_ss:
                changed.append(f"{sid}: status {old_ss or 'active'} → {new_ss or 'active'}")
            if old_hc != new_hc:
                changed.append(f"{sid}: health_check {old_hc or 'active'} → {new_hc or 'active'}")

        if new_ids:
            self.logger.info(f"New stations: {', '.join(sorted(new_ids))}")
        if removed_ids:
            self.logger.info(f"Removed stations: {', '.join(sorted(removed_ids))}")
        if changed:
            self.logger.info(f"Config changes: {'; '.join(changed)}")

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
                # Note: ConfigParser keys are case-insensitive, but we need to check the actual keys
                # because receivers.cfg uses lowercase 'hz' (session_map_1hz_1hr) while our
                # session name uses mixed case 'Hz' (1Hz_1hr)
                for session in ['15s_24hr', '1Hz_1hr', 'status_1hr']:
                    # Try both the session name as-is and lowercase version
                    key = f'session_map_{session}'
                    key_lower = f'session_map_{session.lower()}'

                    # Check if either version exists in config
                    if key in config[receiver_type] or key_lower in config[receiver_type]:
                        sessions.append(session)

                capabilities[receiver_type] = sessions
                self.logger.debug(f"Receiver {receiver_type} supports sessions: {sessions}")

            self.logger.info(f"Loaded session capabilities for {len(capabilities)} receiver types")

        except Exception as e:
            self.logger.error(f"Failed to load receiver session capabilities: {e}")

        return capabilities

    def schedule_all_sessions(self):
        """Schedule all configured download sessions with interleaved job creation.

        Creates jobs in round-robin order by station to ensure all session types
        are distributed evenly in the job queue when using interval triggers.

        Order: AFST(15s, 1Hz, status) → ALFD(15s, 1Hz, status) → ...
        Not: 15s(AFST,ALFD,...) → 1Hz(AFST,ALFD,...) → status(...)
        """

        # Build station lists for each session type
        session_stations = {}
        for session_type, config in self.schedule_configs.items():
            # Check scheduler_types filter
            if not self.scheduler_types.get(session_type, True):
                self.logger.info(f"Skipping session (--only filter): {session_type}")
                continue

            if not config.enabled:
                self.logger.info(f"Skipping disabled session: {session_type}")
                continue

            stations_for_session = self._get_stations_for_session(session_type)
            if not stations_for_session:
                self.logger.warning(f"No stations configured for session: {session_type}")
                continue

            session_stations[session_type] = stations_for_session

        # Create jobs in interleaved order (all sessions for station1, then station2, etc.)
        # This ensures when interval triggers fire all jobs simultaneously, the queue
        # contains a mix of all session types, not just the first session type
        all_stations = set()
        for stations in session_stations.values():
            all_stations.update(stations)
        all_stations = sorted(all_stations)  # Consistent ordering

        total_jobs = 0
        for station_id in all_stations:
            # Schedule all session types for this station
            for session_type, stations in session_stations.items():
                if station_id not in stations:
                    continue

                config = self.schedule_configs[session_type]
                station_index = stations.index(station_id)

                # Parse schedule and apply distribution window
                base_trigger = parse_schedule(config.schedule)
                trigger_type, trigger_kwargs = apply_distribution_window(
                    base_trigger, station_index, len(stations), config.distribution_window
                )

                # Create job
                job_id = f"{session_type}_{station_id}"
                self.scheduler.add_job(
                    func=_download_station_data_job,
                    trigger=trigger_type,
                    args=[station_id, session_type, self.production_mode, config.lookback_periods, config.timeout_minutes, config.rinex],
                    id=job_id,
                    replace_existing=True,
                    **trigger_kwargs
                )
                total_jobs += 1

        # Log summary
        for session_type, stations in session_stations.items():
            base_trigger = parse_schedule(self.schedule_configs[session_type].schedule)
            self.logger.info(
                f"Scheduled {len(stations)} stations for {session_type} "
                f"({base_trigger.description})"
            )

        self.logger.info(f"Total: {total_jobs} jobs scheduled with interleaved ordering for stress testing")

        # Schedule health monitoring if enabled
        self._schedule_health_monitoring()

        # Sync station_status values to DB at startup
        self._sync_station_status_to_db()

        # Schedule config file watcher (every 5 minutes)
        self._schedule_config_watcher()

    def _schedule_config_watcher(self) -> None:
        """Schedule periodic config file change detection."""
        # Initialize mtime tracking
        cfg_path = self._get_stations_cfg_path()
        if cfg_path and cfg_path.exists():
            try:
                self._config_mtime = cfg_path.stat().st_mtime
                self.logger.debug(f"Tracking config changes: {cfg_path}")
            except OSError:
                pass

        self.scheduler.add_job(
            func=_check_config_changes_job,
            trigger='interval',
            minutes=5,
            id='config_watcher',
            replace_existing=True,
        )
        self.logger.info("Scheduled config watcher (every 5 min)")

    def _get_stations_for_session(self, session_type: str) -> List[str]:
        """Get list of stations that support a specific session type."""

        stations = []
        skipped = []

        for station_id, config in self.stations.items():
            if not config.get('enabled', True):
                continue

            # Skip non-active stations (lifecycle or monitoring mode)
            if config.get('station_status') in ('discontinued', 'inactive'):
                continue
            if config.get('health_check') == 'passive':
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

            # Schedule the job with parsed trigger (uses job_defaults max_instances=70)
            self.scheduler.add_job(
                func=_download_station_data_job,
                trigger=trigger_type,
                args=[station_id, session_type, self.production_mode, config.lookback_periods, config.timeout_minutes, config.rinex],
                id=job_id,
                replace_existing=True,
                **trigger_kwargs
            )

        self.logger.info(
            f"Scheduled {len(stations)} stations for {session_type} "
            f"({base_trigger.description})"
        )
        
    def _schedule_health_monitoring(self):
        """Schedule health monitoring jobs (send to Icinga + PostgreSQL).

        Health checks run every 5 minutes for all stations that support live health.
        Equivalent to: receivers health STATION --icinga --save-db
        """
        # Check scheduler_types filter
        if not self.scheduler_types.get('health', True):
            self.logger.info("Health monitoring skipped (--only filter)")
            return

        # Check if health monitoring is enabled in config
        status_monitoring = self.yaml_config.get('status_monitoring', {})
        if not status_monitoring.get('enabled', True):
            self.logger.info("Health monitoring disabled in config")
            return

        # Get schedule (default: every 5 minutes)
        schedule = status_monitoring.get('schedule', '5m')

        # Get stations that support health checks (all receiver types with get_health_status)
        # Supported: PolaRX5, NetR9, NetRS, NetR5, G10
        supported_health_types = {'polarx5', 'netr9', 'netrs', 'netr5', 'g10'}
        health_stations = []
        skipped_stations = []
        for station_id, config in self.stations.items():
            if not config.get('enabled', True):
                continue

            # Skip non-active stations (lifecycle or monitoring mode)
            station_status = config.get('station_status')
            health_check = config.get('health_check')
            if station_status in ('discontinued', 'inactive'):
                skipped_stations.append(station_id)
                continue
            if health_check == 'passive':
                skipped_stations.append(station_id)
                continue

            # Apply station filter if specified
            if self.station_filter and station_id not in self.station_filter:
                continue

            # Check if receiver type supports health checks
            receiver_type = config.get('receiver_type', '').lower()
            if receiver_type not in supported_health_types:
                continue

            health_stations.append(station_id)

        if skipped_stations:
            self.logger.info(
                f"Skipping {len(skipped_stations)} discontinued/passive stations: "
                f"{', '.join(sorted(skipped_stations))}"
            )

        if not health_stations:
            self.logger.info("No stations support health monitoring")
            return

        # Apply max stations limit
        if self.max_stations_per_session and len(health_stations) > self.max_stations_per_session:
            health_stations = health_stations[:self.max_stations_per_session]

        # Parse schedule
        base_trigger = parse_schedule(schedule)

        # Distribution window for health checks (default 3 minutes)
        distribution_window = status_monitoring.get('distribution_window', 3)

        # Schedule health check jobs
        for i, station_id in enumerate(sorted(health_stations)):
            trigger_type, trigger_kwargs = apply_distribution_window(
                base_trigger, i, len(health_stations), distribution_window
            )

            job_id = f"health_{station_id}"
            self.scheduler.add_job(
                func=_status_check_job,
                trigger=trigger_type,
                args=[station_id, True, True],  # send_to_db=True, send_to_icinga=True
                id=job_id,
                replace_existing=True,
                **trigger_kwargs
            )

        self.logger.info(
            f"Scheduled {len(health_stations)} stations for health monitoring "
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
        
    def _acquire_lock(self) -> None:
        """Acquire an exclusive file lock to prevent duplicate scheduler instances.

        Raises:
            RuntimeError: If another scheduler instance is already running.
        """
        existing_pid = ""
        try:
            # Open for reading+writing so we can read existing PID before overwriting
            self._lock_fd = open(self._lock_path, "a+")
            self._lock_fd.seek(0)
            existing_pid = self._lock_fd.read().strip()
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self._lock_fd:
                self._lock_fd.close()
            self._lock_fd = None
            raise RuntimeError(
                f"Another scheduler instance is already running (PID {existing_pid}). "
                f"Lock file: {self._lock_path}"
            )
        # Write our PID for diagnostics
        self._lock_fd.seek(0)
        self._lock_fd.truncate()
        self._lock_fd.write(str(os.getpid()))
        self._lock_fd.flush()

    def _release_lock(self) -> None:
        """Release the file lock and clean up."""
        if self._lock_fd:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except OSError:
                pass
            self._lock_fd = None
            try:
                self._lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def start(self):
        """Start the scheduler.

        Acquires an exclusive lock to prevent duplicate instances.
        """
        self._acquire_lock()
        try:
            self.scheduler.start()
            self.logger.info(f"Scheduler started successfully (PID {os.getpid()})")
        except Exception as e:
            self._release_lock()
            self.logger.error(f"Failed to start scheduler: {e}")
            raise

    def stop(self):
        """Stop the scheduler and release the lock."""
        try:
            self.scheduler.shutdown(wait=True)
            self.logger.info("Scheduler stopped")
        except Exception as e:
            self.logger.error(f"Error stopping scheduler: {e}")
        finally:
            self._release_lock()
            
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
    """DEPRECATED: Use config_loader.create_default_config_file() instead.

    This function created JSON config at ~/.config/gps_receivers/scheduler.json.
    The new function creates YAML config at ~/.config/gpsconfig/scheduler.yaml.
    """
    import warnings
    warnings.warn(
        "create_scheduler_config() is deprecated. "
        "Use receivers.scheduling.config_loader.create_default_config_file() instead.",
        DeprecationWarning,
        stacklevel=2
    )
    from .config_loader import create_default_config_file
    return create_default_config_file()


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