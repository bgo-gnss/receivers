"""A day the receiver never finalised must not be treated as absent.

Septentrio writes ``{SID}{DOY}{s}.{yy}_.A`` while logging and finalises it to
``…_.gz``. A day the receiver was interrupted never finalises and is left bare:
no ``.gz``, no ``.A``. ``_build_remote_template`` bakes in ``compression``
(default ``.gz``), so the downloader asked only for the ``.gz``, got a 550 every
time, and after 3 days / 3 confirmations the slot became a TERMINAL
``file_absence`` — for data sitting on the receiver.

Measured on RIFC 2026-09-14, four bare files present while their slots were
marked terminal (``RIFC2460.26_``, ``RIFC2470.26_``, ``RIFC2480.26_``,
``THEY2500.26_``), each an exact multiple of 4096.

What these tests pin, in order of how badly each would hurt:

* **``.A`` is never fetched.** It is the log being written right now; racing the
  writer would yield a torn file. Only the bare name is tried.
* **The bytes are gzipped to the ``.gz`` name before anything downstream sees
  them.** Uncompressed bytes in a ``.gz``-named file fail gzip validation later
  and poison ``file_tracking`` identity.
* **A short read is not accepted**, so a truncated transfer cannot masquerade
  as a recovered day.
* **A genuinely absent day still reports absent** — the fallback must not turn
  every 550 into a success.
"""

import gzip
from pathlib import Path

import pytest

from receivers.septentrio.polarx5 import PolaRX5


class _FakeFTP:
    """Minimal FTP double: knows which remote paths exist, and their bytes."""

    def __init__(self, files):
        self.files = files  # path -> bytes
        self.size_calls = []
        self.retr_calls = []

    def size(self, path):
        self.size_calls.append(path)
        if path not in self.files:
            raise OSError("550 Can't check for file existence")
        return len(self.files[path])

    def retrbinary(self, cmd, callback, rest=None):
        path = cmd.split(" ", 1)[1]
        self.retr_calls.append(path)
        if path not in self.files:
            raise OSError("550 No such file")
        callback(self.files[path])


@pytest.fixture
def rx(monkeypatch):
    """A PolaRX5 with everything but the code under test stubbed out."""
    obj = object.__new__(PolaRX5)
    obj.station_id = "RIFC"
    obj.logger = __import__("logging").getLogger("test.polarx5")
    obj._last_file_error = None
    obj.handled = []

    def _handled(file_name, local_file, local_size, remote_size, *a, **k):
        obj.handled.append((file_name, Path(local_file), local_size, remote_size))
        return True

    obj._handle_successful_download = _handled
    return obj


def _call(rx, ftp, remote_file, local_file):
    return rx._fetch_unfinalised_variant(
        ftp,
        remote_file,
        Path(remote_file).name,
        local_file,
        "15s_24hr",
        [],
        None,
        False,
        False,
        False,
        None,
        lambda n: None,
    )


REMOTE = "/DSK1/SSN/LOG1_15s_24hr/26248/RIFC2480.26_.gz"
BARE = "/DSK1/SSN/LOG1_15s_24hr/26248/RIFC2480.26_"
PAYLOAD = b"\x24\x40SBF-ish payload" * 512


def test_unfinalised_day_is_fetched_and_gzipped(rx, tmp_path):
    """The RIFC case: .gz absent, bare present, 131072-style block-aligned."""
    ftp = _FakeFTP({BARE: PAYLOAD})
    local = tmp_path / "RIFC2480.26_.gz"

    assert _call(rx, ftp, REMOTE, local) is True
    assert ftp.retr_calls == [BARE]
    assert local.exists(), "must land under the .gz name downstream expects"
    with gzip.open(local, "rb") as fh:
        assert fh.read() == PAYLOAD, "bytes must survive the round trip"
    assert rx.handled and rx.handled[0][0] == "RIFC2480.26_.gz"


def test_staging_file_is_cleaned_up(rx, tmp_path):
    ftp = _FakeFTP({BARE: PAYLOAD})
    local = tmp_path / "RIFC2480.26_.gz"
    _call(rx, ftp, REMOTE, local)
    assert list(tmp_path.iterdir()) == [local], "no .unfinalised leftover"


def test_the_active_log_is_never_fetched(rx, tmp_path):
    """``.A`` is the log being written RIGHT NOW — racing it yields a torn file.

    Only the bare name is probed, so an ``.A``-only day resolves to nothing.
    If anyone generalises this to "try any suffix", this test fails.
    """
    active = "/DSK1/SSN/LOG1_15s_24hr/26257/RIFC2570.26_.A"
    ftp = _FakeFTP({active: PAYLOAD})
    local = tmp_path / "RIFC2570.26_.gz"

    handled = _call(rx, ftp, "/DSK1/SSN/LOG1_15s_24hr/26257/RIFC2570.26_.gz", local)
    assert handled is False
    assert active not in ftp.retr_calls
    assert not local.exists()


def test_genuinely_absent_day_still_reports_absent(rx, tmp_path):
    """Neither form present — the fallback must not swallow a real absence."""
    ftp = _FakeFTP({})
    local = tmp_path / "RIFC2480.26_.gz"
    assert _call(rx, ftp, REMOTE, local) is False
    assert ftp.retr_calls == []
    assert not local.exists()


def test_short_read_is_rejected_and_leaves_nothing_behind(rx, tmp_path):
    """A truncated transfer must not masquerade as a recovered day."""

    class _Truncating(_FakeFTP):
        def retrbinary(self, cmd, callback, rest=None):
            self.retr_calls.append(cmd.split(" ", 1)[1])
            callback(PAYLOAD[: len(PAYLOAD) // 3])  # short

    ftp = _Truncating({BARE: PAYLOAD})
    local = tmp_path / "RIFC2480.26_.gz"

    assert _call(rx, ftp, REMOTE, local) is False
    assert rx.handled == [], "a short read must never reach the archive path"
    assert list(tmp_path.iterdir()) == [], "no partial file left behind"


def test_only_gz_requests_get_the_fallback(rx, tmp_path):
    """An already-uncompressed request has no .gz to strip — do not recurse."""
    ftp = _FakeFTP({BARE: PAYLOAD})
    assert _call(rx, ftp, BARE, tmp_path / "RIFC2480.26_") is False
    assert ftp.size_calls == [], "must not even probe"


def test_zero_byte_remote_is_not_accepted(rx, tmp_path):
    """A 0-byte stub on the receiver is not a recovered day."""
    ftp = _FakeFTP({BARE: b""})
    local = tmp_path / "RIFC2480.26_.gz"
    assert _call(rx, ftp, REMOTE, local) is False
    assert not local.exists()
