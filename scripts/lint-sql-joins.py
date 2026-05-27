#!/usr/bin/env python3
"""Lint SQL files + Grafana dashboards for the cartesian-join footgun.

Defends against the 2026-05-27 incident where a query joining three
``block_*_status`` tables on ``USING (sid)`` (without ``ts``) produced a
multi-billion-row plan and took ``pgdev.vedur.is`` down. See
``CLAUDE.md`` → "Querying gps_health" for the protocol.

Rule (alias-scoped — applies only when ≥2 *direct* references to
``block_*_status`` tables share a statement):

* ``USING (...)`` on a direct-block alias that includes ``sid`` must also
  include ``ts``.
* ``ON`` clauses with a ``sid``-equality between two direct-block aliases
  must also contain the matching ``ts``-equality between those same aliases.

Joins involving a CTE alias (e.g. ``latest_power lp`` built with
``DISTINCT ON (sid)``) are not flagged — the CTE pre-collapses to one row
per ``sid``, so ``ON s.sid = lp.sid`` is safe. This is intentional and
matches the audited-safe pattern used throughout ``migrations/029`` and
``sql/health_views.sql``.

Known V1 gap: joins that live entirely inside a CTE body are NOT scanned
(the audit confirmed no such pattern exists today). Revisit if that
changes.

Usage::

    scripts/lint-sql-joins.py [PATH ...]

If no paths are given, the script scans ``migrations/``, ``sql/``, and
``docs/grafana/*.json``. Exit code 1 if any findings, 0 otherwise.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# SQL keywords that can immediately follow a table reference and must not be
# mistaken for an alias.  Filtered at the Python level to keep the regexes
# readable.
_ALIAS_BLACKLIST = frozenset(
    {
        "on",
        "using",
        "where",
        "group",
        "order",
        "having",
        "limit",
        "offset",
        "union",
        "intersect",
        "except",
        "returning",
        "left",
        "right",
        "inner",
        "full",
        "outer",
        "cross",
        "natural",
        "join",
        "as",
        "with",
        "select",
        "from",
        "and",
        "or",
    }
)


def _clean_alias(raw: str | None, fallback: str) -> str:
    """Return ``raw`` lowercased if it's a valid alias, else ``fallback``."""
    if raw is None or raw.lower() in _ALIAS_BLACKLIST:
        return fallback.lower()
    return raw.lower()


# FROM/JOIN <block_*_status> [AS] <alias>?  → records the alias (or table
# name) and binds it to the underlying block table.
_DIRECT_BLOCK_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(block_[A-Za-z0-9_]+_status)\b(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?",
    re.IGNORECASE,
)

# JOIN <table> [AS <alias>] (USING (...) | ON <body>) — body runs up to
# the next clause-terminating keyword or end of statement. Non-greedy
# DOTALL so multi-line ON clauses are captured.
_JOIN_CLAUSE_RE = re.compile(
    r"\b(?:(?:LEFT|RIGHT|INNER|FULL|OUTER|CROSS|NATURAL)\s+)?JOIN\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?"
    r"\s+(?:"
    r"(?:USING\s*\(([^)]*)\))"
    r"|"
    r"(?:ON\s+(.+?))"
    r")"
    r"(?="
    r"\s*(?:(?:LEFT|RIGHT|INNER|FULL|OUTER|CROSS|NATURAL)\s+)?JOIN\b|"
    r"\s*\bWHERE\b|\s*\bGROUP\s+BY\b|\s*\bORDER\s+BY\b|\s*\bHAVING\b|"
    r"\s*\bLIMIT\b|\s*\bOFFSET\b|\s*\bUNION\b|\s*\bINTERSECT\b|"
    r"\s*\bEXCEPT\b|\s*\bRETURNING\b|\Z"
    r")",
    re.IGNORECASE | re.DOTALL,
)

# Within an ON body: capture LHS and RHS aliases of every sid- / ts-equality.
_SID_EQ_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\.sid\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\.sid\b",
    re.IGNORECASE,
)
_TS_EQ_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\.ts\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\.ts\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    path: str
    locator: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.locator}: {self.message}"


# ── Comment + statement handling ──────────────────────────────────────────────


def strip_comments(sql: str) -> str:
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub("", sql)
    return sql


# ── Per-statement check ───────────────────────────────────────────────────────


def direct_block_aliases(statement: str) -> set[str]:
    """Return the set of aliases (or table names if no alias) that bind
    directly to a ``block_*_status`` table in this statement."""
    aliases = set()
    for match in _DIRECT_BLOCK_RE.finditer(statement):
        aliases.add(_clean_alias(match.group(2), match.group(1)))
    return aliases


