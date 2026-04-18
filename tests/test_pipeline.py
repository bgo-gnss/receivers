"""Tests for pipeline state tracking and persistence.

Tests PipelineJob, PipelineStateStore, and stage dependency management.
"""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from receivers.scheduling.pipeline import (
    STAGE_DEPENDENCIES,
    PipelineJob,
    PipelineStage,
    PipelineStateStore,
    StageResult,
    StageStatus,
)
from receivers.scheduling.task_interface import TaskPriority


class TestPipelineStage:
    """Tests for PipelineStage enum."""

    def test_stage_values(self):
        """Test stage enum values."""
        assert PipelineStage.DOWNLOAD.value == "download"
        assert PipelineStage.RINEX.value == "rinex"
        assert PipelineStage.SYNC.value == "sync"
        assert PipelineStage.HEALTH.value == "health"


class TestStageResult:
    """Tests for StageResult dataclass."""

    def test_create_pending_stage(self):
        """Test creating a pending stage result."""
        result = StageResult(
            stage=PipelineStage.DOWNLOAD,
            status=StageStatus.PENDING,
        )

        assert result.stage == PipelineStage.DOWNLOAD
        assert result.status == StageStatus.PENDING
        assert result.start_time is None
        assert result.end_time is None
        assert result.output_files == []
        assert result.error is None

    def test_duration_calculation(self):
        """Test duration calculation."""
        now = datetime.now(timezone.utc)
        result = StageResult(
            stage=PipelineStage.DOWNLOAD,
            status=StageStatus.COMPLETED,
            start_time=now,
            end_time=now,
        )

        assert result.duration_seconds == 0.0

    def test_to_dict(self):
        """Test serialization to dictionary."""
        result = StageResult(
            stage=PipelineStage.DOWNLOAD,
            status=StageStatus.COMPLETED,
            output_files=["file1.sbf", "file2.sbf"],
        )

        data = result.to_dict()
        assert data["stage"] == "download"
        assert data["status"] == "completed"
        assert data["output_files"] == ["file1.sbf", "file2.sbf"]

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "stage": "download",
            "status": "completed",
            "start_time": "2025-01-01T00:00:00+00:00",
            "end_time": "2025-01-01T00:01:00+00:00",
            "output_files": ["file.sbf"],
            "error": None,
            "metrics": {"bytes": 1000},
        }

        result = StageResult.from_dict(data)
        assert result.stage == PipelineStage.DOWNLOAD
        assert result.status == StageStatus.COMPLETED
        assert result.output_files == ["file.sbf"]


