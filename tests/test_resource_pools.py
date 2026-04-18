"""Tests for ResourcePoolManager.

Tests the resource pool management for network and CPU operations.
"""

import sys
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from receivers.scheduling.resource_pools import PoolStatus, ResourcePoolManager
from receivers.scheduling.task_interface import TaskPriority


class TestResourcePoolManager:
    """Tests for ResourcePoolManager."""

    def test_init_default_config(self):
        """Test initialization with default configuration."""
        manager = ResourcePoolManager()

        assert manager._network_workers == 10
        assert manager._cpu_workers >= 1
        assert manager._network_pool is None
        assert manager._cpu_pool is None

    def test_init_custom_config(self):
        """Test initialization with custom configuration."""
        config = {
            "network_workers": 5,
            "cpu_workers": 2,
        }
        manager = ResourcePoolManager(config)

        assert manager._network_workers == 5
        assert manager._cpu_workers == 2

    def test_start_creates_pools(self):
        """Test that start() creates thread and process pools."""
        manager = ResourcePoolManager()
        manager.start()

        try:
            assert manager._network_pool is not None
            assert manager._cpu_pool is not None
        finally:
            manager.shutdown()

    def test_submit_network_task(self):
        """Test submitting a network (I/O-bound) task."""
        manager = ResourcePoolManager({"network_workers": 2})
        manager.start()

        try:
            result_container = []

            def task():
                result_container.append("done")
                return 42

            future = manager.submit_network(task, priority=TaskPriority.STANDARD)
            assert isinstance(future, Future)

            result = future.result(timeout=5)
            assert result == 42
            assert "done" in result_container
        finally:
            manager.shutdown()

    def test_submit_cpu_task(self):
        """Test submitting a CPU-bound task."""
        manager = ResourcePoolManager({"cpu_workers": 1})
        manager.start()

        try:
            # CPU tasks need picklable functions
            future = manager.submit_cpu(
                sum, [1, 2, 3, 4, 5], priority=TaskPriority.STANDARD
            )
            assert isinstance(future, Future)

            result = future.result(timeout=5)
            assert result == 15
        finally:
            manager.shutdown()

    def test_get_status(self):
        """Test getting pool status."""
        manager = ResourcePoolManager({"network_workers": 3, "cpu_workers": 2})
        manager.start()

        try:
            status = manager.get_status()

            assert "network" in status
            assert "cpu" in status

            network_status = status["network"]
            assert isinstance(network_status, PoolStatus)
            assert network_status.max_workers == 3
            assert network_status.pool_type == "network"

            cpu_status = status["cpu"]
            assert isinstance(cpu_status, PoolStatus)
            assert cpu_status.max_workers == 2
            assert cpu_status.pool_type == "cpu"
        finally:
            manager.shutdown()

    def test_context_manager(self):
        """Test using manager as context manager."""
        with ResourcePoolManager({"network_workers": 2}) as manager:
            future = manager.submit_network(lambda: "hello")
            result = future.result(timeout=5)
            assert result == "hello"

        # Pools should be shut down after exiting context
        assert manager._network_pool is None

    def test_shutdown_waits_for_tasks(self):
        """Test that shutdown waits for pending tasks."""
        manager = ResourcePoolManager({"network_workers": 1})
        manager.start()

        results = []

        def slow_task():
            time.sleep(0.5)
            results.append("done")
            return True

        manager.submit_network(slow_task)

        # Shutdown should wait for the task
        manager.shutdown(wait=True)

        # Task should have completed
        assert "done" in results

    def test_priority_is_tracked(self):
        """Test that priority is tracked in task submission."""
        manager = ResourcePoolManager()
        manager.start()

        try:
            # Submit tasks with different priorities
            future1 = manager.submit_network(
                lambda: 1, priority=TaskPriority.REALTIME, task_id="realtime_task"
            )
            future2 = manager.submit_network(
                lambda: 2, priority=TaskPriority.BACKFILL, task_id="backfill_task"
            )

            # Both should complete
            assert future1.result(timeout=5) == 1
            assert future2.result(timeout=5) == 2
        finally:
            manager.shutdown()

    def test_stats_tracking(self):
        """Test that statistics are tracked correctly."""
        manager = ResourcePoolManager({"network_workers": 2})
        manager.start()

        try:
            # Submit and complete a few tasks
            for i in range(3):
                future = manager.submit_network(lambda: i)
                future.result(timeout=5)

            # Give time for callbacks to run
            time.sleep(0.1)

            status = manager.get_status()
            assert status["network"].completed_total == 3
            assert status["network"].failed_total == 0
        finally:
            manager.shutdown()


class TestPoolStatus:
    """Tests for PoolStatus dataclass."""

    def test_pool_status_creation(self):
        """Test PoolStatus dataclass creation."""
        status = PoolStatus(
            pool_type="network",
            max_workers=5,
            active_tasks=2,
            pending_tasks=3,
            completed_total=100,
            failed_total=5,
            avg_duration_seconds=1.5,
        )

        assert status.pool_type == "network"
        assert status.max_workers == 5
        assert status.active_tasks == 2
        assert status.pending_tasks == 3
        assert status.completed_total == 100
        assert status.failed_total == 5
        assert status.avg_duration_seconds == 1.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