def _check_using(
    joined_alias: str,
    column_list: str,
    direct_blocks: set[str],
) -> str | None:
    """Return a violation message if this USING clause is dangerous."""
    if joined_alias.lower() not in direct_blocks:
        return None
    cols = {c.strip().lower() for c in column_list.split(",")}
    if "sid" not in cols or "ts" in cols:
        return None
    return (
        f"USING ({column_list.strip()}) joins direct-block alias "
        f"`{joined_alias}` on sid without ts — would multiply rows "
        "across all timestamps. Use `USING (sid, ts)`."
    )


def _check_on(body: str, direct_blocks: set[str]) -> list[str]:
    """Return violation messages for any direct-block ``sid``-equality
    in ``body`` that lacks a matching direct-block ``ts``-equality."""
    messages: list[str] = []
    ts_pairs = {frozenset({a.lower(), b.lower()}) for a, b in _TS_EQ_RE.findall(body)}
    seen: set[frozenset[str]] = set()
    for lhs, rhs in _SID_EQ_RE.findall(body):
        lhs_l, rhs_l = lhs.lower(), rhs.lower()
        if lhs_l not in direct_blocks or rhs_l not in direct_blocks:
            continue
        pair = frozenset({lhs_l, rhs_l})
        if pair in seen:
            continue
        seen.add(pair)
        if pair in ts_pairs:
            continue
        messages.append(
            f"ON `{lhs}.sid = {rhs}.sid` aligns sid between two direct-block "
            f"aliases but the same ON clause lacks `{lhs}.ts = {rhs}.ts` "
            "(or the equivalent). Without ts alignment the join is cartesian."
        )
    return messages


def check_statement(statement: str) -> list[str]:
    direct_blocks = direct_block_aliases(statement)
    if len(direct_blocks) < 2:
        return []

    messages: list[str] = []
    for join_match in _JOIN_CLAUSE_RE.finditer(statement):
        joined_table = join_match.group(1)
        joined_alias = _clean_alias(join_match.group(2), joined_table)
        using_cols = join_match.group(3)
        on_body = join_match.group(4)

        if using_cols is not None:
            msg = _check_using(joined_alias, using_cols, direct_blocks)
            if msg:
                messages.append(msg)
        elif on_body is not None:
            messages.extend(_check_on(on_body, direct_blocks))

    return messages


# ── File scanners ─────────────────────────────────────────────────────────────


def scan_sql_text(path: Path, sql: str) -> list[Finding]:
    cleaned = strip_comments(sql)
    findings: list[Finding] = []
    cursor = 0
    for raw in cleaned.split(";"):
        statement = raw.strip()
        if statement:
            for msg in check_statement(statement):
                # Locate statement start to compute line number.
                idx = cleaned.find(statement, cursor)
                if idx == -1:
                    idx = cursor
                line = cleaned.count("\n", 0, idx) + 1
                findings.append(Finding(str(path), f"line {line}", msg))
        cursor += len(raw) + 1  # +1 for the consumed ';'
    return findings


def _walk_strings(node: object, path: list[str]) -> Iterable[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, path + [str(key)])
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk_strings(value, path + [f"[{i}]"])
    elif isinstance(node, str):
        yield "".join(_render_path(path)), node


def _render_path(parts: list[str]) -> list[str]:
    out: list[str] = []
    for p in parts:
        if p.startswith("["):
            out.append(p)
        else:
            if out:
                out.append(".")
            out.append(p)
    return out


def scan_grafana_dashboard(path: Path) -> list[Finding]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return [Finding(str(path), "line 1", f"invalid JSON: {e}")]

    findings: list[Finding] = []
    for jsonpath, value in _walk_strings(data, []):
        if not (jsonpath.endswith("rawSql") or jsonpath.endswith("expr")):
            continue
        cleaned = strip_comments(value)
        for raw in cleaned.split(";"):
            statement = raw.strip()
            if not statement:
                continue
            for msg in check_statement(statement):
                findings.append(Finding(str(path), jsonpath, msg))
    return findings


# ── Entry point ───────────────────────────────────────────────────────────────


DEFAULT_GLOBS = (
    "migrations/*.sql",
    "sql/*.sql",
    "docs/grafana/*.json",
)


def resolve_paths(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(p) for p in argv]
    root = Path(__file__).parent.parent
    paths: list[Path] = []
    for glob in DEFAULT_GLOBS:
        paths.extend(sorted(root.glob(glob)))
    return paths


def scan_file(path: Path) -> list[Finding]:
    if path.suffix == ".sql":
        return scan_sql_text(path, path.read_text())
    if path.suffix == ".json":
        return scan_grafana_dashboard(path)
    return []


def main(argv: list[str]) -> int:
    findings: list[Finding] = []
    for path in resolve_paths(argv):
        if path.is_file():
            findings.extend(scan_file(path))

    for finding in findings:
        print(finding.render(), file=sys.stderr)

    if findings:
        print(
            f"\nlint-sql-joins: {len(findings)} finding(s). "
            "See receivers/CLAUDE.md → 'Querying gps_health' for the rule.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
