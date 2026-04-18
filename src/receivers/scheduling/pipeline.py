"""Pipeline state tracking for multi-stage task execution.

Pipelines represent sequences of dependent tasks:
- Download → RINEX → Sync (15s_24hr)
- Download → Sync (1Hz_1hr)
- Download → Health (status_1hr)

Each pipeline job tracks:
- Stage completion status
- Dependencies between stages
- Output files for chaining
- Crash recovery state
"""

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .task_interface import TaskPriority

logger = logging.getLogger(__name__)


class PipelineStage(Enum):
    """Stages in a data processing pipeline."""

    DOWNLOAD = "download"  # Fetch raw data from receiver
    RINEX = "rinex"  # Convert raw to RINEX format
    SYNC = "sync"  # Rsync to permanent storage
    HEALTH = "health"  # Extract health metrics


class StageStatus(Enum):
    """Status of a pipeline stage."""

    PENDING = "pending"  # Waiting for dependencies
    RUNNING = "running"  # Currently executing
    COMPLETED = "completed"  # Successfully finished
    FAILED = "failed"  # Execution failed
    SKIPPED = "skipped"  # Skipped (not applicable)


# Stage dependency graph: key depends on value stages
STAGE_DEPENDENCIES = {
    PipelineStage.DOWNLOAD: [],
    PipelineStage.RINEX: [PipelineStage.DOWNLOAD],
    PipelineStage.SYNC: [PipelineStage.DOWNLOAD],  # Can sync raw without RINEX
    PipelineStage.HEALTH: [PipelineStage.DOWNLOAD],
}


@dataclass
class StageResult:
    """Result of a single pipeline stage."""

    stage: PipelineStage
    status: StageStatus
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    output_files: List[str] = field(default_factory=list)
    error: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        """Calculate stage duration in seconds."""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "output_files": self.output_files,
            "error": self.error,
            "metrics": self.metrics,
            "duration_seconds": self.duration_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StageResult":
        """Create from dictionary."""
        return cls(
            stage=PipelineStage(data["stage"]),
            status=StageStatus(data["status"]),
            start_time=datetime.fromisoformat(data["start_time"])
            if data.get("start_time")
            else None,
            end_time=datetime.fromisoformat(data["end_time"])
            if data.get("end_time")
            else None,
            output_files=data.get("output_files", []),
            error=data.get("error"),
            metrics=data.get("metrics", {}),
        )


