"""Tests for scripts/lint-sql-joins.py.

Imports the script by file path (hyphenated name) and exercises both the
positive cases (would-have-caught the 2026-05-27 incident) and the
negative cases (the audited-safe CTE pre-collapse pattern used throughout
migrations/029 and sql/health_views.sql).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts" / "lint-sql-joins.py"

_spec = importlib.util.spec_from_file_location("lint_sql_joins", _SCRIPT)
assert _spec is not None and _spec.loader is not None
lint = importlib.util.module_from_spec(_spec)
sys.modules["lint_sql_joins"] = lint
_spec.loader.exec_module(lint)


# ── positive cases (must flag) ────────────────────────────────────────────────


def test_incident_using_sid_only():
    """The 2026-05-27 cartesian-join pattern — USING (sid) across three blocks."""
    sql = """
        SELECT count(*) FROM block_power_status p
        JOIN block_receiver_status r USING (sid)
        JOIN block_ping_status pg USING (sid)
        WHERE ts > now() - interval '7 days'
    """
    messages = lint.check_statement(sql.strip())
    assert len(messages) == 2
    assert all("USING (sid)" in m for m in messages)
    assert all("direct-block alias" in m for m in messages)


def test_incident_on_sid_only():
    """Variant: ON a.sid = b.sid with no ts equality."""
    sql = """
        SELECT count(*) FROM block_power_status p
        JOIN block_receiver_status r ON p.sid = r.sid
        WHERE p.ts > now() - interval '7 days'
    """
    messages = lint.check_statement(sql.strip())
    assert len(messages) == 1
    assert "p.sid = r.sid" in messages[0]
    assert "p.ts = r.ts" in messages[0]


def test_mixed_safe_and_unsafe_joins_flags_only_unsafe():
    """The motivating mixed case — one safe pair + one unsafe pair must
    flag exactly the unsafe pair, not silently pass because some ts=ts
    appears elsewhere in the statement."""
    sql = """
        SELECT * FROM block_power_status a
        JOIN block_receiver_status b ON a.sid = b.sid AND a.ts = b.ts
        JOIN block_ping_status c ON a.sid = c.sid
        WHERE a.ts > now() - interval '1 day'
    """
    messages = lint.check_statement(sql.strip())
    assert len(messages) == 1
    assert "a.sid = c.sid" in messages[0]


def test_incident_query_via_main_emits_findings(tmp_path, capsys):
    """End-to-end via the CLI entry point — must exit 1 with messages on stderr."""
    sql_file = tmp_path / "bad.sql"
    sql_file.write_text(
        "SELECT count(*) FROM block_power_status p\n"
        "JOIN block_receiver_status r USING (sid);\n"
    )
    rc = lint.main([str(sql_file)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "USING (sid)" in captured.err
    assert "1 finding" in captured.err


# ── negative cases (must NOT flag) ────────────────────────────────────────────


def test_safe_cte_distinct_on_collapse():
    """The migration 029 / health_views.sql pattern — CTE pre-collapses to
    one row per sid, so joins on sid alone are safe."""
    sql = """
        WITH lp AS (
            SELECT DISTINCT ON (sid) sid, ts, voltage
            FROM block_power_status ORDER BY sid, ts DESC
        ),
        lr AS (
            SELECT DISTINCT ON (sid) sid, ts, cpu
            FROM block_receiver_status ORDER BY sid, ts DESC
        )
        SELECT s.sid, lp.voltage, lr.cpu
        FROM stations s
        LEFT JOIN lp ON s.sid = lp.sid
        LEFT JOIN lr ON s.sid = lr.sid
    """
    assert lint.check_statement(sql.strip()) == []


def test_safe_using_sid_ts():
    sql = """
        SELECT count(*) FROM block_power_status p
        JOIN block_receiver_status r USING (sid, ts)
        WHERE p.ts > now() - interval '1 day'
    """
    assert lint.check_statement(sql.strip()) == []


def test_safe_on_sid_and_ts():
    sql = """
        SELECT count(*) FROM block_power_status p
        FULL JOIN block_receiver_status r ON p.sid = r.sid AND p.ts = r.ts
    """
    assert lint.check_statement(sql.strip()) == []


def test_single_block_table_never_flags():
    """One direct-block table → no possible cartesian → no findings."""
    sql = """
        SELECT * FROM block_power_status p
        JOIN stations s USING (sid)
        WHERE ts > now() - interval '7 days'
    """
    assert lint.check_statement(sql.strip()) == []


def test_main_returns_zero_on_safe_input(tmp_path, capsys):
    sql_file = tmp_path / "good.sql"
    sql_file.write_text(
        "SELECT count(*) FROM block_power_status p\n"
        "JOIN block_receiver_status r USING (sid, ts);\n"
    )
    rc = lint.main([str(sql_file)])
    assert rc == 0
    assert capsys.readouterr().err == ""


# ── parsing helpers ───────────────────────────────────────────────────────────


def test_strip_comments_removes_line_and_block_comments():
    sql = """
        -- this is a line comment about USING (sid)
        SELECT * FROM /* and a block USING (sid) */ block_power_status
    """
    cleaned = lint.strip_comments(sql)
    assert "USING" not in cleaned
    assert "block_power_status" in cleaned


def test_direct_block_aliases_captures_alias_or_table():
    sql = """
        FROM block_power_status p
        JOIN block_receiver_status AS r ON p.sid = r.sid
        JOIN block_ping_status ON ...
    """
    aliases = lint.direct_block_aliases(sql)
    assert aliases == {"p", "r", "block_ping_status"}


def test_grafana_dashboard_scan(tmp_path):
    """Walk a Grafana panel JSON, flag rawSql that contains the bad pattern."""
    import json

    dashboard = {
        "panels": [
            {
                "title": "bad",
                "targets": [
                    {
                        "rawSql": (
                            "SELECT count(*) FROM block_power_status p "
                            "JOIN block_receiver_status r USING (sid)"
                        ),
                    },
                ],
            },
            {
                "title": "good",
                "targets": [
                    {
                        "rawSql": (
                            "SELECT count(*) FROM block_power_status p "
                            "JOIN block_receiver_status r USING (sid, ts)"
                        ),
                    },
                ],
            },
        ],
    }
    path = tmp_path / "dash.json"
    path.write_text(json.dumps(dashboard))
    findings = lint.scan_grafana_dashboard(path)
    assert len(findings) == 1
    assert "USING (sid)" in findings[0].message
    assert findings[0].locator == "panels[0].targets[0].rawSql"


# ── full-repo regression guard ────────────────────────────────────────────────


def test_full_repo_is_clean():
    """The committed corpus must remain finding-free — if this breaks, either
    a real cartesian crept in (fix it!) or the linter regressed (loosen)."""
    rc = lint.main([])  # default globs: migrations/, sql/, docs/grafana/
    assert rc == 0
