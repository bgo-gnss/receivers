"""Centralized PostgreSQL connection management for GPS health monitoring.

Provides a single source of truth for database connection parameters,
replacing duplicated connection code across db_writer.py, file_tracker.py,
json_importer.py, bulk_scheduler.py, status_task.py, and main.py.

Usage:
    from receivers.health.database_factory import DatabaseConnectionFactory

    # Context manager (recommended)
    with DatabaseConnectionFactory.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")

    # Direct connection
    conn = DatabaseConnectionFactory.get_connection()
    try:
        ...
    finally:
        conn.close()

    # Just get params (for classes that manage their own connections)
    params = DatabaseConnectionFactory.get_connection_params()
"""

import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

logger = logging.getLogger(__name__)

# Type alias to avoid module-level psycopg2 import
Connection = Any


class DatabaseConnectionFactory:
    """Centralized PostgreSQL connection management.

    All database connection parameters are read from environment variables
    with sensible defaults for the GPS health monitoring system.

    Environment Variables:
        POSTGRES_HOST: Database host (default: localhost)
        POSTGRES_PORT: Database port (default: 5432)
        POSTGRES_DB: Database name (default: gps_health)
        POSTGRES_USER: Database user (default: $USER or bgo)
        POSTGRES_PASSWORD: Database password (default: empty)
    """

    @classmethod
    def get_connection_params(cls, database: Optional[str] = None) -> Dict[str, str]:
        """Get connection parameters from environment.

        Args:
            database: Override database name. If None, uses POSTGRES_DB env var.

        Returns:
            Dict with host, port, database, user, password keys.
        """
        return {
            "host": os.getenv("POSTGRES_HOST", "localhost"),
            "port": os.getenv("POSTGRES_PORT", "5432"),
            "database": database or os.getenv("POSTGRES_DB", "gps_health"),
            "user": os.getenv("POSTGRES_USER", os.getenv("USER", "bgo")),
            "password": os.getenv("POSTGRES_PASSWORD", ""),
        }

    @classmethod
    def get_connection(
        cls,
        database: Optional[str] = None,
        connection_string: Optional[str] = None,
    ) -> Connection:
        """Get a new database connection.

        Args:
            database: Override database name.
            connection_string: Full connection string (overrides env vars).

        Returns:
            psycopg2 connection object.

        Raises:
            ImportError: If psycopg2 is not installed.
            psycopg2.OperationalError: If connection fails.
        """
        import psycopg2

        if connection_string:
            return psycopg2.connect(dsn=connection_string)

        params = cls.get_connection_params(database)
        return psycopg2.connect(**params)

    @classmethod
    @contextmanager
    def connection(
        cls,
        database: Optional[str] = None,
        connection_string: Optional[str] = None,
    ) -> Generator[Connection, None, None]:
        """Context manager for safe connection lifecycle.

        Commits on success, rolls back on exception, always closes.

        Args:
            database: Override database name.
            connection_string: Full connection string (overrides env vars).

        Yields:
            psycopg2 connection object.
        """
        conn = cls.get_connection(database, connection_string)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
