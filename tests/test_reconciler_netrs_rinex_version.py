"""The archive reconciler converts NetRS at RINEX 2 (codeless L2 → C2D in R3).

Regression 2026-11-19: ``_convert_raw_to_rinex`` built the converter with no
version, so every SKOG (NetRS) daily the reconciler / "Backfill RINEX" wrote was
RINEX 3 — while the live path and the backfill task already resolved the pin.
These tests drive the real ``_convert_raw_to_rinex`` call site, not only the
helper, so reverting the wiring turns them red.
"""

from pathlib import Path

import pytest

from receivers.rinex.converter_base import NamingConvention, RinexVersion
from receivers.scheduling import archive_reconciler as rec


class _Result:
    success = False
    message = "fake"
    rinex_file = None


def _capture(monkeypatch):
    seen = {}

    class FakeConverter:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def convert_file(self, raw_path, output_dir):
            return _Result()

    import receivers.config.receiver_registry as reg

    monkeypatch.setattr(reg, "get_converter_class", lambda rt: FakeConverter)
    return seen


@pytest.mark.parametrize("rt", ["netrs", "NetRS", "trimble netrs"])
def test_netrs_is_converted_at_rinex2_short(monkeypatch, tmp_path, rt):
    seen = _capture(monkeypatch)
    raw = tmp_path / "SKOG" / "15s_24hr" / "raw" / "SKOG202610210000a.T00.gz"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"x")
    rec._convert_raw_to_rinex("SKOG", raw, receiver_type=rt, session_type="15s_24hr")
    assert seen["rinex_version"] == RinexVersion.RINEX_2
    assert seen["naming_convention"] == NamingConvention.SHORT


@pytest.mark.parametrize("rt", ["netr9", "netr5"])
def test_coded_l2_trimble_stays_rinex3(monkeypatch, tmp_path, rt):
    seen = _capture(monkeypatch)
    raw = tmp_path / "X" / "raw" / "f.T02"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"x")
    rec._convert_raw_to_rinex("XXXX", raw, receiver_type=rt)
    assert seen["rinex_version"] == RinexVersion.RINEX_3
    assert "naming_convention" not in seen


def test_non_trimble_gets_converter_defaults(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    raw = tmp_path / "X" / "raw" / "f.sbf.gz"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"x")
    rec._convert_raw_to_rinex("XXXX", raw, receiver_type="polarx5")
    assert "rinex_version" not in seen and "naming_convention" not in seen
