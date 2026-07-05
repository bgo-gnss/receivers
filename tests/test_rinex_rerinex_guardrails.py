"""Guardrails for idempotent, network-safe re-rinexing.

Covers the three mechanisms that let an interrupted re-rinex run be finished by
re-running the SAME command:

1. ``_is_network_error`` / ``NetworkUnavailableError`` — a TOS/network drop is
   distinguished from a data no-op and surfaced as a typed, retryable error.
2. ``convert_file`` propagates ``NetworkUnavailableError`` (does not mask it as a
   per-file failure) so the run aborts before the push.
3. ``_staged_rinex_for_date`` — a complete staged output is detected so a resume
   skips it.
"""

import socket
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from receivers.cli.main import _staged_rinex_for_date
from receivers.rinex.converter_base import (
    ConversionError,
    NetworkUnavailableError,
    _is_network_error,
)
from receivers.rinex import SBFConverter


# --------------------------------------------------------------------------- #
# 1. Network-error detection
# --------------------------------------------------------------------------- #
class _NameResolutionError(Exception):
    pass


_NameResolutionError.__name__ = "NameResolutionError"


class _ConnErr(Exception):
    pass


_ConnErr.__name__ = "ConnectionError"


@pytest.mark.parametrize(
    "exc,expected",
    [
        (socket.gaierror("Name or service not known"), True),  # DNS, OSError
        (_NameResolutionError("boom"), True),  # urllib3 class by name
        (_ConnErr("refused"), True),  # requests.ConnectionError by name
        (ConnectionRefusedError("nope"), True),  # OSError subclass
        (ValueError("bad data"), False),  # genuine data error
        (KeyError("station"), False),
    ],
)
def test_is_network_error(exc, expected):
    assert _is_network_error(exc) is expected


def test_is_network_error_follows_cause_chain():
    wrapped = RuntimeError("wrap")
    wrapped.__cause__ = socket.gaierror("dns down")
    assert _is_network_error(wrapped) is True


# --------------------------------------------------------------------------- #
# 2. convert_file propagates NetworkUnavailableError, masks ordinary failures
# --------------------------------------------------------------------------- #
def _make_converter(tmp_path: Path) -> SBFConverter:
    conv = SBFConverter("ELDC")
    return conv


def test_convert_file_propagates_network_error(tmp_path):
    """A NetworkUnavailableError from header correction must escape convert_file
    (so the caller can abort the whole run), NOT be swallowed into a failed
    result the way ordinary exceptions are."""
    conv = _make_converter(tmp_path)
    raw = tmp_path / "ELDC202401010000a.sbf.gz"
    raw.write_bytes(b"\x1f\x8b\x00")  # any bytes; _run_conversion is stubbed
    produced = tmp_path / "ELDC0010.24o"
    produced.write_text("dummy rinex\n")

    with patch.object(conv, "_run_conversion", return_value=produced), patch.object(
        conv,
        "_apply_header_corrections",
        side_effect=NetworkUnavailableError("TOS down"),
    ):
        with pytest.raises(NetworkUnavailableError):
            conv.convert_file(raw, output_dir=tmp_path)


def test_convert_file_masks_ordinary_error(tmp_path):
    """A non-network failure stays a failed result (success=False), not a raise —
    the run continues to the next file."""
    conv = _make_converter(tmp_path)
    raw = tmp_path / "ELDC202401010000a.sbf.gz"
    raw.write_bytes(b"\x1f\x8b\x00")

    with patch.object(
        conv, "_run_conversion", side_effect=ConversionError("bad file", raw)
    ):
        result = conv.convert_file(raw, output_dir=tmp_path)
    assert result.success is False


def test_apply_header_corrections_maps_systemexit(tmp_path):
    """tostools.search_station does sys.exit(1) on a ConnectionError; that
    SystemExit must be converted to NetworkUnavailableError, not kill the run."""
    conv = _make_converter(tmp_path)
    rinex = tmp_path / "ELDC0010.24o"
    rinex.write_text("dummy\n")

    def _boom(*a, **k):
        raise SystemExit(1)

    with patch("tostools.rinex.correct_rinex_from_tos", _boom):
        with pytest.raises(NetworkUnavailableError):
            conv._apply_header_corrections(rinex, datetime(2024, 1, 1))


def test_apply_header_corrections_data_noop_returns_zero(tmp_path):
    """A genuine None result (no corrections / data no-op) is NOT a network
    error — returns 0 so the caller can decide (drop it in re-rinex mode)."""
    conv = _make_converter(tmp_path)
    rinex = tmp_path / "ELDC0010.24o"
    rinex.write_text("dummy\n")

    with patch("tostools.rinex.correct_rinex_from_tos", return_value=None):
        assert conv._apply_header_corrections(rinex, datetime(2024, 1, 1)) == 0


# --------------------------------------------------------------------------- #
# 3. Staged-output detection (idempotent resume)
# --------------------------------------------------------------------------- #
def _write_compress_z(path: Path, size: int = 500) -> None:
    path.write_bytes(b"\x1f\x9d" + b"\x00" * (size - 2))  # .Z magic + padding


def test_staged_detected_for_matching_date(tmp_path):
    out = tmp_path / "rinex"
    out.mkdir()
    _write_compress_z(out / "RHOF0010.14D.Z")
    hit = _staged_rinex_for_date(out, datetime(2014, 1, 1), "RHOF", "15s_24hr")
    assert hit is not None and hit.name == "RHOF0010.14D.Z"


def test_staged_none_for_other_date(tmp_path):
    out = tmp_path / "rinex"
    out.mkdir()
    _write_compress_z(out / "RHOF0010.14D.Z")
    assert _staged_rinex_for_date(out, datetime(2014, 2, 2), "RHOF", "15s_24hr") is None


def test_staged_rejects_truncated_remnant(tmp_path):
    out = tmp_path / "rinex"
    out.mkdir()
    (out / "RHOF0010.14D.Z").write_bytes(b"\x1f\x9d")  # magic ok but < 200 bytes
    assert _staged_rinex_for_date(out, datetime(2014, 1, 1), "RHOF", "15s_24hr") is None


def test_staged_rejects_wrong_magic(tmp_path):
    out = tmp_path / "rinex"
    out.mkdir()
    (out / "RHOF0010.14D.Z").write_bytes(b"XX" + b"\x00" * 500)  # not .Z/.gz
    assert _staged_rinex_for_date(out, datetime(2014, 1, 1), "RHOF", "15s_24hr") is None


def test_staged_missing_dir(tmp_path):
    assert (
        _staged_rinex_for_date(
            tmp_path / "nope", datetime(2014, 1, 1), "RHOF", "15s_24hr"
        )
        is None
    )