@dataclass
class PipelineJob:
    """A complete pipeline job with multiple stages.

    Tracks the full lifecycle of a data processing pipeline from
    download through RINEX conversion and sync to permanent storage.
    """

    job_id: str
    station_id: str
    session_type: str
    target_time: datetime
    priority: TaskPriority
    stages: Dict[PipelineStage, StageResult] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def create(
        cls,
        station_id: str,
        session_type: str,
        target_time: datetime,
        enabled_stages: List[PipelineStage],
        priority: TaskPriority = TaskPriority.STANDARD,
    ) -> "PipelineJob":
        """Create a new pipeline job.

        Args:
            station_id: Station identifier
            session_type: Session type (15s_24hr, 1Hz_1hr, etc.)
            target_time: Target data time
            enabled_stages: List of stages to run
            priority: Job priority

        Returns:
            New PipelineJob instance
        """
        job_id = f"{station_id}_{session_type}_{target_time.strftime('%Y%m%d%H%M')}_{uuid.uuid4().hex[:8]}"

        stages = {}
        for stage in enabled_stages:
            stages[stage] = StageResult(stage=stage, status=StageStatus.PENDING)

        return cls(
            job_id=job_id,
            station_id=station_id,
            session_type=session_type,
            target_time=target_time,
            priority=priority,
            stages=stages,
        )

    def can_run_stage(self, stage: PipelineStage) -> bool:
        """Check if a stage can be run (dependencies satisfied).

        Args:
            stage: Stage to check

        Returns:
            True if all dependencies are completed
        """
        if stage not in self.stages:
            return False

        stage_result = self.stages[stage]
        if stage_result.status != StageStatus.PENDING:
            return False  # Already running, completed, or failed

        # Check all dependencies are completed
        for dep_stage in STAGE_DEPENDENCIES.get(stage, []):
            if dep_stage in self.stages:
                dep_result = self.stages[dep_stage]
                if dep_result.status != StageStatus.COMPLETED:
                    return False

        return True

    def get_runnable_stages(self) -> List[PipelineStage]:
        """Get list of stages that can currently run.

        Returns:
            List of stages with satisfied dependencies
        """
        return [stage for stage in self.stages if self.can_run_stage(stage)]

    def mark_stage_started(self, stage: PipelineStage) -> None:
        """Mark a stage as started."""
        if stage in self.stages:
            self.stages[stage].status = StageStatus.RUNNING
            self.stages[stage].start_time = datetime.now(timezone.utc)
            self.updated_at = datetime.now(timezone.utc)

    def mark_stage_complete(
        self,
        stage: PipelineStage,
        output_files: Optional[List[str]] = None,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mark a stage as completed successfully."""
        if stage in self.stages:
            result = self.stages[stage]
            result.status = StageStatus.COMPLETED
            result.end_time = datetime.now(timezone.utc)
            result.output_files = output_files or []
            result.metrics = metrics or {}
            self.updated_at = datetime.now(timezone.utc)

    def mark_stage_failed(self, stage: PipelineStage, error: str) -> None:
        """Mark a stage as failed."""
        if stage in self.stages:
            result = self.stages[stage]
            result.status = StageStatus.FAILED
            result.end_time = datetime.now(timezone.utc)
            result.error = error
            self.updated_at = datetime.now(timezone.utc)

    def is_complete(self) -> bool:
        """Check if all stages are complete (success or failure)."""
        for result in self.stages.values():
            if result.status in (StageStatus.PENDING, StageStatus.RUNNING):
                return False
        return True

    def is_successful(self) -> bool:
        """Check if all stages completed successfully."""
        for result in self.stages.values():
            if result.status != StageStatus.COMPLETED:
                return False
        return True

    def get_stage_output_files(self, stage: PipelineStage) -> List[str]:
        """Get output files from a specific stage."""
        if stage in self.stages:
            return self.stages[stage].output_files
        return []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "job_id": self.job_id,
            "station_id": self.station_id,
            "session_type": self.session_type,
            "target_time": self.target_time.isoformat(),
            "priority": self.priority.value,
            "stages": {
                stage.value: result.to_dict() for stage, result in self.stages.items()
            },
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineJob":
        """Create from dictionary."""
        stages = {}
        for stage_name, result_data in data.get("stages", {}).items():
            stage = PipelineStage(stage_name)
            stages[stage] = StageResult.from_dict(result_data)

        return cls(
            job_id=data["job_id"],
            station_id=data["station_id"],
            session_type=data["session_type"],
            target_time=datetime.fromisoformat(data["target_time"]),
            priority=TaskPriority(data["priority"]),
            stages=stages,
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )


class PipelineStateStore:
    """SQLite-backed persistence for pipeline state.

    Enables crash recovery by persisting pipeline state to disk.
    Incomplete jobs can be resumed after process restart.
    """

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize state store.

        Args:
            db_path: Path to SQLite database file.
                    Default: ~/.cache/gps_receivers/pipeline.db
        """
        if db_path is None:
            db_path = Path.home() / ".cache" / "gps_receivers" / "pipeline.db"

        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_jobs (
                    job_id TEXT PRIMARY KEY,
                    station_id TEXT NOT NULL,
                    session_type TEXT NOT NULL,
                    target_time TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    stages_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_complete INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_station_session
                ON pipeline_jobs(station_id, session_type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_incomplete
                ON pipeline_jobs(is_complete) WHERE is_complete = 0
            """)
            conn.commit()

    def save_job(self, job: PipelineJob) -> None:
        """Save or update a pipeline job.

        Args:
            job: PipelineJob to persist
        """
        stages_json = json.dumps(
            {stage.value: result.to_dict() for stage, result in job.stages.items()}
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pipeline_jobs
                (job_id, station_id, session_type, target_time, priority,
                 stages_json, created_at, updated_at, is_complete)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    job.job_id,
                    job.station_id,
                    job.session_type,
                    job.target_time.isoformat(),
                    job.priority.value,
                    stages_json,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                    1 if job.is_complete() else 0,
                ),
            )
            conn.commit()

        logger.debug(f"Saved pipeline job {job.job_id}")

    def load_job(self, job_id: str) -> Optional[PipelineJob]:
        """Load a pipeline job by ID.

        Args:
            job_id: Job identifier

        Returns:
            PipelineJob if found, None otherwise
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM pipeline_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()

            if row is None:
                return None

            return self._row_to_job(row)

    def load_incomplete_jobs(self) -> List[PipelineJob]:
        """Load all incomplete pipeline jobs.

        Used for crash recovery - returns jobs that were in progress
        when the scheduler stopped.

        Returns:
            List of incomplete PipelineJobs
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM pipeline_jobs WHERE is_complete = 0 ORDER BY priority, created_at"
            ).fetchall()

            jobs = [self._row_to_job(row) for row in rows]
            logger.info(f"Loaded {len(jobs)} incomplete pipeline jobs for recovery")
            return jobs

    def load_jobs_by_station(
        self,
        station_id: str,
        session_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[PipelineJob]:
        """Load recent jobs for a station.

        Args:
            station_id: Station identifier
            session_type: Optional session type filter
            limit: Maximum number of jobs to return

        Returns:
            List of PipelineJobs
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            if session_type:
                rows = conn.execute(
                    """
                    SELECT * FROM pipeline_jobs
                    WHERE station_id = ? AND session_type = ?
                    ORDER BY created_at DESC LIMIT ?
                """,
                    (station_id, session_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM pipeline_jobs
                    WHERE station_id = ?
                    ORDER BY created_at DESC LIMIT ?
                """,
                    (station_id, limit),
                ).fetchall()

            return [self._row_to_job(row) for row in rows]

    def mark_stage_complete(
        self,
        job_id: str,
        stage: PipelineStage,
        output_files: Optional[List[str]] = None,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> Optional[PipelineJob]:
        """Mark a stage as complete and save.

        Args:
            job_id: Job identifier
            stage: Stage that completed
            output_files: Output files from the stage
            metrics: Stage metrics

        Returns:
            Updated PipelineJob, or None if not found
        """
        job = self.load_job(job_id)
        if job is None:
            return None

        job.mark_stage_complete(stage, output_files, metrics)
        self.save_job(job)
        return job

    def mark_stage_failed(
        self,
        job_id: str,
        stage: PipelineStage,
        error: str,
    ) -> Optional[PipelineJob]:
        """Mark a stage as failed and save.

        Args:
            job_id: Job identifier
            stage: Stage that failed
            error: Error message

        Returns:
            Updated PipelineJob, or None if not found
        """
        job = self.load_job(job_id)
        if job is None:
            return None

        job.mark_stage_failed(stage, error)
        self.save_job(job)
        return job

    def delete_job(self, job_id: str) -> bool:
        """Delete a pipeline job.

        Args:
            job_id: Job identifier

        Returns:
            True if job was deleted
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM pipeline_jobs WHERE job_id = ?", (job_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def cleanup_old_jobs(self, days: int = 30) -> int:
        """Remove completed jobs older than specified days.

        Args:
            days: Age threshold in days

        Returns:
            Number of jobs deleted
        """
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                DELETE FROM pipeline_jobs
                WHERE is_complete = 1 AND updated_at < ?
            """,
                (cutoff,),
            )
            conn.commit()
            deleted = cursor.rowcount

        logger.info(f"Cleaned up {deleted} old pipeline jobs")
        return deleted

    def _row_to_job(self, row: sqlite3.Row) -> PipelineJob:
        """Convert database row to PipelineJob."""
        stages_data = json.loads(row["stages_json"])
        stages = {}
        for stage_name, result_data in stages_data.items():
            stage = PipelineStage(stage_name)
            stages[stage] = StageResult.from_dict(result_data)

        return PipelineJob(
            job_id=row["job_id"],
            station_id=row["station_id"],
            session_type=row["session_type"],
            target_time=datetime.fromisoformat(row["target_time"]),
            priority=TaskPriority(row["priority"]),
            stages=stages,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get pipeline statistics.

        Returns:
            Dictionary with pipeline statistics
        """
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0]

            incomplete = conn.execute(
                "SELECT COUNT(*) FROM pipeline_jobs WHERE is_complete = 0"
            ).fetchone()[0]

            by_session = {}
            for row in conn.execute("""
                SELECT session_type, COUNT(*) as count
                FROM pipeline_jobs
                GROUP BY session_type
            """).fetchall():
                by_session[row[0]] = row[1]

            return {
                "total_jobs": total,
                "incomplete_jobs": incomplete,
                "complete_jobs": total - incomplete,
                "by_session_type": by_session,
            }
