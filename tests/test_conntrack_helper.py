"""Tests for receivers.cfg.conntrack_helper — RutOS FTP conntrack-helper via SSH.

paramiko/SSH is fully mocked (no network). A FakeSSH maps command substrings to
canned ``(rc, stdout, stderr)`` so the apply/idempotency/dry-run logic is tested
against the real RUT241 state shapes captured live 2026-06-07.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from receivers.cfg.conntrack_helper import (
    ProbeError,
    check_conntrack_helper,
    ensure_conntrack_helper,
)


class FakeSSH:
    """Minimal paramiko.SSHClient stand-in driven by a (substr → (rc,out,err)) map."""

    def __init__(self, responses):
        self._responses = responses  # list of (substr, (rc, out, err))
        self.commands = []

    def exec_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        rc, out, err = 0, "", ""
        for substr, resp in self._responses:
            if substr in cmd:
                rc, out, err = resp
                break
        stdout = MagicMock()
        stdout.channel.recv_exit_status.return_value = rc
        stdout.read.return_value = out.encode()
        stderr = MagicMock()
        stderr.read.return_value = err.encode()
        return MagicMock(), stdout, stderr

    def close(self):
        pass


def _patch(fake, *, pw="secret"):
    """Patch _connect → fake client and resolve_credentials → (admin, pw)."""
    return (
        patch("receivers.cfg.conntrack_helper._connect", return_value=fake),
        patch(
            "receivers.cfg.conntrack_helper.resolve_credentials",
            lambda **_k: ("admin", pw),
        ),
    )


# State of a fresh router: helper disabled, not persisted, ftp tracks 21.
_FRESH = [
    ("sysctl -n net.netfilter.nf_conntrack_helper", (0, "0", "")),
    ("grep -E", (1, "", "")),  # not persisted
    ("parameters/ports", (0, "21", "")),
]
# State of an already-fixed router.
_FIXED = [
    ("sysctl -n net.netfilter.nf_conntrack_helper", (0, "1", "")),
    ("grep -E", (0, "net.netfilter.nf_conntrack_helper=1", "")),
    ("parameters/ports", (0, "21", "")),
]


def test_check_reads_state():
    fake = FakeSSH(_FRESH)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        st = check_conntrack_helper("10.6.1.228")
    assert st.helper_value == "0"
    assert st.helper_persisted is False
    assert st.ftp_ports == "21"


def test_dry_run_plans_but_does_not_write():
    fake = FakeSSH(_FRESH)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", dry_run=True)
    assert res["dry_run"] is True
    assert res["changed"] is True
    assert res["applied"] is False
    # plan: live sysctl + persist; no ftp-ports op (not requested)
    assert any(
        "sysctl -w net.netfilter.nf_conntrack_helper=1" in c for c in res["planned"]
    )
    assert any("/etc/sysctl.conf" in c for c in res["planned"])
    assert not any("modprobe" in c for c in res["planned"])
    # dry-run must not have executed any apply command (only the 3 read probes)
    assert not any("sysctl -w" in c for c in fake.commands)


def test_live_apply_enables_and_verifies():
    # reads return fresh first, then fixed on the post-apply re-read
    class SeqSSH(FakeSSH):
        def exec_command(self, cmd, timeout=None):
            self.commands.append(cmd)
            if "sysctl -n" in cmd:
                # before → "0", after (once an apply cmd has run) → "1"
                applied = any("sysctl -w" in c for c in self.commands)
                val = "1" if applied else "0"
                return self._mk(0, val)
            if "grep -E" in cmd:
                applied = any(
                    "sysctl.conf" in c and "echo" in c or "sed" in c
                    for c in self.commands
                )
                return self._mk(
                    0 if applied else 1,
                    "net.netfilter.nf_conntrack_helper=1" if applied else "",
                )
            if "parameters/ports" in cmd:
                return self._mk(0, "21")
            return self._mk(0, "")

        def _mk(self, rc, out):
            so = MagicMock()
            so.channel.recv_exit_status.return_value = rc
            so.read.return_value = out.encode()
            se = MagicMock()
            se.read.return_value = b""
            return MagicMock(), so, se

    fake = SeqSSH([])
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", dry_run=False)
    assert res["applied"] is True
    assert res["after"]["helper"] == "1"
    assert any(
        "sysctl -w net.netfilter.nf_conntrack_helper=1" in c for c in fake.commands
    )


def test_idempotent_when_already_enabled():
    fake = FakeSSH(_FIXED)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", dry_run=False)
    assert res["changed"] is False
    assert res["applied"] is False
    assert res["planned"] == []
    assert not any("sysctl -w" in c for c in fake.commands)


def test_ftp_ports_extension_adds_modprobe_and_reload():
    fake = FakeSSH(_FRESH)  # ftp_ports currently "21", we want "21,2160"
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", ftp_ports="21,2160", dry_run=True)
    assert any("modprobe.d/nf_conntrack_ftp.conf" in c for c in res["planned"])
    assert any("modprobe nf_conntrack_ftp ports=21,2160" in c for c in res["planned"])


def test_ftp_ports_noop_when_already_matching():
    fixed_ports = [
        ("sysctl -n net.netfilter.nf_conntrack_helper", (0, "1", "")),
        ("grep -E", (0, "net.netfilter.nf_conntrack_helper=1", "")),
        ("parameters/ports", (0, "21,2160", "")),
    ]
    fake = FakeSSH(fixed_ports)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", ftp_ports="21,2160", dry_run=True)
    assert res["changed"] is False  # helper already on AND ports already match


def test_invalid_ftp_ports_rejected():
    """--ftp-ports with shell metacharacters must be rejected (no injection)."""
    fake = FakeSSH(_FRESH)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        with pytest.raises(ProbeError):
            ensure_conntrack_helper("10.6.1.228", ftp_ports="21; reboot", dry_run=True)


def test_no_password_raises():
    cm_conn, cm_creds = _patch(FakeSSH(_FRESH), pw="")
    with cm_conn, cm_creds:
        with pytest.raises(ProbeError):
            ensure_conntrack_helper("10.6.1.228", dry_run=True)


def test_module_reload_busy_is_not_fatal():
    """rmmod failing (module busy) must be a note, not a hard error."""
    seq_responses = [
        ("sysctl -n", (0, "1", "")),  # helper already on so only ports change
        ("grep -E", (0, "net.netfilter.nf_conntrack_helper=1", "")),
        ("parameters/ports", (0, "21", "")),
        ("rmmod nf_conntrack_ftp", (1, "", "rmmod: module is in use")),
        ("modprobe.d", (0, "", "")),
    ]
    fake = FakeSSH(seq_responses)
    cm_conn, cm_creds = _patch(fake)
    with cm_conn, cm_creds:
        res = ensure_conntrack_helper("10.6.1.228", ftp_ports="21,2160", dry_run=False)
    assert res["applied"] is True
    assert any("busy" in n for n in res.get("notes", []))
