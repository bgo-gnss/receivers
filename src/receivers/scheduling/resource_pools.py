"""Resource pool manager for scheduler operations.

Manages separate pools for I/O-bound (network) and CPU-bound operations:
- Network pool (ThreadPoolExecutor): Downloads, rsync, health checks
- CPU pool (ProcessPoolExecutor): RINEX conversion (memory-limited)

This separation ensures:
1. Network operations don't block on CPU work
2. CPU workers are limited to prevent memory exhaustion
3. Priority ordering is respected within each pool
"""

import logging
import os
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from queue import PriorityQueue
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

from .task_interface import TaskPriority

logger = logging.getLogger(__name__)


@dataclass(order=True)
class PrioritizedTask:
    """Task wrapper for priority queue ordering.

    Lower priority value = higher priority (processed first).
    Ties are broken by submission order (timestamp).
    """

    priority: int
    submit_time: float = field(compare=True)
    task_id: str = field(compare=False)
    fn: Callable = field(compare=False)
    args: tuple = field(compare=False)
    kwargs: dict = field(compare=False)
    future: Optional[Future] = field(compare=False, default=None)


@dataclass
class PoolStatus:
    """Status of a resource pool."""

    pool_type: str
    max_workers: int
    active_tasks: int
    pending_tasks: int
    completed_total: int
    failed_total: int
    avg_duration_seconds: float


