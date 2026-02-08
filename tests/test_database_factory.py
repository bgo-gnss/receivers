"""Tests for DatabaseConnectionFactory."""

import os
import pytest
from unittest.mock import patch, MagicMock

from receivers.health.database_factory import DatabaseConnectionFactory


class TestGetConnectionParams:
    """Test get_connection_params method."""

    def test_default_params(self):
        """Test default connection parameters."""
        with patch.dict(os.environ, {}, clear=True):
            # Clear all POSTGRES_* vars, set USER
            env = {"USER": "testuser"}
            with patch.dict(os.environ, env, clear=True):
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

    def test_user_fallback_to_bgo(self):
        """Test USER fallback when no env vars set."""
        with patch.dict(os.environ, {}, clear=True):
            params = DatabaseConnectionFactory.get_connection_params()
            assert params["user"] == "bgo"


class TestGetConnection:
    """Test get_connection method."""

    @patch("receivers.health.database_factory.DatabaseConnectionFactory.get_connection_params")
    def test_get_connection_with_params(self, mock_params):
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
                with DatabaseConnectionFactory.connection() as conn:
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
