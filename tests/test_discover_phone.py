"""Tests for ``send_sms_ssh`` (the SSH/gsmctl MSISDN-discovery send) backing
``receivers cfg discover-phone``. No network — dry-run + mocked SSH.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from receivers.cfg.telemetry_probe import ProbeError, send_sms_ssh


def test_dry_run_returns_planned_command():
    r = send_sms_ssh("10.4.2.163", "+3548000000", "hi there", dry_run=True)
    assert r["dry_run"] is True and r["sent"] is False
    assert r["transport"] == "ssh"
    # number + text in one quoted arg, wrapped in a router-side timeout guard
    assert r["cmd"] == "timeout 30 gsmctl -S -s '+3548000000 hi there'"


def test_rejects_non_phone_number():
    with pytest.raises(ProbeError, match="invalid --to"):
        send_sms_ssh("h", "not-a-number", "hi", dry_run=True)


def test_quotes_neutralise_shell_metacharacters():
    r = send_sms_ssh("h", "+354800", "a'; rm -rf / #", dry_run=True)
    # the payload is shlex-quoted — no unquoted break-out
    assert "gsmctl -S -s '" in r["cmd"]
    assert "rm -rf" in r["cmd"]  # present, but inside the quoted arg


def test_live_send_invokes_gsmctl_over_ssh():
    fake_client = MagicMock()
    with (
        patch("receivers.cfg.conntrack_helper._connect", return_value=fake_client),
        patch("receivers.cfg.conntrack_helper._run", return_value=(0, "OK", "")) as run,
        patch(
            "receivers.cfg.telemetry_probe.resolve_credentials",
            return_value=("root", "secret"),
        ),
    ):
        r = send_sms_ssh("10.4.2.163", "+354800", "hi", dry_run=False)
    assert r["sent"] is True and r["transport"] == "ssh"
    run.assert_called_once()
    # the command run is the timeout-guarded gsmctl send
    assert run.call_args.args[1].startswith("timeout ")
    assert "gsmctl -S -s '" in run.call_args.args[1]


def test_live_send_raises_without_password():
    with patch(
        "receivers.cfg.telemetry_probe.resolve_credentials",
        return_value=("root", None),
    ):
        with pytest.raises(ProbeError, match="no router password"):
            send_sms_ssh("10.4.2.163", "+354800", "hi", dry_run=False)


def test_live_send_raises_on_nonzero_gsmctl():
    fake_client = MagicMock()
    with (
        patch("receivers.cfg.conntrack_helper._connect", return_value=fake_client),
        patch("receivers.cfg.conntrack_helper._run", return_value=(1, "", "no modem")),
        patch(
            "receivers.cfg.telemetry_probe.resolve_credentials",
            return_value=("root", "secret"),
        ),
    ):
        with pytest.raises(ProbeError, match="gsmctl SMS send failed"):
            send_sms_ssh("10.4.2.163", "+354800", "hi", dry_run=False)


def test_live_send_raises_on_router_timeout_rc124():
    fake_client = MagicMock()
    with (
        patch("receivers.cfg.conntrack_helper._connect", return_value=fake_client),
        patch("receivers.cfg.conntrack_helper._run", return_value=(124, "", "")),
        patch(
            "receivers.cfg.telemetry_probe.resolve_credentials",
            return_value=("root", "secret"),
        ),
    ):
        with pytest.raises(ProbeError, match="timed out"):
            send_sms_ssh("10.4.2.163", "+354800", "hi", dry_run=False)
