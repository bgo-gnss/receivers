"""Tests for receivers.cli.health_query.

Focused on the EXPLAIN-gate logic — the CLI plumbing is intentionally thin
so most of the surface is covered by these pure-function tests.
"""

from __future__ import annotations

import argparse

import pytest

from receivers.cli.arguments import create_argument_parser
from receivers.cli.health_query import (
    DEFAULT_MAX_COST,
    DEFAULT_MAX_ROWS,
    _coerce_ceiling,
    _resolve_sql,
    parse_explain_estimates,
)

# ── parse_explain_estimates ───────────────────────────────────────────────────


def test_parse_simple_seq_scan():
    text = "Seq Scan on stations  (cost=0.00..4.50 rows=173 width=64)"
    max_rows, top_cost = parse_explain_estimates(text)
    assert max_rows == 173
    assert top_cost == 4.50


def test_parse_picks_max_rows_across_nodes_not_top_aggregate():
    # Pattern from the 2026-05-27 cartesian incident: top Aggregate shows
    # rows=1 (it's a COUNT), inner join node shows the catastrophic estimate.
    text = """
Aggregate  (cost=999999.50..999999.51 rows=1 width=8)
  ->  Nested Loop  (cost=0.00..899999.00 rows=27139881911 width=0)
        ->  Seq Scan on block_a_status p  (cost=0.00..120.00 rows=4500 width=8)
        ->  Seq Scan on block_b_status r  (cost=0.00..145.00 rows=6031 width=8)
""".strip()
    max_rows, top_cost = parse_explain_estimates(text)
    assert max_rows == 27_139_881_911, (
        "must pick max rows across ALL nodes, not the top aggregate's rows=1"
    )
    # First cost= encountered is the top-of-plan total cost.
    assert top_cost == 999999.51


def test_parse_realistic_safe_count():
    text = """
Aggregate  (cost=8431.21..8431.22 rows=1 width=8)
  ->  Seq Scan on stations  (cost=0.00..8430.00 rows=999344 width=0)
""".strip()
    max_rows, top_cost = parse_explain_estimates(text)
    assert max_rows == 999_344
    assert top_cost == 8431.22


def test_parse_raises_when_no_rows():
    with pytest.raises(ValueError, match="row estimates"):
        parse_explain_estimates("ERROR: relation does not exist")


def test_parse_raises_when_no_cost():
    # Synthetic: contains rows= but no cost=A..B
    with pytest.raises(ValueError, match="cost"):
        parse_explain_estimates("custom rows=100 widget")


# ── ceiling parsing ───────────────────────────────────────────────────────────


def test_coerce_ceiling_accepts_scientific_notation():
    assert _coerce_ceiling("1e8") == 1e8
    assert _coerce_ceiling("1.5e9") == 1.5e9


def test_coerce_ceiling_accepts_plain_integer_string():
    assert _coerce_ceiling("100000000") == 1e8


def test_coerce_ceiling_accepts_numeric_types():
    assert _coerce_ceiling(1_000_000) == 1_000_000.0
    assert _coerce_ceiling(2.5) == 2.5


def test_coerce_ceiling_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        _coerce_ceiling("not-a-number")


# ── SQL resolution ────────────────────────────────────────────────────────────


def _ns(**kw) -> argparse.Namespace:
    """Build a Namespace with default-Falsey fields for sql resolution."""
    base = {"sql": [], "file": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_resolve_sql_from_positional_args():
    ns = _ns(sql=["SELECT", "1"])
    assert _resolve_sql(ns) == "SELECT 1"


def test_resolve_sql_from_file(tmp_path):
    f = tmp_path / "q.sql"
    f.write_text("SELECT 42\n")
    ns = _ns(file=str(f))
    assert _resolve_sql(ns) == "SELECT 42\n"


def test_resolve_sql_missing_file_returns_none(tmp_path, capsys):
    ns = _ns(file=str(tmp_path / "does-not-exist.sql"))
    assert _resolve_sql(ns) is None
    assert "cannot read" in capsys.readouterr().err


def test_resolve_sql_empty_returns_none(capsys):
    ns = _ns()
    assert _resolve_sql(ns) is None
    assert "no SQL provided" in capsys.readouterr().err


# ── argparse wiring ───────────────────────────────────────────────────────────


def _parse(argv: list[str]) -> argparse.Namespace:
    return create_argument_parser().parse_args(argv)


def test_health_query_subcommand_is_registered():
    args = _parse(["health-query", "SELECT 1"])
    assert args.command == "health-query"
    assert args.sql == ["SELECT 1"]
    # defaults
    assert args.no_explain is False
    assert args.explain_only is False
    assert float(args.max_rows) == DEFAULT_MAX_ROWS
    assert float(args.max_cost) == DEFAULT_MAX_COST


def test_health_query_no_explain_and_explain_only_are_mutex():
    parser = create_argument_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["health-query", "--explain-only", "--no-explain", "SELECT 1"]
        )


def test_health_query_accepts_file_argument(tmp_path):
    f = tmp_path / "q.sql"
    f.write_text("SELECT 1")
    args = _parse(["health-query", "-f", str(f)])
    assert args.file == str(f)
    assert args.sql == []


def test_health_query_raises_max_rows_via_flag():
    args = _parse(["health-query", "--max-rows", "1e10", "SELECT 1"])
    assert float(args.max_rows) == 1e10