class TestPipelineJob:
    """Tests for PipelineJob."""

    def test_create_pipeline(self):
        """Test creating a new pipeline job."""
        job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
            enabled_stages=[PipelineStage.DOWNLOAD, PipelineStage.RINEX],
            priority=TaskPriority.STANDARD,
        )

        assert job.station_id == "ELDC"
        assert job.session_type == "15s_24hr"
        assert job.priority == TaskPriority.STANDARD
        assert PipelineStage.DOWNLOAD in job.stages
        assert PipelineStage.RINEX in job.stages
        assert PipelineStage.SYNC not in job.stages

    def test_can_run_stage_initial(self):
        """Test can_run_stage for initial download stage."""
        job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            enabled_stages=[PipelineStage.DOWNLOAD, PipelineStage.RINEX],
        )

        # Download has no dependencies, should be runnable
        assert job.can_run_stage(PipelineStage.DOWNLOAD) is True
        # RINEX depends on DOWNLOAD, should not be runnable yet
        assert job.can_run_stage(PipelineStage.RINEX) is False

    def test_can_run_stage_after_download(self):
        """Test can_run_stage after download completes."""
        job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            enabled_stages=[
                PipelineStage.DOWNLOAD,
                PipelineStage.RINEX,
                PipelineStage.SYNC,
            ],
        )

        # Complete download
        job.mark_stage_complete(PipelineStage.DOWNLOAD, output_files=["file.sbf"])

        # Now RINEX and SYNC should be runnable (both depend only on DOWNLOAD)
        assert job.can_run_stage(PipelineStage.DOWNLOAD) is False  # Already completed
        assert job.can_run_stage(PipelineStage.RINEX) is True
        assert job.can_run_stage(PipelineStage.SYNC) is True

    def test_get_runnable_stages(self):
        """Test getting all runnable stages."""
        job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            enabled_stages=[
                PipelineStage.DOWNLOAD,
                PipelineStage.RINEX,
                PipelineStage.SYNC,
            ],
        )

        # Initially only DOWNLOAD
        runnable = job.get_runnable_stages()
        assert runnable == [PipelineStage.DOWNLOAD]

        # After download completes
        job.mark_stage_complete(PipelineStage.DOWNLOAD)
        runnable = job.get_runnable_stages()
        assert PipelineStage.RINEX in runnable
        assert PipelineStage.SYNC in runnable

    def test_mark_stage_started(self):
        """Test marking stage as started."""
        job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            enabled_stages=[PipelineStage.DOWNLOAD],
        )

        job.mark_stage_started(PipelineStage.DOWNLOAD)

        result = job.stages[PipelineStage.DOWNLOAD]
        assert result.status == StageStatus.RUNNING
        assert result.start_time is not None

    def test_mark_stage_complete(self):
        """Test marking stage as complete."""
        job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            enabled_stages=[PipelineStage.DOWNLOAD],
        )

        job.mark_stage_started(PipelineStage.DOWNLOAD)
        job.mark_stage_complete(
            PipelineStage.DOWNLOAD,
            output_files=["file1.sbf", "file2.sbf"],
            metrics={"bytes": 5000},
        )

        result = job.stages[PipelineStage.DOWNLOAD]
        assert result.status == StageStatus.COMPLETED
        assert result.end_time is not None
        assert result.output_files == ["file1.sbf", "file2.sbf"]
        assert result.metrics == {"bytes": 5000}

    def test_mark_stage_failed(self):
        """Test marking stage as failed."""
        job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            enabled_stages=[PipelineStage.DOWNLOAD],
        )

        job.mark_stage_started(PipelineStage.DOWNLOAD)
        job.mark_stage_failed(PipelineStage.DOWNLOAD, "Connection timeout")

        result = job.stages[PipelineStage.DOWNLOAD]
        assert result.status == StageStatus.FAILED
        assert result.error == "Connection timeout"

    def test_is_complete(self):
        """Test checking if pipeline is complete."""
        job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            enabled_stages=[PipelineStage.DOWNLOAD, PipelineStage.RINEX],
        )

        assert job.is_complete() is False

        job.mark_stage_complete(PipelineStage.DOWNLOAD)
        assert job.is_complete() is False

        job.mark_stage_complete(PipelineStage.RINEX)
        assert job.is_complete() is True

    def test_is_successful(self):
        """Test checking if pipeline succeeded."""
        job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            enabled_stages=[PipelineStage.DOWNLOAD, PipelineStage.RINEX],
        )

        job.mark_stage_complete(PipelineStage.DOWNLOAD)
        job.mark_stage_failed(PipelineStage.RINEX, "Converter error")

        assert job.is_complete() is True
        assert job.is_successful() is False

    def test_to_dict_and_from_dict(self):
        """Test serialization round-trip."""
        job = PipelineJob.create(
            station_id="ELDC",
            session_type="15s_24hr",
            target_time=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
            enabled_stages=[PipelineStage.DOWNLOAD, PipelineStage.RINEX],
            priority=TaskPriority.REALTIME,
        )

        job.mark_stage_complete(PipelineStage.DOWNLOAD, output_files=["file.sbf"])

        # Serialize
        data = job.to_dict()
        assert data["station_id"] == "ELDC"
        assert data["priority"] == 1  # REALTIME

        # Deserialize
        restored = PipelineJob.from_dict(data)
        assert restored.station_id == job.station_id
        assert restored.session_type == job.session_type
        assert restored.priority == job.priority
        assert restored.stages[PipelineStage.DOWNLOAD].status == StageStatus.COMPLETED


