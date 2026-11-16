"""Identifiers in dynamic SQL are composed, never string-interpolated (S8).

Table and column names cannot be bound as query parameters, so
``receivers db drop-station``'s per-table row-count UNION composes them with
``psycopg2.sql.Identifier`` and lets the driver quote them. The inputs are
internal (information_schema table names), so this is defence-in-depth rather
than a live injection; these tests keep it that way.

Everything here runs against fake cursors — no PostgreSQL required. A
``sql.Composed`` cannot be rendered to text without a live connection
(``as_string`` needs one), so the assertions inspect the composed tree instead:
the name must appear as an ``Identifier`` node and must NOT appear inside any
literal ``SQL`` fragment.
"""

import argparse
from unittest.mock import patch

from psycopg2 import sql

# A name that would be catastrophic if it were pasted into the statement text.
HOSTILE = 'x"; DROP TABLE stations; --'


def _identifiers(composed) -> list[str]:
    """Every identifier string in a composed statement (depth-first)."""
    out: list[str] = []
    for part in getattr(composed, "seq", []):
        if isinstance(part, sql.Identifier):
            out.extend(part.strings)
        elif isinstance(part, sql.Composed):
            out.extend(_identifiers(part))
    return out


def _sql_text(composed) -> str:
    """Concatenation of the fixed SQL fragments (no identifiers, no values)."""
    chunks: list[str] = []
    for part in getattr(composed, "seq", []):
        if isinstance(part, sql.Composed):
            chunks.append(_sql_text(part))
        elif isinstance(part, sql.SQL):
            chunks.append(part.string)
    return "".join(chunks)


class _FakeCur:
    """Records statements; feeds ``fetchone``/``fetchall`` from queued rows."""

    def __init__(self, ones=(), alls=()):
        self.executed: list[tuple] = []
        self._ones = list(ones)
        self._alls = list(alls)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        self.executed.append((stmt, params))

    def fetchone(self):
        return self._ones.pop(0) if self._ones else None

    def fetchall(self):
        return self._alls.pop(0) if self._alls else []


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.closed = False

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class TestDropStationRowCountUnion:
    """The UNION builder emits one Identifier and two bound params per table."""

    def test_union_composes_identifiers_and_pairs_params(self, capsys):
        from receivers.cli import db as cli_db

        tables = ["block_power_status", "file_tracking"]
        cur = _FakeCur(
            ones=[("ELDC",)],  # station-exists probe
            alls=[
                [(t,) for t in tables],  # information_schema lookup
                [("block_power_status", 3), ("file_tracking", 0)],  # counts
            ],
        )
        conn = _FakeConn(cur)

        # db_connection() is the single-host wrapper every ``db`` verb goes through
        with patch.object(cli_db, "db_connection", return_value=conn):
            rc = cli_db.cmd_db_drop_station(
                argparse.Namespace(
                    station_id="eldc", host=None, dry_run=True, force=False
                )
            )
        assert rc == 0

        union_stmt, params = cur.executed[2]
        assert _identifiers(union_stmt) == tables
        # label and sid are bound per branch, in table order — an off-by-one here
        # would only surface against a live DB
        assert params == ("block_power_status", "ELDC", "file_tracking", "ELDC")
        assert "block_power_status" not in _sql_text(union_stmt)
        assert _sql_text(union_stmt).count("UNION ALL") == 1

        out = capsys.readouterr().out
        assert "dry run" in out
        assert conn.closed
