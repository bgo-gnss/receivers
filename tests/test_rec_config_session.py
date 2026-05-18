"""Tests for `receivers rec-config --check-session` / `--enable-session`.

The CLI helpers `parse_log_session_state` and `_load_session_template` are
unit-tested directly without touching a real receiver. Sample `getLogSession`
fixtures come from the response format documented in
`src/receivers/health/polarx5_tcp_extractor.py`.
"""

from __future__ import annotations

import argparse

import pytest

from receivers.cli.arguments import setup_rec_config_parser
from receivers.cli.main import (
    _ENABLABLE_SESSIONS,
    _load_session_template,
    parse_log_session_state,
)


# Authentic-shape getLogSession response. Slots LOG1 and LOG5 enabled,
# LOG4 disabled, LOG6 unused (empty name).
ENABLED_RESPONSE = """
$R: getLogSession
LogSession, LOG1, Enabled, DSK1, "15s_24hr", After1Year, High, Continuous
LogSession, LOG2, Enabled, DSK1, "1Hz_1hr", After30Days, High, Continuous
LogSession, LOG4, Disabled, DSK1, "geod_15m", After1Year, Medium, Continuous
LogSession, LOG5, Enabled, DSK1, "status_1hr", After1Year, High, Continuous
LogSession, LOG6, Unused, DSK1, "", Never, Medium, Continuous
IP1234>
"""

DISABLED_RESPONSE = """
$R: getLogSession
LogSession, LOG1, Enabled, DSK1, "15s_24hr", After1Year, High, Continuous
LogSession, LOG5, Disabled, DSK1, "status_1hr", After1Year, High, Continuous
IP1234>
"""

UNUSED_RESPONSE = """
$R: getLogSession
LogSession, LOG1, Enabled, DSK1, "15s_24hr", After1Year, High, Continuous
LogSession, LOG5, Unused, DSK1, "status_1hr", Never, Medium, Continuous
IP1234>
"""

MISSING_RESPONSE = """
$R: getLogSession
LogSession, LOG1, Enabled, DSK1, "15s_24hr", After1Year, High, Continuous
LogSession, LOG2, Enabled, DSK1, "1Hz_1hr", After30Days, High, Continuous
LogSession, LOG6, Unused, DSK1, "", Never, Medium, Continuous
IP1234>
"""


# --- parse_log_session_state ---------------------------------------------


def test_parse_enabled():
    assert parse_log_session_state(ENABLED_RESPONSE, "status_1hr") == "enabled"


def test_parse_disabled():
    assert parse_log_session_state(DISABLED_RESPONSE, "status_1hr") == "disabled"


def test_parse_unused():
    assert parse_log_session_state(UNUSED_RESPONSE, "status_1hr") == "unused"


def test_parse_missing():
    assert parse_log_session_state(MISSING_RESPONSE, "status_1hr") == "missing"


def test_parse_case_insensitive():
    """Session name match should ignore case (operator typo tolerance)."""
    assert parse_log_session_state(ENABLED_RESPONSE, "STATUS_1HR") == "enabled"


def test_parse_single_quoted_name():
    """Some receivers echo names with single quotes; both must work."""
    resp = "LogSession, LOG5, Enabled, DSK1, 'status_1hr', After1Year, High, Continuous"
    assert parse_log_session_state(resp, "status_1hr") == "enabled"


def test_parse_ignores_command_echo():
    """The '$R: getLogSession' command-echo line must not be parsed as a slot."""
    resp = "$R: getLogSession\n"
    assert parse_log_session_state(resp, "status_1hr") == "missing"


def test_parse_prefers_enabled_over_disabled_dupes():
    """If a session name appears in two slots (defensive), Enabled wins."""
    resp = (
        'LogSession, LOG3, Disabled, DSK1, "status_1hr", After1Year, High, Continuous\n'
        'LogSession, LOG5, Enabled, DSK1, "status_1hr", After1Year, High, Continuous\n'
    )
    assert parse_log_session_state(resp, "status_1hr") == "enabled"


def test_parse_empty_response():
    assert parse_log_session_state("", "status_1hr") == "missing"


# --- _load_session_template ----------------------------------------------


def test_load_status_1hr_template():
    """status_1hr template ships with the package and must be loadable."""
    commands = _load_session_template("status_1hr")
    assert len(commands) >= 5
    # Spot-check that the SBF output + LogSession + FileNaming commands are present.
    joined = "\n".join(commands)
    assert "setSBFOutput, Stream7, LOG5" in joined
    assert "setLogSession, LOG5, Enabled" in joined
    assert "'status_1hr'" in joined
    assert "setFileNaming, LOG5" in joined


def test_load_unknown_session_template():
    with pytest.raises(FileNotFoundError):
        _load_session_template("does_not_exist")


def test_status_1hr_is_enablable():
    assert "status_1hr" in _ENABLABLE_SESSIONS


# --- Argument parser wiring ----------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    subparsers = p.add_subparsers(dest="command")
    setup_rec_config_parser(subparsers)
    return p


def test_check_session_flag_parses():
    p = _build_parser()
    ns = p.parse_args(["rec-config", "THOB", "--check-session", "status_1hr"])
    assert ns.check_session == "status_1hr"
    assert ns.extract is False
    assert ns.push is None


def test_enable_session_flag_parses():
    p = _build_parser()
    ns = p.parse_args(["rec-config", "THOB", "--enable-session", "status_1hr"])
    assert ns.enable_session == "status_1hr"


def test_modes_are_mutually_exclusive():
    """argparse mutex group should reject combining --extract with --check-session."""
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(
            ["rec-config", "THOB", "--extract", "--check-session", "status_1hr"]
        )


def test_check_and_enable_mutually_exclusive():
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(
            [
                "rec-config",
                "THOB",
                "--check-session",
                "status_1hr",
                "--enable-session",
                "status_1hr",
            ]
        )


def test_one_mode_required():
    """At least one mode must be supplied."""
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["rec-config", "THOB"])