class TestPipelineStateStore:
    """Tests for PipelineStateStore persistence."""

    def test_save_and_load_job(self):
        """Test saving and loading a pipeline job."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_pipeline.db"
            store = PipelineStateStore(db_path)

            job = PipelineJob.create(
                station_id="ELDC",
                session_type="15s_24hr",
                target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                enabled_stages=[PipelineStage.DOWNLOAD, PipelineStage.RINEX],
            )

            store.save_job(job)

            # Load it back
            loaded = store.load_job(job.job_id)
            assert loaded is not None
            assert loaded.station_id == "ELDC"
            assert loaded.session_type == "15s_24hr"
            assert PipelineStage.DOWNLOAD in loaded.stages

    def test_load_nonexistent_job(self):
        """Test loading a job that doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_pipeline.db"
            store = PipelineStateStore(db_path)

            loaded = store.load_job("nonexistent_id")
            assert loaded is None

    def test_load_incomplete_jobs(self):
        """Test loading all incomplete jobs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_pipeline.db"
            store = PipelineStateStore(db_path)

            # Create incomplete job
            incomplete = PipelineJob.create(
                station_id="ELDC",
                session_type="15s_24hr",
                target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                enabled_stages=[PipelineStage.DOWNLOAD],
            )
            store.save_job(incomplete)

            # Create complete job
            complete = PipelineJob.create(
                station_id="THOB",
                session_type="15s_24hr",
                target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                enabled_stages=[PipelineStage.DOWNLOAD],
            )
            complete.mark_stage_complete(PipelineStage.DOWNLOAD)
            store.save_job(complete)

            # Load incomplete
            jobs = store.load_incomplete_jobs()
            assert len(jobs) == 1
            assert jobs[0].station_id == "ELDC"

    def test_mark_stage_complete_via_store(self):
        """Test marking stage complete through store."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_pipeline.db"
            store = PipelineStateStore(db_path)

            job = PipelineJob.create(
                station_id="ELDC",
                session_type="15s_24hr",
                target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                enabled_stages=[PipelineStage.DOWNLOAD],
            )
            store.save_job(job)

            # Mark complete via store
            updated = store.mark_stage_complete(
                job.job_id,
                PipelineStage.DOWNLOAD,
                output_files=["file.sbf"],
            )

            assert updated is not None
            assert (
                updated.stages[PipelineStage.DOWNLOAD].status == StageStatus.COMPLETED
            )

            # Verify persistence
            loaded = store.load_job(job.job_id)
            assert loaded.stages[PipelineStage.DOWNLOAD].status == StageStatus.COMPLETED

    def test_delete_job(self):
        """Test deleting a job."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_pipeline.db"
            store = PipelineStateStore(db_path)

            job = PipelineJob.create(
                station_id="ELDC",
                session_type="15s_24hr",
                target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                enabled_stages=[PipelineStage.DOWNLOAD],
            )
            store.save_job(job)

            # Delete
            result = store.delete_job(job.job_id)
            assert result is True

            # Verify gone
            loaded = store.load_job(job.job_id)
            assert loaded is None

    def test_get_stats(self):
        """Test getting pipeline statistics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_pipeline.db"
            store = PipelineStateStore(db_path)

            # Create some jobs
            for i in range(3):
                job = PipelineJob.create(
                    station_id=f"STN{i}",
                    session_type="15s_24hr",
                    target_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    enabled_stages=[PipelineStage.DOWNLOAD],
                )
                if i == 0:
                    job.mark_stage_complete(PipelineStage.DOWNLOAD)
                store.save_job(job)

            stats = store.get_stats()
            assert stats["total_jobs"] == 3
            assert stats["complete_jobs"] == 1
            assert stats["incomplete_jobs"] == 2


class TestStageDependencies:
    """Tests for stage dependency configuration."""

    def test_download_has_no_dependencies(self):
        """Test that DOWNLOAD has no dependencies."""
        assert STAGE_DEPENDENCIES[PipelineStage.DOWNLOAD] == []

    def test_rinex_depends_on_download(self):
        """Test that RINEX depends on DOWNLOAD."""
        assert PipelineStage.DOWNLOAD in STAGE_DEPENDENCIES[PipelineStage.RINEX]

    def test_sync_depends_on_download(self):
        """Test that SYNC depends on DOWNLOAD."""
        assert PipelineStage.DOWNLOAD in STAGE_DEPENDENCIES[PipelineStage.SYNC]

    def test_health_depends_on_download(self):
        """Test that HEALTH depends on DOWNLOAD."""
        assert PipelineStage.DOWNLOAD in STAGE_DEPENDENCIES[PipelineStage.HEALTH]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
