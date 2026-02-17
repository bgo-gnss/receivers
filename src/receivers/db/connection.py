"""Centralized database connection for GPS receivers.

Thin wrapper around DatabaseConnectionFactory that provides a simple
interface with optional host override for CLI commands.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)


def get_connection(
    host_override: Optional[str] = None,
    database: Optional[str] = None,
) -> Any:
    """Get a database connection using centralized config.

    Uses DatabaseConnectionFactory for connection params with optional
    host override for pointing at different servers.

    Args:
        host_override: Override the configured host (e.g., 'pgdev.vedur.is').
        database: Override the database name (default: gps_health).

    Returns:
        psycopg2 connection object.

    Raises:
        ImportError: If psycopg2 is not installed.
        psycopg2.OperationalError: If connection fails.
    """
    import os

    from ..health.database_factory import DatabaseConnectionFactory

    if host_override:
        # Temporarily set env var to override host
        old_host = os.environ.get("POSTGRES_HOST")
        os.environ["POSTGRES_HOST"] = host_override
        try:
            conn = DatabaseConnectionFactory.get_connection(database=database or "gps_health")
        finally:
            if old_host is not None:
                os.environ["POSTGRES_HOST"] = old_host
            else:
                os.environ.pop("POSTGRES_HOST", None)
        return conn

    return DatabaseConnectionFactory.get_connection(database=database or "gps_health")


@contextmanager
def managed_connection(
    host_override: Optional[str] = None,
    database: Optional[str] = None,
) -> Generator:
    """Context manager for safe connection lifecycle.

    Commits on success, rolls back on exception, always closes.

    Args:
        host_override: Override the configured host.
        database: Override the database name.

    Yields:
        psycopg2 connection object.
    """
    conn = get_connection(host_override=host_override, database=database)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
