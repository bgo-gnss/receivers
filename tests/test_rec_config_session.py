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
from receivers.septentrio.session_state import (
    SessionState,
    diff_session_state,
    parse_session_state,
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


def test_update_session_flag_parses():
    p = _build_parser()
    ns = p.parse_args(["rec-config", "THOB", "--update-session", "status_1hr"])
    assert ns.update_session == "status_1hr"


def test_audit_session_flag_parses():
    p = _build_parser()
    ns = p.parse_args(["rec-config", "THOB", "--audit-session", "status_1hr"])
    assert ns.audit_session == "status_1hr"


def test_audit_and_update_mutually_exclusive():
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(
            [
                "rec-config",
                "THOB",
                "--audit-session",
                "status_1hr",
                "--update-session",
                "status_1hr",
            ]
        )


# --- parse_session_state -------------------------------------------------


# Authentic shape: a slice of `lstConfigFile, Current` covering one full
# canonical status_1hr session on LOG5 fed by Stream7. Mirrors the output
# format observed on real THOB.
THOB_LIKE_CONFIG = """\
setSBFOutput, Stream7, LOG5
setSBFOutput, Stream7, , PVTGeodetic+PosCovGeodetic+ReceiverTime+SatVisibility+ChannelStatus+ReceiverStatus+ReceiverSetup+IPStatus+PosLocal+QualityInd+NTRIPClientStatus+WiFiAPStatus+DiskStatus+NTRIPServerStatus+PowerStatus+LogStatus+SystemInfo
setSBFOutput, Stream7, , , sec60
setLogSession, LOG5, Enabled
setLogSession, LOG5, , , "status_1hr"
setLogSession, LOG5, , , , After1Year
setLogSession, LOG5, , , , , High
setFileNaming, LOG5, IGS1H
setFileNaming, LOG5, , , on
""".splitlines()


def test_parse_session_state_full_match():
    """Canonical config parses into a complete SessionState."""
    state = parse_session_state(THOB_LIKE_CONFIG, "status_1hr")
    assert state is not None
    assert state.log_slot == "LOG5"
    assert state.stream_slot == "Stream7"
    assert state.state == "Enabled"
    assert state.interval == "sec60"
    assert state.retention == "After1Year"
    assert state.priority == "High"
    assert state.file_naming_format == "IGS1H"
    assert state.file_naming_enabled is True
    assert "PVTGeodetic" in state.sbf_blocks
    assert "SystemInfo" in state.sbf_blocks
    assert len(state.sbf_blocks) == 17


def test_parse_session_state_missing():
    """No LOG slot with the target name → returns None."""
    assert parse_session_state(THOB_LIKE_CONFIG, "geod_15m") is None


def test_parse_session_state_single_quoted_name():
    """Template uses single quotes; parser must accept them."""
    config = THOB_LIKE_CONFIG[:]
    config[4] = "setLogSession, LOG5, , , 'status_1hr'"
    state = parse_session_state(config, "status_1hr")
    assert state is not None
    assert state.log_slot == "LOG5"


def test_parse_session_state_finds_alternative_slot():
    """status_1hr in LOG3/Stream4 (non-canonical slot) is still discovered."""
    config = """\
setSBFOutput, Stream4, LOG3
setSBFOutput, Stream4, , PVTGeodetic+ReceiverStatus
setSBFOutput, Stream4, , , sec60
setLogSession, LOG3, Enabled
setLogSession, LOG3, , , "status_1hr"
""".splitlines()
    state = parse_session_state(config, "status_1hr")
    assert state is not None
    assert state.log_slot == "LOG3"
    assert state.stream_slot == "Stream4"


def test_parse_session_state_template_round_trip():
    """The shipped template must parse into the same SessionState as a real receiver."""
    commands = _load_session_template("status_1hr")
    state = parse_session_state(commands, "status_1hr")
    assert state is not None
    assert state.log_slot == "LOG5"
    assert state.stream_slot == "Stream7"
    assert state.state == "Enabled"
    assert state.interval == "sec60"
    assert state.retention == "After1Year"
    assert state.priority == "High"
    assert state.file_naming_format == "IGS1H"
    assert state.file_naming_enabled is True
    assert len(state.sbf_blocks) == 17


# --- diff_session_state --------------------------------------------------


def _template_state() -> SessionState:
    return parse_session_state(_load_session_template("status_1hr"), "status_1hr")


def test_diff_no_drift():
    """Identical states produce no diff."""
    state = _template_state()
    assert diff_session_state(state, state) == []


def test_diff_missing_sbf_block():
    """Receiver missing a block from the template surfaces in diff."""
    template = _template_state()
    receiver = SessionState(
        name=template.name,
        log_slot=template.log_slot,
        state=template.state,
        stream_slot=template.stream_slot,
        sbf_blocks=template.sbf_blocks - {"PowerStatus"},
        interval=template.interval,
        retention=template.retention,
        priority=template.priority,
        file_naming_format=template.file_naming_format,
        file_naming_enabled=template.file_naming_enabled,
    )
    diffs = diff_session_state(receiver, template)
    assert len(diffs) == 1
    assert "sbf_blocks" in diffs[0]
    assert "PowerStatus" in diffs[0]


def test_diff_extra_sbf_block():
    """Receiver with an unexpected block also flagged."""
    template = _template_state()
    receiver = SessionState(
        name=template.name,
        log_slot=template.log_slot,
        state=template.state,
        stream_slot=template.stream_slot,
        sbf_blocks=template.sbf_blocks | {"Commands"},
        interval=template.interval,
        retention=template.retention,
        priority=template.priority,
        file_naming_format=template.file_naming_format,
        file_naming_enabled=template.file_naming_enabled,
    )
    diffs = diff_session_state(receiver, template)
    assert len(diffs) == 1
    assert "Commands" in diffs[0]


def test_diff_interval_drift():
    template = _template_state()
    receiver = SessionState(
        name=template.name,
        log_slot=template.log_slot,
        state=template.state,
        stream_slot=template.stream_slot,
        sbf_blocks=template.sbf_blocks,
        interval="sec30",  # wrong
        retention=template.retention,
        priority=template.priority,
        file_naming_format=template.file_naming_format,
        file_naming_enabled=template.file_naming_enabled,
    )
    diffs = diff_session_state(receiver, template)
    assert any("interval" in d for d in diffs)


def test_diff_state_disabled():
    """Receiver has matching template but session is Disabled — should flag."""
    template = _template_state()
    receiver = SessionState(
        name=template.name,
        log_slot=template.log_slot,
        state="Disabled",  # wrong
        stream_slot=template.stream_slot,
        sbf_blocks=template.sbf_blocks,
        interval=template.interval,
        retention=template.retention,
        priority=template.priority,
        file_naming_format=template.file_naming_format,
        file_naming_enabled=template.file_naming_enabled,
    )
    diffs = diff_session_state(receiver, template)
    assert any("state" in d for d in diffs)


def test_diff_ignores_slot_identifier():
    """Same content on different LOG/Stream slot → no drift (slot is informational)."""
    template = _template_state()
    receiver = SessionState(
        name=template.name,
        log_slot="LOG3",  # different
        state=template.state,
        stream_slot="Stream4",  # different
        sbf_blocks=template.sbf_blocks,
        interval=template.interval,
        retention=template.retention,
        priority=template.priority,
        file_naming_format=template.file_naming_format,
        file_naming_enabled=template.file_naming_enabled,
    )
    assert diff_session_state(receiver, template) == []


# --- multi-stream binding (FAGC-style legacy cruft) ----------------------


# Mimics FAGC: Stream2 → LOG5 (legacy, no blocks, sec1) + Stream7 → LOG5
# (our push, 17 blocks, sec60). Parser should prefer Stream7.
FAGC_LIKE_CONFIG = """\
setSBFOutput, Stream2, LOG5
setSBFOutput, Stream7, LOG5
setSBFOutput, Stream2, , , sec1
setSBFOutput, Stream7, , PVTGeodetic+PosCovGeodetic+ReceiverTime+SatVisibility+ChannelStatus+ReceiverStatus+ReceiverSetup+IPStatus+PosLocal+QualityInd+NTRIPClientStatus+WiFiAPStatus+DiskStatus+NTRIPServerStatus+PowerStatus+LogStatus+SystemInfo
setSBFOutput, Stream7, , , sec60
setLogSession, LOG5, Enabled
setLogSession, LOG5, , , "status_1hr"
setLogSession, LOG5, , , , After1Year
setLogSession, LOG5, , , , , High
setFileNaming, LOG5, IGS1H
setFileNaming, LOG5, , , on
""".splitlines()


def test_parse_prefers_stream_with_more_blocks():
    """When multiple streams target the same LOG, pick the one with blocks."""
    state = parse_session_state(FAGC_LIKE_CONFIG, "status_1hr")
    assert state is not None
    assert state.stream_slot == "Stream7"
    assert len(state.sbf_blocks) == 17
    assert state.interval == "sec60"
    assert state.extra_stream_slots == ("Stream2",)


def test_diff_flags_extra_bindings():
    """Legacy multi-binding shows up as a drift entry."""
    template = _template_state()
    receiver = parse_session_state(FAGC_LIKE_CONFIG, "status_1hr")
    assert receiver is not None
    diffs = diff_session_state(receiver, template)
    assert len(diffs) == 1
    assert "extra_stream_bindings" in diffs[0]
    assert "Stream2" in diffs[0]


# Mimics OLKE post-push: Stream7 → LOG5 bound, interval sec60, but the
# `setSBFOutput, Stream7, , <blocks>` line was silently rejected so blocks
# are empty. LOG5 will produce no data.
OLKE_LIKE_CONFIG = """\
setSBFOutput, Stream7, LOG5
setSBFOutput, Stream7, , , sec60
setLogSession, LOG5, Enabled
setLogSession, LOG5, , , "status_1hr"
setLogSession, LOG5, , , , After1Year
setLogSession, LOG5, , , , , High
setFileNaming, LOG5, IGS1H
setFileNaming, LOG5, , , on
""".splitlines()


def test_diff_catches_missing_blocks_partial_push():
    """OLKE-style partial push (interval set, blocks empty) → DRIFT on blocks."""
    template = _template_state()
    receiver = parse_session_state(OLKE_LIKE_CONFIG, "status_1hr")
    assert receiver is not None
    assert receiver.stream_slot == "Stream7"
    assert len(receiver.sbf_blocks) == 0
    diffs = diff_session_state(receiver, template)
    assert any("sbf_blocks" in d for d in diffs)
    assert any("missing" in d for d in diffs if "sbf_blocks" in d)
