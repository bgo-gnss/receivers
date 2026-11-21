"""Write a check report to one or more databases (slice 2: disposable only).

Each destination is written in its own connection and its own transaction via
``record_solution_check(payload)``, which installs the whole snapshot or
nothing. Destinations are EXPLICIT (owner decision G8, 2026-11-21): there is no
reliance on the connection factory's silent mirror, and a failure on one
destination is reported, never swallowed, and never undoes another.

This module never reads ``database.cfg`` or the config singleton. In slice 2 it
also refuses any destination that is not on this machine, and the production
database name, so nothing here can reach rek-d01 or pgdev. Remote destinations
are wired in slice 5, behind the owner's go-ahead.
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any

LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})
#: never written from slice 2, even on localhost (the laptop's live dev DB)
PROTECTED_DBNAMES = frozenset({"gps_health"})
STATEMENT_TIMEOUT_MS = 60_000


class DestinationRefusedError(ValueError):
    """A destination the slice-2 store will not write to."""


@dataclasses.dataclass(frozen=True)
class DestinationResult:
    label: str
    ok: bool
    status: str | None
    error: str | None


def _parse(dsn: str) -> dict[str, str]:
    from psycopg2.extensions import parse_dsn

    try:
        return dict(parse_dsn(dsn))
    except Exception as exc:  # psycopg2.ProgrammingError; never echo the DSN
        raise DestinationRefusedError("unparsable DSN") from exc


def describe(dsn: str) -> str:
    """``host:port/dbname`` without user or password, safe to print."""
    p = _parse(dsn)
    return (
        f"{p.get('host', '') or 'local'}:{p.get('port', '5432')}/{p.get('dbname', '?')}"
    )


def check_destination(dsn: str) -> None:
    """Raise :class:`DestinationRefusedError` unless ``dsn`` is a disposable local DB."""
    p = _parse(dsn)
    # Anything libpq would fill in from elsewhere is refused: without an
    # explicit host it falls back to PGHOST, and hostaddr / a service entry
    # (from the DSN, PGHOSTADDR or PGSERVICE) can point anywhere.
    if p.get("service") or p.get("hostaddr"):
        raise DestinationRefusedError(
            f"{describe(dsn)}: service and hostaddr are not allowed"
        )
    for var in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
        if os.environ.get(var):
            raise DestinationRefusedError(
                f"{describe(dsn)}: refused while {var} is set in the environment"
            )
    host = p.get("host", "")
    if not host:
        raise DestinationRefusedError(
            f"{describe(dsn)}: an explicit local host= is required"
        )
    for h in host.split(","):
        if not (h in LOCAL_HOSTS - {""} or h.startswith("/")):
            raise DestinationRefusedError(
                f"{describe(dsn)}: slice 2 writes to local disposable databases only"
            )
    if p.get("dbname", "") in PROTECTED_DBNAMES or not p.get("dbname"):
        raise DestinationRefusedError(
            f"{describe(dsn)}: an explicit, non-production dbname is required"
        )


def record_check(dsn: str, payload: dict[str, Any]) -> str:
    """Record ``payload`` in one database; return the function's status text."""
    import psycopg2

    check_destination(dsn)
    conn = psycopg2.connect(dsn, connect_timeout=10)
    try:
        with conn:  # commit on success, roll back on any error
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                cur.execute(
                    "SELECT record_solution_check(%s::jsonb)", (json.dumps(payload),)
                )
                row = cur.fetchone()
        return str(row[0]) if row else "no-status"
    finally:
        conn.close()


def record_to_destinations(
    dsns: list[str], payload: dict[str, Any]
) -> list[DestinationResult]:
    """Record ``payload`` in each destination independently, in order."""
    results: list[DestinationResult] = []
    for dsn in dsns:
        try:
            label = describe(dsn)
        except DestinationRefusedError as exc:
            results.append(DestinationResult("?", False, None, str(exc)))
            continue
        try:
            status = record_check(dsn, payload)
        except Exception as exc:  # report every failure, keep going
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            results.append(
                DestinationResult(label, False, None, f"{type(exc).__name__}: {first}")
            )
        else:
            results.append(DestinationResult(label, True, status, None))
    return results
