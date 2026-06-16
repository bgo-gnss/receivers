"""Tests for the SSH MSISDN-discovery helpers backing ``cfg discover-phone``:
``send_sms_ssh`` (SMS catcher), ``query_ussd_ssh`` (USSD fallback), and the
``_ssh_run_bounded`` client-side timeout. No network — dry-run + mocked SSH.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from receivers.cfg.telemetry_probe import (
    ProbeError,
    _ssh_run_bounded,
    query_ussd_ssh,
    send_sms_ssh,
)

# --- send_sms_ssh ---------------------------------------------------------


def test_dry_run_returns_planned_command():
    r = send_sms_ssh("10.4.2.163", "+3548000000", "hi there", dry_run=True)
    assert r["dry_run"] is True and r["sent"] is False
    assert r["transport"] == "ssh"
    # number + text in one quoted arg (no router-side `timeout` — busybox lacks it)
    assert r["cmd"] == "gsmctl -S -s '+3548000000 hi there'"


def test_rejects_non_phone_number():
    with pytest.raises(ProbeError, match="invalid --to"):
        send_sms_ssh("h", "not-a-number", "hi", dry_run=True)


def test_quotes_neutralise_shell_metacharacters():
    r = send_sms_ssh("h", "+354800", "a'; rm -rf / #", dry_run=True)
    assert "gsmctl -S -s '" in r["cmd"]
    assert "rm -rf" in r["cmd"]  # present, but inside the quoted arg


def test_live_send_invokes_gsmctl_over_ssh():
    fake_client = MagicMock()
    with (
        patch("receivers.cfg.conntrack_helper._connect", return_value=fake_client),
        patch(
            "receivers.cfg.telemetry_probe._ssh_run_bounded", return_value=(0, "OK", "")
        ) as run,
        patch(
            "receivers.cfg.telemetry_probe.resolve_credentials",
            return_value=("root", "secret"),
        ),
    ):
        r = send_sms_ssh("10.4.2.163", "+354800", "hi", dry_run=False)
    assert r["sent"] is True and r["transport"] == "ssh"
    run.assert_called_once()
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
        patch(
            "receivers.cfg.telemetry_probe._ssh_run_bounded",
            return_value=(1, "", "no modem"),
        ),
        patch(
            "receivers.cfg.telemetry_probe.resolve_credentials",
            return_value=("root", "secret"),
        ),
    ):
        with pytest.raises(ProbeError, match="gsmctl SMS send failed"):
            send_sms_ssh("10.4.2.163", "+354800", "hi", dry_run=False)


# --- _ssh_run_bounded (client-side timeout) -------------------------------


def test_ssh_run_bounded_times_out_client_side():
    """No router `timeout` needed — a never-ready channel is abandoned client-side."""
    chan = MagicMock()
    chan.exit_status_ready.return_value = False  # never finishes
    stdout = MagicMock()
    stdout.channel = chan
    fake_client = MagicMock()
    fake_client.exec_command.return_value = (MagicMock(), stdout, MagicMock())
    with pytest.raises(ProbeError, match="did not return within"):
        _ssh_run_bounded(fake_client, "sleep 999", timeout=1)
    chan.close.assert_called()


def test_ssh_run_bounded_returns_on_ready():
    chan = MagicMock()
    chan.exit_status_ready.return_value = True
    chan.recv_exit_status.return_value = 0
    stdout = MagicMock()
    stdout.channel = chan
    stdout.read.return_value = b"OK\n"
    stderr = MagicMock()
    stderr.read.return_value = b""
    fake_client = MagicMock()
    fake_client.exec_command.return_value = (MagicMock(), stdout, stderr)
    rc, out, err = _ssh_run_bounded(fake_client, "echo OK", timeout=5)
    assert (rc, out, err) == (0, "OK", "")


# --- query_ussd_ssh (USSD fallback) ---------------------------------------


def test_ussd_dry_run_plans_send_and_read():
    r = query_ussd_ssh("10.4.2.163", "*101#", dry_run=True)
    assert r["dry_run"] is True
    assert "gsmctl -U " in r["cmd"] and "cat /tmp/ussd_" in r["cmd"]


def test_ussd_rejects_bad_code():
    with pytest.raises(ProbeError, match="invalid --ussd"):
        query_ussd_ssh("h", "DROP TABLE", dry_run=True)


def test_ussd_live_returns_network_reply():
    fake_client = MagicMock()
    with (
        patch("receivers.cfg.conntrack_helper._connect", return_value=fake_client),
        patch(
            "receivers.cfg.telemetry_probe._ssh_run_bounded",
            return_value=(0, "Your number is +3548474985", ""),
        ),
        patch(
            "receivers.cfg.telemetry_probe.resolve_credentials",
            return_value=("root", "secret"),
        ),
    ):
        r = query_ussd_ssh("10.4.2.163", "*101#", dry_run=False)
    assert r["response"] == "Your number is +3548474985"
