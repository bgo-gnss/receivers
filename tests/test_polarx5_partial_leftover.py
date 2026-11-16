"""A leftover PARTIAL in tmp must never be archived as if it were complete.

When the receiver answers 550 for a file and a local copy is sitting in the tmp
dir, ``_ftp_download`` used to queue that copy for archiving if it was merely
non-empty. The copy is usually the partial of an earlier attempt that timed out.
On 2026-11-16 that put SVIN202611151800b.sbf.gz into the archive at 2,491,128
bytes with a broken gzip stream, while SVIN's FTP was answering "not found" for
files it held. Downstream checks look only at size and magic bytes, so the
truncated hour then counted as present and was never fetched again.

These tests drive the real ``_ftp_download`` with a fake FTP server that answers
550, so they cover the call sites, not just the helper.
"""

from __future__ import annotations

import ftplib
import gzip
from pathlib import Path

import pytest

from receivers.septentrio.polarx5 import PolaRX5

STATION_CONFIG = {
    "receiver": {"type": "PolaRX5"},
    "router": {"ip": "192.0.2.1"},  # TEST-NET-1, never routable
}
FILE = "SVIN319s.26_.gz"
REMOTE_DIR = "/DSK1/SSN/LOG2_1Hz_1hr/26319/"


class Ftp550:
    """Every file is 'not found' — the flaky-receiver answer from SVIN."""

    sock = object()  # looks connected to the dead-connection guard

    def size(self, path):
        raise ftplib.error_perm("550 File not found")

    def nlst(self, *a):
        raise ftplib.error_perm("550 File not found")

    def retrbinary(self, *a, **k):
        raise ftplib.error_perm("550 File not found")

    def sendcmd(self, *a, **k):
        raise ftplib.error_perm("550 File not found")

    def voidcmd(self, *a, **k):
        raise ftplib.error_perm("550 File not found")

    def close(self):
        pass


@pytest.fixture(autouse=True)
def no_file_tracker(monkeypatch):
    """No database: make FileTracker unavailable, as on a host without one."""
    import receivers.health as health

    class _NoDb:
        def connect(self):
            return False

    monkeypatch.setattr(health, "FileTracker", _NoDb, raising=False)


def _gz_bytes(n: int = 200_000) -> bytes:
    import os

    return gzip.compress(os.urandom(n))  # incompressible: a real-sized stream


def _run(tmp_path: Path, content: bytes) -> tuple[list[str], Path]:
    rx = PolaRX5("TEST", STATION_CONFIG)
    local = tmp_path / FILE
    local.write_bytes(content)
    queued = rx._ftp_download(
        {FILE: REMOTE_DIR},
        tmp_path,
        ftp=Ftp550(),
        archive=False,
        immediate_archive=False,
        missing_file_dict=None,
        session=None,
    )
    return list(queued or []), local


@pytest.mark.unit
def test_truncated_leftover_is_not_queued_for_archiving(tmp_path):
    whole = _gz_bytes()
    queued, local = _run(tmp_path, whole[: len(whole) * 3 // 4])
    assert queued == [], "a truncated partial was queued for the archive"
    assert local.exists(), "the partial must be kept for a later resume"


@pytest.mark.unit
def test_complete_leftover_is_still_queued(tmp_path):
    queued, local = _run(tmp_path, _gz_bytes())
    assert queued == [str(local)]
