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
    host_override: str | None = None,
    database: str | None = None,
) -> Any:
    """Get a database connection using centralized config.

    Uses DatabaseConnectionFactory for connection params with optional
    host override for pointing at different servers.

    When ``host_override`` targets the configured ``mirror_host``, the
    connection uses that host's declared identity (``mirror_user`` + its
    ``~/.pgpass`` credential), NOT the primary user — so a ``--catalog-prod``
    reindex reaches the mirror the same way the mirror writer does. Credential
    resolution lives in :meth:`DatabaseConnectionFactory.get_connection_params_for_host`
    (database.cfg is the single source of truth for per-host access).

    Args:
        host_override: Override the configured host (e.g., 'pgdev.vedur.is').
        database: Override the database name (default: gps_health).

    Returns:
        psycopg2 connection object.

    Raises:
        ImportError: If psycopg2 is not installed.
        psycopg2.OperationalError: If connection fails.
    """
    from ..health.database_factory import DatabaseConnectionFactory

    if host_override:
        # Single direct connection to the specific host, resolving that
        # host's credentials from database.cfg (mirror_host → mirror_user).
        return DatabaseConnectionFactory.connect_to_host(
            host_override, database=database or "gps_health"
        )

    return DatabaseConnectionFactory.get_connection(database=database or "gps_health")


@contextmanager
def managed_connection(
    host_override: str | None = None,
    database: str | None = None,
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
