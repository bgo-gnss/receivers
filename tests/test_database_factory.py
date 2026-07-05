"""Tests for DatabaseConnectionFactory."""

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from receivers.health.database_factory import (
    _MAX_POOL_CONN,
    DatabaseConnectionFactory,
    _DualConnection,
    _conn_semaphore,
)


class TestGetConnectionParams:
    """Test get_connection_params method."""

    def test_default_params(self):
        """Test default connection parameters."""
        with patch(
            "receivers.health.database_factory._load_config_file", return_value={}
        ):
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
        with patch(
            "receivers.health.database_factory._load_config_file", return_value={}
        ):
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


class _FakePool:
    """Minimal ThreadedConnectionPool stand-in for tests."""

    def __init__(self, conns=None):
        self._conns = list(conns) if conns else []
        self.given = []
        self.returned = []  # list of (conn, close)

    def getconn(self):
        conn = self._conns.pop(0) if self._conns else _live_conn()
        self.given.append(conn)
        return conn

    def putconn(self, conn, close=False):
        self.returned.append((conn, close))

    def closeall(self):
        pass


def _live_conn():
    """A MagicMock connection that passes _ping()."""
    conn = MagicMock()
    conn.closed = 0
    return conn


def _dead_conn():
    """A MagicMock connection that fails _ping() (server dropped it)."""
    conn = MagicMock()
    conn.closed = 1
    return conn


class TestConnectionContextManager:
    """Test the pooled connection context manager."""

    def test_context_manager_commits_and_returns_to_pool(self):
        """Commits on success and returns the connection to the pool."""
        mock_conn = _live_conn()
        pool = _FakePool([mock_conn])
        with patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool), \
             patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None):
            with DatabaseConnectionFactory.connection() as conn:
                assert conn is mock_conn
            mock_conn.commit.assert_called_once()
            # returned to pool, NOT closed (discard=False)
            assert pool.returned == [(mock_conn, False)]
            mock_conn.close.assert_not_called()

    def test_context_manager_discards_poisoned_on_exception(self):
        """Rolls back and DISCARDS (close=True) a connection whose op raised."""
        mock_conn = _live_conn()
        pool = _FakePool([mock_conn])
        with patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool), \
             patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None):
            with pytest.raises(ValueError):
                with DatabaseConnectionFactory.connection():
                    raise ValueError("test error")
            # rollback is called (pre-ping also rolls back, so >=1)
            assert mock_conn.rollback.called
            mock_conn.commit.assert_not_called()
            assert pool.returned == [(mock_conn, True)]  # discarded

    def test_context_manager_returns_even_if_commit_fails(self):
        """A commit failure still returns/discards the connection (no leak)."""
        mock_conn = _live_conn()
        mock_conn.commit.side_effect = Exception("commit failed")
        pool = _FakePool([mock_conn])
        with patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool), \
             patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None):
            with pytest.raises(Exception):
                with DatabaseConnectionFactory.connection():
                    pass
            assert pool.returned and pool.returned[0][0] is mock_conn

    def test_pre_ping_discards_dead_connection(self):
        """A dead pooled connection is discarded; a live one is handed out."""
        dead = _dead_conn()
        live = _live_conn()
        pool = _FakePool([dead, live])
        with patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool), \
             patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None):
            with DatabaseConnectionFactory.connection() as conn:
                assert conn is live
            # dead one was closed-discarded during checkout
            assert (dead, True) in pool.returned
            # live one returned normally
            assert (live, False) in pool.returned

    def test_mirror_yields_dual_connection(self):
        """With a mirror pool, a _DualConnection is yielded and both returned."""
        primary = _live_conn()
        mirror = _live_conn()
        ppool = _FakePool([primary])
        mpool = _FakePool([mirror])
        with patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=ppool), \
             patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=mpool), \
             patch(
                 "receivers.health.database_factory._load_config_file",
                 return_value={"mirror_host": "mirror.example.com"},
             ):
            with DatabaseConnectionFactory.connection() as conn:
                assert isinstance(conn, _DualConnection)
            assert ppool.returned == [(primary, False)]
            assert mpool.returned == [(mirror, False)]

    def test_connection_string_bypasses_pool(self):
        """A full-DSN connection is not pooled — opened and closed directly."""
        mock_conn = MagicMock()
        with patch.object(
            DatabaseConnectionFactory, "get_connection", return_value=mock_conn
        ) as mock_get, \
             patch.object(DatabaseConnectionFactory, "_primary_pool") as mock_pool:
            with DatabaseConnectionFactory.connection(
                connection_string="postgresql://u:p@h/db"
            ) as conn:
                assert conn is mock_conn
            mock_conn.commit.assert_called_once()
            mock_conn.close.assert_called_once()
            mock_pool.assert_not_called()
            mock_get.assert_called_once_with(connection_string="postgresql://u:p@h/db")


class TestConnectionSemaphore:
    """Test connection pool semaphore limiting."""

    def test_semaphore_default_limit(self):
        """Semaphore allows _MAX_POOL_CONN concurrent connections."""
        assert _MAX_POOL_CONN == 20

    def test_semaphore_released_on_success(self):
        """Semaphore is released after successful context manager exit."""
        pool = _FakePool([_live_conn()])
        with patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool), \
             patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None):
            with DatabaseConnectionFactory.connection():
                pass
        # If semaphore leaked, subsequent acquires would eventually block.
        assert _conn_semaphore.acquire(timeout=0.1)
        _conn_semaphore.release()

    def test_semaphore_released_on_exception(self):
        """Semaphore is released even when the caller raises."""
        pool = _FakePool([_live_conn()])
        with patch.object(DatabaseConnectionFactory, "_primary_pool", return_value=pool), \
             patch.object(DatabaseConnectionFactory, "_mirror_pool", return_value=None):
            with pytest.raises(RuntimeError):
                with DatabaseConnectionFactory.connection():
                    raise RuntimeError("boom")
        assert _conn_semaphore.acquire(timeout=0.1)
        _conn_semaphore.release()

    def test_semaphore_released_on_checkout_failure(self):
        """Semaphore is released when the pool checkout itself fails."""
        with patch.object(
            DatabaseConnectionFactory,
            "_primary_pool",
            side_effect=Exception("pool init refused"),
        ):
            with pytest.raises(Exception, match="pool init refused"):
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
