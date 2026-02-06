"""Pipeline scheduler for multi-stage task orchestration.

Extends BulkDownloadScheduler with:
- Pipeline support: Download → RINEX → Sync sequences
- Resource pools: Separate network and CPU pools
- Priority system: Real-time vs backfill scheduling
- Crash recovery: Resume incomplete pipelines

Usage:
    scheduler = PipelineScheduler()
    scheduler.schedule_all_sessions()  # Schedule pipelines, not just downloads
    scheduler.start()
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bulk_scheduler import BulkDownloadScheduler
from .pipeline import PipelineJob, PipelineStage, PipelineStateStore, StageStatus
from .resource_pools import ResourcePoolManager
from .task_interface import TaskConfig, TaskFactory, TaskPriority, TaskType


logger = logging.getLogger(__name__)


# Default pipeline configurations per session type
DEFAULT_PIPELINES = {
    '15s_24hr': {
        'stages': [PipelineStage.DOWNLOAD, PipelineStage.RINEX, PipelineStage.SYNC],
        'priority': TaskPriority.STANDARD,
        'sync_types': ['raw', 'rinex'],
    },
    '1Hz_1hr': {
        'stages': [PipelineStage.DOWNLOAD, PipelineStage.SYNC],
        'priority': TaskPriority.REALTIME,
        'sync_types': ['raw'],  # No RINEX for high-rate
    },
    'status_1hr': {
        'stages': [PipelineStage.DOWNLOAD, PipelineStage.HEALTH],
        'priority': TaskPriority.STANDARD,
        'sync_types': [],
    },
}


class PipelineScheduler(BulkDownloadScheduler):
    """Extended scheduler with pipeline orchestration and resource pools.

    Key features:
    - Pipelines: Multi-stage processing (download → rinex → sync)
    - Resource pools: Separate network/CPU pools
    - Priority: Real-time vs backfill task ordering
    - Recovery: Resume incomplete pipelines after crash
    """

    def __init__(
        self,
        database_url: str = None,
        log_dir: Path = None,
        production_mode: bool = True,
        max_workers: int = None,
        station_filter: List[str] = None,
        max_stations_per_session: int = None,
        config_path: Path = None,
        enable_pipelines: bool = True,
        resource_pool_config: Optional[Dict[str, Any]] = None,
    ):
        """Initialize pipeline scheduler.

        Args:
            database_url: SQLite database URL for job persistence
            log_dir: Directory for log files
            production_mode: Use production logging
            max_workers: Maximum concurrent workers
            station_filter: List of station IDs to include
            max_stations_per_session: Limit stations per session (for testing)
            config_path: Path to scheduler.yaml
            enable_pipelines: Enable pipeline mode (vs simple download)
            resource_pool_config: Configuration for resource pools
        """
        # Initialize base scheduler
        super().__init__(
            database_url=database_url,
            log_dir=log_dir,
            production_mode=production_mode,
            max_workers=max_workers,
            station_filter=station_filter,
            max_stations_per_session=max_stations_per_session,
            config_path=config_path,
        )

        self.enable_pipelines = enable_pipelines

        # Initialize resource pools
        pool_config = resource_pool_config or self._load_pool_config()
        self.resource_pools = ResourcePoolManager(pool_config)

        # Initialize pipeline state store
        pipeline_db = self.log_dir / 'pipeline.db'
        self.pipeline_store = PipelineStateStore(pipeline_db)

        # Pipeline configurations (from YAML or defaults)
        self.pipeline_configs = self._load_pipeline_configs()

        # Track active pipelines
        self.active_pipelines: Dict[str, PipelineJob] = {}

        self.logger.info(
            f"PipelineScheduler initialized: pipelines={enable_pipelines}, "
            f"pool_config={pool_config}"
        )

    def _load_pool_config(self) -> Dict[str, Any]:
        """Load resource pool configuration from YAML."""
        pools = self.yaml_config.get('resource_pools', {})
        return {
            'network_workers': pools.get('network_workers', 10),
            'cpu_workers': pools.get('cpu_workers', 4),
        }

    def _load_pipeline_configs(self) -> Dict[str, Dict]:
        """Load pipeline configurations from YAML or use defaults."""
        yaml_pipelines = self.yaml_config.get('pipelines', {})

        configs = {}
        for session_type, default in DEFAULT_PIPELINES.items():
            yaml_cfg = yaml_pipelines.get(session_type, {})

            # Parse stages
            stage_names = yaml_cfg.get('stages', [s.value for s in default['stages']])
            stages = [PipelineStage(s) for s in stage_names]

            # Parse priority
            priority_name = yaml_cfg.get('priority', default['priority'].name)
            if isinstance(priority_name, str):
                priority = TaskPriority[priority_name.upper()]
            else:
                priority = default['priority']

            configs[session_type] = {
                'stages': stages,
                'priority': priority,
                'sync_types': yaml_cfg.get('sync_types', default.get('sync_types', [])),
                'rinex_timing': yaml_cfg.get('rinex_timing', 'immediate'),
            }

        return configs

    def start(self):
        """Start the scheduler and resource pools."""
        # Start resource pools
        self.resource_pools.start()

        # Recover incomplete pipelines
        self._recover_incomplete_pipelines()

        # Start APScheduler
        super().start()

        self.logger.info("Pipeline scheduler started")

    def stop(self):
        """Stop the scheduler and resource pools."""
        # Stop APScheduler first
        super().stop()

        # Shutdown resource pools
        self.resource_pools.shutdown(wait=True)

        self.logger.info("Pipeline scheduler stopped")

    def schedule_pipeline(
        self,
        station_id: str,
        session_type: str,
        target_time: datetime,
        priority: Optional[TaskPriority] = None,
    ) -> PipelineJob:
        """Schedule a complete pipeline for a station.

        Args:
            station_id: Station identifier
            session_type: Session type (15s_24hr, 1Hz_1hr, status_1hr)
            target_time: Target data time
            priority: Optional priority override

        Returns:
            Created PipelineJob
        """
        config = self.pipeline_configs.get(session_type, DEFAULT_PIPELINES.get(session_type))
        if config is None:
            raise ValueError(f"Unknown session type: {session_type}")

        # Create pipeline job
        job = PipelineJob.create(
            station_id=station_id,
            session_type=session_type,
            target_time=target_time,
            enabled_stages=config['stages'],
            priority=priority or config['priority'],
        )

        # Save to state store
        self.pipeline_store.save_job(job)
        self.active_pipelines[job.job_id] = job

        # Start first stage
        self._schedule_next_stage(job)

        self.logger.info(
            f"Created pipeline: {job.job_id} with stages {[s.value for s in config['stages']]}"
        )

        return job

    def _schedule_next_stage(self, job: PipelineJob) -> None:
        """Schedule the next runnable stage for a pipeline.

        Args:
            job: Pipeline job to advance
        """
        runnable = job.get_runnable_stages()
        if not runnable:
            if job.is_complete():
                self.logger.info(f"Pipeline complete: {job.job_id}")
                self._cleanup_pipeline(job)
            return

        for stage in runnable:
            self._execute_stage(job, stage)

    def _execute_stage(self, job: PipelineJob, stage: PipelineStage) -> None:
        """Execute a pipeline stage.

        Args:
            job: Pipeline job
            stage: Stage to execute
        """
        job.mark_stage_started(stage)
        self.pipeline_store.save_job(job)

        # Get resource pool based on stage
        if stage == PipelineStage.RINEX:
            pool = 'cpu'
        else:
            pool = 'network'

        # Create callback for completion
        def on_complete(result):
            self._on_stage_complete(job, stage, result)

        # Submit to appropriate pool
        if pool == 'cpu':
            future = self.resource_pools.submit_cpu(
                self._run_stage,
                job,
                stage,
                priority=job.priority,
                task_id=f"{job.job_id}_{stage.value}"
            )
        else:
            future = self.resource_pools.submit_network(
                self._run_stage,
                job,
                stage,
                priority=job.priority,
                task_id=f"{job.job_id}_{stage.value}"
            )

        future.add_done_callback(lambda f: on_complete(f.result() if not f.exception() else None))

    def _run_stage(self, job: PipelineJob, stage: PipelineStage) -> Dict[str, Any]:
        """Run a pipeline stage and return result.

        Args:
            job: Pipeline job
            stage: Stage to run

        Returns:
            Stage result dictionary
        """
        try:
            # Get input files from previous stage
            input_files = []
            if stage == PipelineStage.RINEX:
                input_files = job.get_stage_output_files(PipelineStage.DOWNLOAD)
            elif stage == PipelineStage.SYNC:
                input_files = job.get_stage_output_files(PipelineStage.DOWNLOAD)
                if PipelineStage.RINEX in job.stages:
                    input_files.extend(job.get_stage_output_files(PipelineStage.RINEX))
            elif stage == PipelineStage.HEALTH:
                input_files = job.get_stage_output_files(PipelineStage.DOWNLOAD)

            # Map stage to task type
            task_type_map = {
                PipelineStage.DOWNLOAD: TaskType.DOWNLOAD,
                PipelineStage.RINEX: TaskType.RINEX,
                PipelineStage.SYNC: TaskType.SYNC,
                PipelineStage.HEALTH: TaskType.HEALTH,
            }

            task_type = task_type_map[stage]

            # Create task config
            from .task_interface import TaskFrequency
            config = TaskConfig(
                task_type=task_type,
                session_type=job.session_type,
                schedule_minute=0,  # Not used for direct execution
                distribution_window=0,
                frequency=TaskFrequency.MANUAL,
                priority=job.priority,
            )

            # Register tasks if needed
            if not TaskFactory.get_registered_types():
                TaskFactory.register_builtin_tasks()

            # Create and execute task
            task = TaskFactory.create(
                task_type=task_type,
                station_id=job.station_id,
                config=config,
            )

            # Set input files if task supports it
            if hasattr(task, 'input_files') and hasattr(task, '__dict__'):
                task.__dict__['input_files'] = input_files

            result = task.execute()

            return {
                'success': result.success,
                'output_files': result.output_files or [],
                'metrics': result.metrics or {},
                'error': result.error,
            }

        except Exception as e:
            self.logger.error(f"Stage {stage.value} failed for {job.job_id}: {e}")
            return {
                'success': False,
                'output_files': [],
                'metrics': {},
                'error': str(e),
            }

    def _on_stage_complete(
        self,
        job: PipelineJob,
        stage: PipelineStage,
        result: Optional[Dict[str, Any]],
    ) -> None:
        """Handle stage completion callback.

        Args:
            job: Pipeline job
            stage: Completed stage
            result: Stage result
        """
        if result is None or not result.get('success'):
            error = result.get('error', 'Unknown error') if result else 'Task failed'
            job.mark_stage_failed(stage, error)
            self.logger.warning(f"Stage {stage.value} failed for {job.job_id}: {error}")
        else:
            job.mark_stage_complete(
                stage,
                output_files=result.get('output_files', []),
                metrics=result.get('metrics', {}),
            )
            self.logger.info(f"Stage {stage.value} complete for {job.job_id}")

        # Save state
        self.pipeline_store.save_job(job)

        # Schedule next stage
        self._schedule_next_stage(job)

    def _recover_incomplete_pipelines(self) -> None:
        """Recover and resume incomplete pipelines from previous run."""
        incomplete = self.pipeline_store.load_incomplete_jobs()

        if not incomplete:
            self.logger.info("No incomplete pipelines to recover")
            return

        self.logger.info(f"Recovering {len(incomplete)} incomplete pipelines")

        for job in incomplete:
            # Reset any running stages to pending
            for stage, result in job.stages.items():
                if result.status == StageStatus.RUNNING:
                    result.status = StageStatus.PENDING
                    result.start_time = None

            self.active_pipelines[job.job_id] = job
            self._schedule_next_stage(job)

    def _cleanup_pipeline(self, job: PipelineJob) -> None:
        """Clean up completed pipeline.

        Args:
            job: Completed pipeline job
        """
        if job.job_id in self.active_pipelines:
            del self.active_pipelines[job.job_id]

    def get_pipeline_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a pipeline job.

        Args:
            job_id: Pipeline job identifier

        Returns:
            Status dictionary or None if not found
        """
        job = self.active_pipelines.get(job_id)
        if job is None:
            job = self.pipeline_store.load_job(job_id)

        if job is None:
            return None

        return job.to_dict()

    def get_pipeline_stats(self) -> Dict[str, Any]:
        """Get overall pipeline statistics.

        Returns:
            Statistics dictionary
        """
        db_stats = self.pipeline_store.get_stats()
        pool_status = self.resource_pools.get_status()

        return {
            'pipelines': db_stats,
            'resource_pools': {
                name: {
                    'max_workers': status.max_workers,
                    'active_tasks': status.active_tasks,
                    'completed': status.completed_total,
                    'failed': status.failed_total,
                }
                for name, status in pool_status.items()
            },
            'active_pipelines': len(self.active_pipelines),
        }


