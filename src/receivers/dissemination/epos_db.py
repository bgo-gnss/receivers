"""EPOS GNSS DB connection — the dedicated, non-gps_health Postgres link.

Decision #1 (see docs/architecture/epos-dissemination-plan.md): a dedicated
connection, NOT a ``DatabaseConnectionFactory`` override — the EPOS DB is a
different server/user/creds and must not mutate the global ``POSTGRES_HOST`` env
or trigger the gps_health mirror dual-write.

Config comes from a ``[epos_db]`` section in ``database.cfg`` (the same file the
gps_health factory reads; never synced, so credentials stay local — mirrors the
existing ``[tos]`` precedent). The ``schema`` setting handles the
dev-vs-prod layout difference: on dev/local the tables live in ``public`` (the
GNSS data is its own database, ``gnss-europe-v0-2-9``); on prod they live in a
schema literally named ``gnss-europe-v0-2-9`` inside the ``epos`` database. We set
``search_path`` to that schema on connect, so all SQL references tables unqualified.

All callers use parameterized SQL (psycopg2 ``%s``); the helpers here never format
values into the statement text.
"""

from __future__ import annotations

import configparser
import logging
import os
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

logger = logging.getLogger("receivers.dissemination.epos_db")

DEFAULT_SCHEMA = "public"


def _config_path() -> Path:
    config_dir = os.getenv("GPS_CONFIG_PATH")
    base = Path(config_dir) if config_dir else Path.home() / ".config" / "gpsconfig"
    return base / "database.cfg"


def get_epos_config(overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Resolve EPOS DB connection params: overrides > env > ``[epos_db]`` > defaults.

    Env vars: ``EPOS_DB_HOST/PORT/NAME/USER/PASSWORD/SCHEMA``. Returns a dict with
    host, port, dbname, user, password, schema.
    """
    cfg: dict[str, str] = {}
    path = _config_path()
    if path.exists():
        parser = configparser.ConfigParser()
        parser.read(path)
        if parser.has_section("epos_db"):
            cfg = dict(parser.items("epos_db"))

    resolved = {
        "host": os.getenv("EPOS_DB_HOST", cfg.get("host", "localhost")),
        "port": os.getenv("EPOS_DB_PORT", cfg.get("port", "5432")),
        "dbname": os.getenv("EPOS_DB_NAME", cfg.get("database", cfg.get("dbname", ""))),
        "user": os.getenv("EPOS_DB_USER", cfg.get("user", os.getenv("USER", ""))),
        "password": os.getenv("EPOS_DB_PASSWORD", cfg.get("password", "")),
        "schema": os.getenv("EPOS_DB_SCHEMA", cfg.get("schema", DEFAULT_SCHEMA)),
    }
    if overrides:
        resolved.update(overrides)
    return resolved


_CONN_SCHEMAS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def connect(overrides: Optional[dict[str, Any]] = None) -> Any:
    """Open a psycopg2 connection to the EPOS DB with ``search_path`` set.

    ``overrides`` (e.g. for tests pointing at the local copy) take precedence over
    config/env. The password is omitted from the connect call when empty so libpq
    falls back to ``~/.pgpass``.
    """
    import psycopg2

    params = get_epos_config(overrides)
    schema = params.pop("schema") or DEFAULT_SCHEMA
    if not params.get("password"):
        params.pop("password", None)  # let ~/.pgpass supply it
    conn = psycopg2.connect(**params)
    with conn.cursor() as cur:
        # Quote the schema (the prod name 'gnss-europe-v0-2-9' has hyphens).
        cur.execute(f'SET search_path TO "{schema}"')
    conn.commit()
    # Remember the schema so recover() can re-assert the search_path after a
    # transaction abort (a failed statement poisons the psycopg2 transaction;
    # everything after it fails with 'current transaction is aborted' until a
    # rollback — and the rollback must not leave the session without its path).
    # psycopg2 connections are C objects (no attribute assignment) — track in
    # a weak-keyed side table that dies with the connection.
    _CONN_SCHEMAS[conn] = schema
    return conn


def recover(conn: Any) -> bool:
    """Return a poisoned connection to a usable state (rollback + search_path).

    Call after any statement error on a long-lived EPOS connection (sweeps
    reuse one connection across thousands of index calls — one duplicate-key
    error must not kill indexing for the rest of the run). Best-effort;
    returns False when the connection is beyond recovery (caller reconnects).
    """
    try:
        conn.rollback()
        schema = _CONN_SCHEMAS.get(conn) or DEFAULT_SCHEMA
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
        conn.commit()
        return True
    except Exception:  # noqa: BLE001 - dead connection
        return False


@contextmanager
def managed(overrides: Optional[dict[str, Any]] = None) -> Generator[Any, None, None]:
    """Context manager: commit on success, rollback on error, always close."""
    conn = connect(overrides)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── insert/upsert helpers (constraint- and sequence-agnostic) ─────────────────
#
# The dev schema lacks the UNIQUE constraints the legacy ON CONFLICT upserts
# assumed, and some tables' ``id`` columns have no default. So we compute the next
# id explicitly and emulate get-or-create with a SELECT — works on any of the
# dev/local/prod schemas. Single-threaded ETL, so max(id)+1 is safe.


def insert_row(cur, table: str, values: dict[str, Any]) -> int:
    """INSERT ``values`` into ``table`` with an explicit next id; return the id."""
    cur.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}")
    new_id = cur.fetchone()[0]
    cols = ["id"] + list(values.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    params = [new_id] + list(values.values())
    cur.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
        params,
    )
    return int(cur.fetchone()[0])


def get_or_create(
    cur, table: str, match: dict[str, Any], extra: Optional[dict[str, Any]] = None
) -> int:
    """Return the id of the row matching ``match``, inserting it if absent.

    ``match`` are the identifying columns (looked up with ``=`` / ``IS NULL``);
    ``extra`` are additional columns set only on insert.
    """
    where = " AND ".join(
        f"{k} IS NULL" if v is None else f"{k} = %s" for k, v in match.items()
    )
    params = [v for v in match.values() if v is not None]
    cur.execute(f"SELECT id FROM {table} WHERE {where} LIMIT 1", params)
    row = cur.fetchone()
    if row is not None:
        return int(row[0])
    return insert_row(cur, table, {**match, **(extra or {})})


def update_row(cur, table: str, row_id: int, values: dict[str, Any]) -> None:
    """UPDATE ``table`` row ``row_id`` with ``values`` (parameterized)."""
    if not values:
        return
    sets = ", ".join(f"{k} = %s" for k in values)
    cur.execute(
        f"UPDATE {table} SET {sets} WHERE id = %s", list(values.values()) + [row_id]
    )
