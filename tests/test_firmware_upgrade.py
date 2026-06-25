"""Unit tests for the pure-logic pieces of septentrio.firmware_upgrade.

The flash itself needs a real receiver; these cover the bits that don't:
version parsing, content hashing, and the upgrade-mode readiness handshake.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from receivers.septentrio import firmware_upgrade as fw


def test_sha256_of(tmp_path):
    p = tmp_path / "x.suf"
    p.write_bytes(b"hello")
    import hashlib

    assert fw.sha256_of(p) == hashlib.sha256(b"hello").hexdigest()


def test_read_firmware_version_labeled():
    sock = MagicMock()
    sock.sendall = MagicMock()
    sock.recv = MagicMock(side_effect=[b"... Firmware: 5.7.0 ...\nIP10>", b""])
    assert fw.read_firmware_version(sock) == "5.7.0"


def test_read_firmware_version_bare_triplet():
    sock = MagicMock()
    sock.recv = MagicMock(side_effect=[b"blah 5.6.0 blah IP10>", b""])
    assert fw.read_firmware_version(sock) == "5.6.0"


def test_read_firmware_version_none():
    sock = MagicMock()
    sock.recv = MagicMock(side_effect=[b"no version here IP10>", b""])
    assert fw.read_firmware_version(sock) is None


def test_stream_suf_aborts_without_ready_signal(tmp_path):
    """If the receiver never says 'Ready for SUF download', stream_suf must raise
    BEFORE sending any firmware bytes."""
    suf = tmp_path / "PolaRx5-5.7.0.suf"
    suf.write_bytes(b"\x00" * 4096)
    sent = []
    sock = MagicMock()
    sock.sendall = MagicMock(side_effect=lambda b: sent.append(b))
    sock.recv = MagicMock(return_value=b"garbage prompt IP10>")  # never "Ready"

    with pytest.raises(fw.FirmwareUpgradeError):
        fw.stream_suf(sock, suf, ready_timeout_s=0.3)

    # Only the exeResetReceiver command was sent — no firmware payload.
    assert sent == [b"exeResetReceiver, Upgrade, none\n"]