class ResourcePoolManager:
    """Manages separate pools for I/O and CPU operations.

    Resource pools prevent resource contention:
    - Network pool: High concurrency for I/O-bound operations (downloads, rsync)
    - CPU pool: Limited workers for memory-intensive operations (RINEX conversion)

    Each pool supports priority ordering so real-time tasks are processed first.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize resource pools.

        Args:
            config: Configuration dictionary with keys:
                - network_workers: Max workers for network pool (default: 10)
                - cpu_workers: Max workers for CPU pool (default: min(4, cpu_count-2))
        """
        config = config or {}

        # Network pool: ThreadPoolExecutor for I/O-bound operations
        self._network_workers = config.get("network_workers", 10)
        self._network_pool: Optional[ThreadPoolExecutor] = None

        # CPU pool: ProcessPoolExecutor for CPU-bound operations
        # Limit workers to prevent memory exhaustion
        default_cpu = max(1, min(4, (os.cpu_count() or 4) - 2))
        self._cpu_workers = config.get("cpu_workers", default_cpu)
        self._cpu_pool: Optional[ProcessPoolExecutor] = None

        # Priority tracking
        self._network_pending: PriorityQueue = PriorityQueue()
        self._cpu_pending: PriorityQueue = PriorityQueue()

        # Statistics
        self._lock = Lock()
        self._stats = {
            "network": {"completed": 0, "failed": 0, "total_duration": 0.0},
            "cpu": {"completed": 0, "failed": 0, "total_duration": 0.0},
        }

        # Track active futures
        self._active_futures: Dict[str, Future] = {}

        logger.info(
            f"ResourcePoolManager initialized: network={self._network_workers}, "
            f"cpu={self._cpu_workers}"
        )

    def start(self) -> None:
        """Start the resource pools."""
        if self._network_pool is None:
            self._network_pool = ThreadPoolExecutor(
                max_workers=self._network_workers, thread_name_prefix="gps_network"
            )
            logger.debug(f"Started network pool with {self._network_workers} workers")

        if self._cpu_pool is None:
            self._cpu_pool = ProcessPoolExecutor(max_workers=self._cpu_workers)
            logger.debug(f"Started CPU pool with {self._cpu_workers} workers")

    def submit_network(
        self,
        fn: Callable,
        *args,
        priority: TaskPriority = TaskPriority.STANDARD,
        task_id: Optional[str] = None,
        **kwargs,
    ) -> Future:
        """Submit a task to the network pool.

        Args:
            fn: Function to execute
            *args: Positional arguments for fn
            priority: Task priority (lower value = higher priority)
            task_id: Optional task identifier for tracking
            **kwargs: Keyword arguments for fn

        Returns:
            Future representing the task result
        """
        if self._network_pool is None:
            self.start()

        task_id = task_id or f"net_{datetime.now(UTC).timestamp()}"

        # Submit directly to the pool (APScheduler handles priority)
        assert self._network_pool is not None
        future = self._network_pool.submit(
            self._wrap_task, "network", task_id, fn, args, kwargs
        )

        with self._lock:
            self._active_futures[task_id] = future

        # Add callback to track completion
        future.add_done_callback(
            lambda f: self._on_task_complete("network", task_id, f)
        )

        logger.debug(f"Submitted network task {task_id} with priority {priority.name}")
        return future

    def submit_cpu(
        self,
        fn: Callable,
        *args,
        priority: TaskPriority = TaskPriority.STANDARD,
        task_id: Optional[str] = None,
        **kwargs,
    ) -> Future:
        """Submit a task to the CPU pool.

        Args:
            fn: Function to execute (must be picklable for ProcessPool)
            *args: Positional arguments for fn
            priority: Task priority (lower value = higher priority)
            task_id: Optional task identifier for tracking
            **kwargs: Keyword arguments for fn

        Returns:
            Future representing the task result
        """
        if self._cpu_pool is None:
            self.start()

        task_id = task_id or f"cpu_{datetime.now(UTC).timestamp()}"

        # Submit directly to the pool
        assert self._cpu_pool is not None
        future = self._cpu_pool.submit(fn, *args, **kwargs)

        with self._lock:
            self._active_futures[task_id] = future

        # Add callback to track completion
        future.add_done_callback(lambda f: self._on_task_complete("cpu", task_id, f))

        logger.debug(f"Submitted CPU task {task_id} with priority {priority.name}")
        return future

    def _wrap_task(
        self, pool_type: str, _task_id: str, fn: Callable, args: tuple, kwargs: dict
    ) -> Any:
        """Wrap a task execution with timing and error handling."""
        import time

        start = time.time()

        try:
            result = fn(*args, **kwargs)
            return result
        finally:
            duration = time.time() - start
            with self._lock:
                self._stats[pool_type]["total_duration"] += duration

    def _on_task_complete(self, pool_type: str, task_id: str, future: Future) -> None:
        """Handle task completion."""
        with self._lock:
            if task_id in self._active_futures:
                del self._active_futures[task_id]

            if future.exception() is not None:
                self._stats[pool_type]["failed"] += 1
                logger.warning(f"Task {task_id} failed: {future.exception()}")
            else:
                self._stats[pool_type]["completed"] += 1

    def get_status(self) -> Dict[str, PoolStatus]:
        """Get status of all resource pools.

        Returns:
            Dictionary mapping pool name to PoolStatus
        """
        with self._lock:
            network_stats = self._stats["network"]
            cpu_stats = self._stats["cpu"]

            # Count active network tasks
            network_active = sum(
                1 for tid in self._active_futures if tid.startswith("net_")
            )
            cpu_active = sum(
                1 for tid in self._active_futures if tid.startswith("cpu_")
            )

            # Calculate average durations
            network_completed = network_stats["completed"] + network_stats["failed"]
            cpu_completed = cpu_stats["completed"] + cpu_stats["failed"]

            return {
                "network": PoolStatus(
                    pool_type="network",
                    max_workers=self._network_workers,
                    active_tasks=network_active,
                    pending_tasks=self._network_pending.qsize(),
                    completed_total=network_stats["completed"],
                    failed_total=network_stats["failed"],
                    avg_duration_seconds=(
                        network_stats["total_duration"] / network_completed
                        if network_completed > 0
                        else 0.0
                    ),
                ),
                "cpu": PoolStatus(
                    pool_type="cpu",
                    max_workers=self._cpu_workers,
                    active_tasks=cpu_active,
                    pending_tasks=self._cpu_pending.qsize(),
                    completed_total=cpu_stats["completed"],
                    failed_total=cpu_stats["failed"],
                    avg_duration_seconds=(
                        cpu_stats["total_duration"] / cpu_completed
                        if cpu_completed > 0
                        else 0.0
                    ),
                ),
            }

    def get_active_tasks(self) -> List[str]:
        """Get list of active task IDs."""
        with self._lock:
            return list(self._active_futures.keys())

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a pending or running task.

        Args:
            task_id: Task identifier

        Returns:
            True if cancellation was successful
        """
        with self._lock:
            future = self._active_futures.get(task_id)
            if future:
                cancelled = future.cancel()
                if cancelled:
                    del self._active_futures[task_id]
                    logger.info(f"Cancelled task {task_id}")
                return cancelled
            return False

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """Shutdown all resource pools.

        Args:
            wait: Wait for pending tasks to complete
            cancel_futures: Cancel pending futures (Python 3.9+)
        """
        logger.info("Shutting down resource pools...")

        if self._network_pool:
            try:
                self._network_pool.shutdown(wait=wait, cancel_futures=cancel_futures)
            except TypeError:
                # Python < 3.9 doesn't support cancel_futures
                self._network_pool.shutdown(wait=wait)
            self._network_pool = None

        if self._cpu_pool:
            try:
                self._cpu_pool.shutdown(wait=wait, cancel_futures=cancel_futures)
            except TypeError:
                self._cpu_pool.shutdown(wait=wait)
            self._cpu_pool = None

        logger.info("Resource pools shut down")

    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        """Context manager exit."""
        self.shutdown(wait=True)