# Module-level pipeline job function for APScheduler serialization
def _run_pipeline_job(
    station_id: str,
    session_type: str,
    production_mode: bool = False,
    priority_name: str = 'STANDARD',
):
    """Run a complete pipeline for a station (standalone job function).

    This is a module-level function for APScheduler serialization.
    Creates a temporary PipelineScheduler to execute the pipeline.

    Args:
        station_id: Station identifier
        session_type: Session type
        production_mode: Use production logging
        priority_name: Priority level name (REALTIME, STANDARD, BACKFILL)
    """
    logger = logging.getLogger(f'gps_scheduler.pipeline.{station_id}')

    try:
        priority = TaskPriority[priority_name.upper()]

        # Create minimal scheduler for pipeline execution
        # In production, this would be a singleton or use shared resources
        scheduler = PipelineScheduler(production_mode=production_mode)

        # Schedule and wait for pipeline
        target_time = datetime.now(timezone.utc)
        job = scheduler.schedule_pipeline(
            station_id=station_id,
            session_type=session_type,
            target_time=target_time,
            priority=priority,
        )

        logger.info(f"Started pipeline: {job.job_id}")

        # Note: Pipeline runs asynchronously, completion handled by callbacks

    except Exception as e:
        logger.error(f"Pipeline failed: {station_id} ({session_type}) - {e}")
