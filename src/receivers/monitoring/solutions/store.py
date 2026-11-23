"""Write a check report to one or more databases.

Each destination is written in its own connection and its own transaction via
``record_solution_check(payload)``, which installs the whole snapshot or
nothing. Destinations are EXPLICIT (owner decision G8, 2026-11-21): there is no
reliance on the connection factory's silent mirror, and a failure on one
destination is reported, never swallowed, and never undoes another.

This module never reads ``database.cfg`` or the config singleton. By default it
refuses any destination that is not a disposable database on this machine, and
the production database name. With ``production=True`` (the CLI's
``--production``) it accepts exactly the production destinations and nothing
else: ``gps_health`` on this machine (the rek-d01 timer) and ``gps_health`` on
pgdev.vedur.is (the second destination, G8). A typo therefore never lands in a
third database.
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any

LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})
#: never written without production=True, even on localhost (the laptop's live dev DB)
PROTECTED_DBNAMES = frozenset({"gps_health"})
#: the only remote host a production run may write to (G8: explicit second destination)
PRODUCTION_REMOTE_HOSTS = frozenset({"pgdev.vedur.is"})
STATEMENT_TIMEOUT_MS = 60_000


class DestinationRefusedError(ValueError):
    """A destination the store will not write to."""


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


def check_destination(dsn: str, *, production: bool = False) -> None:
    """Raise :class:`DestinationRefusedError` unless ``dsn`` may be written.

    Default: a disposable database on this machine, never ``gps_health``.
    ``production=True``: ``gps_health`` on this machine or on pgdev.vedur.is,
    and nothing else.
    """
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
    if production:
        _check_production(dsn, host, p.get("dbname", ""))
        return
    for h in host.split(","):
        if not _is_local(h):
            raise DestinationRefusedError(
                f"{describe(dsn)}: slice 2 writes to local disposable databases only"
            )
    if p.get("dbname", "") in PROTECTED_DBNAMES or not p.get("dbname"):
        raise DestinationRefusedError(
            f"{describe(dsn)}: an explicit, non-production dbname is required"
        )


def _is_local(host: str) -> bool:
    return host in LOCAL_HOSTS - {""} or host.startswith("/")


def _check_production(dsn: str, host: str, dbname: str) -> None:
    hosts = host.split(",")
    if len(hosts) != 1:
        raise DestinationRefusedError(
            f"{describe(dsn)}: one host per production destination"
        )
    if dbname not in PROTECTED_DBNAMES:
        raise DestinationRefusedError(
            f"{describe(dsn)}: --production writes only to "
            f"{', '.join(sorted(PROTECTED_DBNAMES))}"
        )
    if not (_is_local(hosts[0]) or hosts[0] in PRODUCTION_REMOTE_HOSTS):
        raise DestinationRefusedError(
            f"{describe(dsn)}: --production writes only to this machine or "
            f"{', '.join(sorted(PRODUCTION_REMOTE_HOSTS))}"
        )


def record_check(dsn: str, payload: dict[str, Any], *, production: bool = False) -> str:
    """Record ``payload`` in one database; return the function's status text."""
    import psycopg2

    check_destination(dsn, production=production)
    conn = psycopg2.connect(dsn, connect_timeout=10)
    try:
        with conn:  # commit on success, roll back on any error
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                cur.execute(
                    "SELECT record_solution_check(%s::jsonb)", (json.dumps(payload),)
                )
                row = cur.fetchone()
                if "station_info" in payload:  # migration 078; same transaction
                    cur.execute(
                        "SELECT record_solution_stations(%s::jsonb)",
                        (json.dumps(payload),),
                    )
                if "completions" in payload:  # migration 079; same transaction
                    cur.execute(
                        "SELECT record_solution_completions(%s::jsonb)",
                        (json.dumps(payload),),
                    )
        return str(row[0]) if row else "no-status"
    finally:
        conn.close()


def record_to_destinations(
    dsns: list[str], payload: dict[str, Any], *, production: bool = False
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
            status = record_check(dsn, payload, production=production)
        except Exception as exc:  # report every failure, keep going
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            results.append(
                DestinationResult(label, False, None, f"{type(exc).__name__}: {first}")
            )
        else:
            results.append(DestinationResult(label, True, status, None))
    return results
