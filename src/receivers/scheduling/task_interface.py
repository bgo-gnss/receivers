"""Abstract interface for scheduled tasks.

Provides extensible architecture for scheduling various operations beyond downloads:
- DownloadTask: Download data from receivers
- StatusTask: Check receiver status
- HealthTask: Perform health checks
- ValidateTask: Validate configurations

This allows the scheduler to handle any type of task while maintaining
a consistent interface and behavior.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
from enum import Enum
import logging


class TaskType(Enum):
    """Types of scheduled tasks."""
    DOWNLOAD = "download"
    STATUS = "status"
    HEALTH = "health"
    VALIDATE = "validate"


class TaskFrequency(Enum):
    """Task execution frequency."""
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MANUAL = "manual"


@dataclass
class TaskConfig:
    """Configuration for a scheduled task.

    Attributes:
        task_type: Type of task (download, status, health, etc.)
        session_type: Session identifier (e.g., '1Hz_1hr', 'status_1hr')
        schedule_minute: Minute past the hour/day to start
        distribution_window: Minutes to spread tasks across
        frequency: How often to run (hourly, daily, etc.)
        enabled: Whether this task type is enabled
        max_concurrent: Maximum concurrent instances
        timeout_minutes: Timeout for task execution
        retry_count: Number of retries on failure
        priority: Task priority (higher = more important)
    """
    task_type: TaskType
    session_type: str
    schedule_minute: int
    distribution_window: int
    frequency: TaskFrequency
    enabled: bool = True
    max_concurrent: int = 3
    timeout_minutes: int = 30
    retry_count: int = 0
    priority: int = 5


@dataclass
class TaskResult:
    """Result of task execution.

    Attributes:
        success: Whether task completed successfully
        status: Status string ('completed', 'failed', 'partial', etc.)
        duration: Execution time in seconds
        message: Human-readable result message
        data: Task-specific result data (files downloaded, status info, etc.)
        error: Error information if task failed
        metrics: Performance/monitoring metrics
    """
    success: bool
    status: str
    duration: float
    message: str
    data: Dict[str, Any]
    error: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None


class ScheduledTask(ABC):
    """Abstract base class for scheduled tasks.

    All scheduled tasks must implement this interface. Tasks are executed
    by the scheduler and can be of any type (download, status check, etc.).

    The scheduler handles:
    - Job scheduling and persistence
    - Time distribution across windows
    - Concurrent execution management
    - Error handling and retries
    - Audit logging

    Tasks are responsible for:
    - Determining time parameters
    - Executing the actual operation
    - Validating prerequisites
    - Returning structured results
    """

    def __init__(
        self,
        station_id: str,
        config: TaskConfig,
        logger: Optional[logging.Logger] = None
    ):
        """Initialize task.

        Args:
            station_id: Station identifier
            config: Task configuration
            logger: Optional logger instance
        """
        self.station_id = station_id
        self.config = config
        self.logger = logger or logging.getLogger(f'task.{station_id}')

    @abstractmethod
    def get_time_parameters(self) -> Tuple[datetime, datetime]:
        """Calculate start and end times for this task execution.

        Different task types may calculate times differently:
        - Hourly tasks: previous hour
        - Daily tasks: previous day
        - Status tasks: current time

        Returns:
            Tuple of (start_time, end_time)
        """
        pass

    @abstractmethod
    def validate_prerequisites(self) -> Tuple[bool, Optional[str]]:
        """Validate that task can be executed.

        Checks:
        - Station configuration exists
        - Required services available
        - No conflicting operations

        Returns:
            Tuple of (is_valid, error_message)
        """
        pass

    @abstractmethod
    def execute(self) -> TaskResult:
        """Execute the task.

        This is the main entry point called by the scheduler.
        Should handle all errors internally and return a TaskResult.

        Returns:
            TaskResult with execution details
        """
        pass

    def get_task_id(self) -> str:
        """Get unique task identifier.

        Format: {session_type}_{station_id}
        Example: '1Hz_1hr_ELDC'

        Returns:
            Task identifier string
        """
        return f"{self.config.session_type}_{self.station_id}"

    def should_retry(self, result: TaskResult) -> bool:
        """Determine if task should be retried after failure.

        Args:
            result: Result of failed execution

        Returns:
            True if retry should be attempted
        """
        if result.success:
            return False

        # Check if error is retryable
        if result.error:
            # Don't retry configuration errors
            if 'ConfigurationError' in result.error:
                return False
            # Don't retry validation errors
            if 'ValidationError' in result.error:
                return False

        # Retry network/connection errors
        return True

    def get_audit_data(self, result: TaskResult) -> Dict[str, Any]:
        """Get data for audit logging.

        Args:
            result: Task execution result

        Returns:
            Dictionary with audit information
        """
        return {
            'task_type': self.config.task_type.value,
            'session': self.config.session_type,
            'status': result.status,
            'duration': result.duration,
            'scheduled': True,
            'success': result.success,
            'error_message': result.error if result.error else None
        }


class TaskFactory:
    """Factory for creating scheduled tasks.

    Provides central registration and creation of task types.
    Allows easy addition of new task types without modifying scheduler.
    """

    _task_classes: Dict[TaskType, type] = {}

    @classmethod
    def register(cls, task_type: TaskType, task_class: type):
        """Register a task class for a task type.

        Args:
            task_type: Type of task
            task_class: Class implementing ScheduledTask
        """
        if not issubclass(task_class, ScheduledTask):
            raise TypeError(f"{task_class} must inherit from ScheduledTask")
        cls._task_classes[task_type] = task_class

    @classmethod
    def create(
        cls,
        task_type: TaskType,
        station_id: str,
        config: TaskConfig,
        logger: Optional[logging.Logger] = None
    ) -> ScheduledTask:
        """Create a task instance.

        Args:
            task_type: Type of task to create
            station_id: Station identifier
            config: Task configuration
            logger: Optional logger

        Returns:
            Task instance

        Raises:
            ValueError: If task type not registered
        """
        if task_type not in cls._task_classes:
            raise ValueError(
                f"Task type {task_type} not registered. "
                f"Available: {list(cls._task_classes.keys())}"
            )

        task_class = cls._task_classes[task_type]
        return task_class(station_id, config, logger)

    @classmethod
    def get_registered_types(cls) -> list:
        """Get list of registered task types.

        Returns:
            List of TaskType enums
        """
        return list(cls._task_classes.keys())
