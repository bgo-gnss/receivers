"""Tests for receivers.cli.arguments helpers."""

from receivers.cli.arguments import normalize_station_tokens


def test_normalize_clean_input():
    assert normalize_station_tokens(["AFST", "ENTC"]) == ["AFST", "ENTC"]


def test_normalize_uppercases():
    assert normalize_station_tokens(["afst", "Entc"]) == ["AFST", "ENTC"]


def test_normalize_strips_trailing_commas():
    """Reproduces the comma-paste bug: shell splits on whitespace but commas stay."""
    assert normalize_station_tokens(
        ["AFST,", "ENTC,", "FAGD,", "GOLA,", "HUSM,", "SVIE,", "THOB,", "SEY9"]
    ) == ["AFST", "ENTC", "FAGD", "GOLA", "HUSM", "SVIE", "THOB", "SEY9"]


def test_normalize_splits_comma_list():
    """User pastes the list as a single quoted string with commas."""
    assert normalize_station_tokens(["AFST,ENTC,FAGD"]) == ["AFST", "ENTC", "FAGD"]


def test_normalize_handles_inner_whitespace():
    """User pastes 'AFST, ENTC, FAGD' as a single quoted arg."""
    assert normalize_station_tokens(["AFST, ENTC, FAGD"]) == ["AFST", "ENTC", "FAGD"]


def test_normalize_filters_empty_tokens():
    assert normalize_station_tokens(["AFST", "", ",", "  ", "ENTC"]) == ["AFST", "ENTC"]


def test_normalize_strips_semicolons():
    assert normalize_station_tokens(["AFST;", "ENTC"]) == ["AFST", "ENTC"]


def test_normalize_none_input():
    assert normalize_station_tokens(None) == []


def test_normalize_empty_list():
    assert normalize_station_tokens([]) == []
