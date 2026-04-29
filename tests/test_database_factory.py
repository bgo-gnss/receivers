"""Tests for DatabaseConnectionFactory."""

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from receivers.health.database_factory import (
    _MAX_POOL_CONN,
    DatabaseConnectionFactory,
    _conn_semaphore,
)


class TestGetConnectionParams:
    """Test get_connection_params method."""

    def test_default_params(self):
        """Test default connection parameters."""
        with patch("receivers.health.database_factory._load_config_file", return_value={}):
            with patch.dict(os.environ, {"USER": "testuser"}, clear=True):
                params = DatabaseConnectionFactory.get_connection_params()
                assert params["host"] == "localhost"
                assert params["port"] == "5432"
                assert params["database"] == "gps_health"
                assert params["user"] == "testuser"
                assert params["password"] == ""

    def test_env_overrides(self):
        """Test environment variable overrides."""
        env = {
            "POSTGRES_HOST": "db.example.com",
            "POSTGRES_PORT": "5433",
            "POSTGRES_DB": "custom_db",
            "POSTGRES_USER": "dbuser",
            "POSTGRES_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            params = DatabaseConnectionFactory.get_connection_params()
            assert params["host"] == "db.example.com"
            assert params["port"] == "5433"
            assert params["database"] == "custom_db"
            assert params["user"] == "dbuser"
            assert params["password"] == "secret"

    def test_database_override(self):
        """Test database parameter override."""
        params = DatabaseConnectionFactory.get_connection_params(database="other_db")
        assert params["database"] == "other_db"

    def test_user_fallback_to_postgres(self):
        """Test USER fallback when no env vars set."""
        with patch("receivers.health.database_factory._load_config_file", return_value={}):
            with patch.dict(os.environ, {}, clear=True):
                params = DatabaseConnectionFactory.get_connection_params()
                assert params["user"] == "postgres"


class TestGetConnection:
    """Test get_connection method."""

    @patch("receivers.health.database_factory._load_config_file", return_value={})
    @patch(
        "receivers.health.database_factory.DatabaseConnectionFactory.get_connection_params"
    )
    def test_get_connection_with_params(self, mock_params, _mock_cfg):
        """Test connection creation with environment parameters."""
        mock_params.return_value = {
            "host": "localhost",
            "port": "5432",
            "database": "gps_health",
            "user": "testuser",
            "password": "",
        }
        with patch("psycopg2.connect") as mock_connect:
            mock_connect.return_value = MagicMock()
            conn = DatabaseConnectionFactory.get_connection()
            mock_connect.assert_called_once_with(
                host="localhost",
                port="5432",
                database="gps_health",
                user="testuser",
                password="",
            )
            assert conn is not None

    def test_get_connection_with_connection_string(self):
        """Test connection creation with explicit connection string."""
        with patch("psycopg2.connect") as mock_connect:
            mock_connect.return_value = MagicMock()
            conn = DatabaseConnectionFactory.get_connection(
                connection_string="postgresql://user:pass@host:5432/db"
            )
            mock_connect.assert_called_once_with(
                dsn="postgresql://user:pass@host:5432/db"
            )
            assert conn is not None

    def test_get_connection_import_error(self):
        """Test ImportError when psycopg2 not available."""
        with patch.dict("sys.modules", {"psycopg2": None}):
            with pytest.raises(ImportError):
                DatabaseConnectionFactory.get_connection()


class TestConnectionContextManager:
    """Test connection context manager."""

    def test_context_manager_commits_on_success(self):
        """Test that context manager commits on successful exit."""
        mock_conn = MagicMock()
        with patch.object(
            DatabaseConnectionFactory, "get_connection", return_value=mock_conn
        ):
            with DatabaseConnectionFactory.connection() as conn:
                assert conn is mock_conn

            mock_conn.commit.assert_called_once()
            mock_conn.close.assert_called_once()
            mock_conn.rollback.assert_not_called()

    def test_context_manager_rollback_on_exception(self):
        """Test that context manager rolls back on exception."""
        mock_conn = MagicMock()
        with patch.object(
            DatabaseConnectionFactory, "get_connection", return_value=mock_conn
        ):
            with pytest.raises(ValueError):
                with DatabaseConnectionFactory.connection():
                    raise ValueError("test error")

            mock_conn.rollback.assert_called_once()
            mock_conn.close.assert_called_once()
            mock_conn.commit.assert_not_called()

    def test_context_manager_always_closes(self):
        """Test that connection is always closed."""
        mock_conn = MagicMock()
        mock_conn.commit.side_effect = Exception("commit failed")
        with patch.object(
            DatabaseConnectionFactory, "get_connection", return_value=mock_conn
        ):
            with pytest.raises(Exception):
                with DatabaseConnectionFactory.connection():
                    pass

            mock_conn.close.assert_called_once()


class TestConnectionSemaphore:
    """Test connection pool semaphore limiting."""

    def test_semaphore_default_limit(self):
        """Semaphore allows _MAX_POOL_CONN concurrent connections."""
        assert _MAX_POOL_CONN == 20

    def test_semaphore_released_on_success(self):
        """Semaphore is released after successful context manager exit."""
        mock_conn = MagicMock()
        with patch.object(
            DatabaseConnectionFactory, "get_connection", return_value=mock_conn
        ):
            with DatabaseConnectionFactory.connection():
                pass
        # If semaphore leaked, subsequent acquires would eventually block.
        # Quick check: acquire and release immediately.
        assert _conn_semaphore.acquire(timeout=0.1)
        _conn_semaphore.release()

    def test_semaphore_released_on_exception(self):
        """Semaphore is released even when the caller raises."""
        mock_conn = MagicMock()
        with patch.object(
            DatabaseConnectionFactory, "get_connection", return_value=mock_conn
        ):
            with pytest.raises(RuntimeError):
                with DatabaseConnectionFactory.connection():
                    raise RuntimeError("boom")
        assert _conn_semaphore.acquire(timeout=0.1)
        _conn_semaphore.release()

    def test_semaphore_released_on_connect_failure(self):
        """Semaphore is released when get_connection() itself fails."""
        with patch.object(
            DatabaseConnectionFactory,
            "get_connection",
            side_effect=Exception("connection refused"),
        ):
            with pytest.raises(Exception, match="connection refused"):
                with DatabaseConnectionFactory.connection():
                    pass
        assert _conn_semaphore.acquire(timeout=0.1)
        _conn_semaphore.release()

    def test_semaphore_limits_concurrency(self):
        """Threads beyond _MAX_POOL_CONN block until a slot opens."""
        # Use a small semaphore for this test to avoid needing 20+ threads.
        test_sem = threading.BoundedSemaphore(2)
        inside = threading.Event()
        gate = threading.Event()
        blocked = threading.Event()

        def _hold_slot():
            test_sem.acquire()
            try:
                inside.set()
                gate.wait(timeout=5)
            finally:
                test_sem.release()

        def _try_slot():
            got = test_sem.acquire(timeout=0.3)
            if not got:
                blocked.set()
            else:
                test_sem.release()

        # Fill 2 slots
        t1 = threading.Thread(target=_hold_slot)
        t2 = threading.Thread(target=_hold_slot)
        t1.start()
        t2.start()
        inside.wait(timeout=2)

        # Third thread should block
        t3 = threading.Thread(target=_try_slot)
        t3.start()
        t3.join(timeout=2)
        assert blocked.is_set(), "Third thread should have been blocked"

        # Release and clean up
        gate.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
