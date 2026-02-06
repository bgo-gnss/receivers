"""GPS Receivers Scheduling Package.

This package provides scheduling infrastructure for automated GPS data processing:

Core Components:
- task_interface: Abstract base for scheduled tasks (ScheduledTask, TaskFactory)
- bulk_scheduler: APScheduler-based download scheduling (BulkDownloadScheduler)
- pipeline_scheduler: Multi-stage pipeline orchestration (PipelineScheduler)
- resource_pools: Network/CPU resource pool management (ResourcePoolManager)
- pipeline: Pipeline state tracking and persistence (PipelineJob, PipelineStateStore)

Task Implementations (in tasks/):
- DownloadTask: Download data from receivers
- StatusTask: Live receiver status checks (real-time)
- HealthTask: Background health extraction from files
- RINEXTask: Raw to RINEX conversion
- SyncTask: Rsync to permanent storage
"""

from .task_interface import (
    ScheduledTask,
    TaskConfig,
    TaskFactory,
    TaskFrequency,
    TaskPriority,
    TaskResult,
    TaskType,
)
from .pipeline import (
    PipelineJob,
    PipelineStage,
    PipelineStateStore,
    StageResult,
    StageStatus,
)
from .resource_pools import (
    PoolStatus,
    ResourcePoolManager,
)

# Lazy imports for components with external dependencies
__all__ = [
    # Task interface
    'ScheduledTask',
    'TaskConfig',
    'TaskFactory',
    'TaskFrequency',
    'TaskPriority',
    'TaskResult',
    'TaskType',
    # Pipeline
    'PipelineJob',
    'PipelineStage',
    'PipelineStateStore',
    'StageResult',
    'StageStatus',
    # Resource pools
    'PoolStatus',
    'ResourcePoolManager',
]


def get_bulk_scheduler():
    """Lazy import BulkDownloadScheduler (requires APScheduler)."""
    from .bulk_scheduler import BulkDownloadScheduler
    return BulkDownloadScheduler


def get_pipeline_scheduler():
    """Lazy import PipelineScheduler (requires APScheduler)."""
    from .pipeline_scheduler import PipelineScheduler
    return PipelineScheduler
