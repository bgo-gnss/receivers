"""Tests for `receivers cfg update-device`.

Companion to cmd_cfg_update_device in src/receivers/cli/cfg.py — exercises
the probe → TOS-lookup → patch flow with mocked TOSWriter and probe_receiver.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from receivers.cfg.device_probe import (
    ProbeError,
    ProbeUnreachableError,
    ReceiverIdentity,
)
from receivers.cli.arguments import create_argument_parser
from receivers.cli.cfg import cmd_cfg_update_device


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def parser():
    return create_argument_parser()


def _identity(**overrides) -> ReceiverIdentity:
    base = dict(
        subtype="gnss_receiver",
        probe_type="polarx5",
        serial="SN_4101524",
        model_raw="PolaRx5",
        firmware_version="5.7.0",
        marker_name="HRAC",
        partial=False,
    )
    base.update(overrides)
    return ReceiverIdentity(**base)


def _make_writer_mock(*, device=None) -> MagicMock:
    writer = MagicMock()
    writer.find_device_by_serial.return_value = device or {"id_entity": 12345}
    writer.upsert_attribute_value.return_value = {"ok": True}
    writer.transition_attribute_value.return_value = {"ok": True}
    return writer


# ── Happy path ─────────────────────────────────────────────────────────────


def test_dry_run_firmware_update(parser, capsys):
    args = parser.parse_args(
        ["cfg", "update-device", "--probe", "192.168.3.1", "--field", "firmware_version"]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    # Dry-run: writer still receives the call (with dry_run=True passed at construction)
    writer.find_device_by_serial.assert_called_once_with("gnss_receiver", "SN_4101524")
    writer.upsert_attribute_value.assert_called_once()
    call = writer.upsert_attribute_value.call_args
    assert call.args[0] == 12345                  # id_entity
    assert call.args[1] == "firmware_version"     # code
    assert call.args[2] == "5.7.0"                # value
    assert call.args[3] == date.today().isoformat()  # date
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "5.7.0" in out


def test_commit_firmware_update(parser, capsys):
    args = parser.parse_args(
        [
            "cfg", "update-device",
            "--probe", "192.168.3.1",
            "--field", "firmware_version",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" not in out
    assert "Pattern 1 (in-place upsert)" in out
    writer.upsert_attribute_value.assert_called_once()
    writer.transition_attribute_value.assert_not_called()


def test_transition_mode_uses_pattern_2(parser, capsys):
    args = parser.parse_args(
        [
            "cfg", "update-device",
            "--probe", "192.168.3.1",
            "--field", "firmware_version",
            "--transition",
            "--date", "2026-05-30",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    writer.transition_attribute_value.assert_called_once_with(
        12345, "firmware_version", "5.7.0", "2026-05-30"
    )
    writer.upsert_attribute_value.assert_not_called()
    assert "Pattern 2" in capsys.readouterr().out


def test_multiple_fields_in_one_call(parser):
    args = parser.parse_args(
        [
            "cfg", "update-device",
            "--probe", "192.168.3.1",
            "--field", "firmware_version",
            "--field", "model",
            "--no-dry-run",
        ]
    )
    writer = _make_writer_mock()
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 0
    assert writer.upsert_attribute_value.call_count == 2


# ── Error paths ────────────────────────────────────────────────────────────


def test_no_field_supplied(parser, capsys):
    args = parser.parse_args(
        ["cfg", "update-device", "--probe", "192.168.3.1"]
    )
    rc = cmd_cfg_update_device(args)
    assert rc == 2
    assert "--field is required" in capsys.readouterr().err


def test_unsupported_field(parser, capsys):
    args = parser.parse_args(
        ["cfg", "update-device", "--probe", "192.168.3.1", "--field", "voltage"]
    )
    with patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()):
        rc = cmd_cfg_update_device(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "voltage" in err and "not supported" in err


def test_probe_unreachable(parser, capsys):
    args = parser.parse_args(
        ["cfg", "update-device", "--probe", "192.168.99.99", "--field", "firmware_version"]
    )
    with patch(
        "receivers.cfg.device_probe.probe_receiver",
        side_effect=ProbeUnreachableError("no route to 192.168.99.99"),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 1
    assert "no route" in capsys.readouterr().err


def test_device_not_found_in_tos(parser, capsys):
    args = parser.parse_args(
        ["cfg", "update-device", "--probe", "192.168.3.1", "--field", "firmware_version"]
    )
    writer = _make_writer_mock(device={})  # empty dict — no id_entity
    writer.find_device_by_serial.return_value = None  # actually missing
    with (
        patch("receivers.cfg.device_probe.probe_receiver", return_value=_identity()),
        patch("tostools.api.tos_writer.TOSWriter", return_value=writer),
    ):
        rc = cmd_cfg_update_device(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "no TOS device" in err
    assert "SN_4101524" in err
    assert "cfg add-receiver" in err  # helpful hint


def test_probe_returns_no_firmware(parser, capsys):
    """Probe succeeded but receiver reported no firmware_version — clear error."""
    args = parser.parse_args(
        ["cfg", "update-device", "--probe", "192.168.3.1", "--field", "firmware_version"]
    )
    no_fw = _identity(firmware_version=None)
    with patch("receivers.cfg.device_probe.probe_receiver", return_value=no_fw):
        rc = cmd_cfg_update_device(args)
    assert rc == 1
    assert "firmware_version" in capsys.readouterr().err
