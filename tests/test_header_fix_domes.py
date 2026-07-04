"""fix-headers rewrites MARKER NUMBER (DOMES) in the same single read/fix pass.

A domes-only discrepancy must no longer be dropped as "formatting noise": it is
now a real, fixable field. These tests drive ``fix_headers_in_file`` fully
offline — the header read, TOS session, validator, corrector and regenerability
gate are all monkeypatched — and assert the DOMES label flows through to the
corrector's ``only_fields`` in one pass (no separate DOMES sweep).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import tostools.rinex as tr
import tostools.rinex.validator as tv

from receivers.rinex import header_fix as hf
from receivers.rinex import raw_presence as rp


def _drive(monkeypatch, tmp_path, *, comparison):
    """Run fix_headers_in_file on one real (empty) file with everything mocked.

    Returns (result, captured) where captured["only_fields"] is the set handed
    to the corrector (None if the corrector was never called).
    """
    f = tmp_path / "RHOF0910.10D.Z"
    f.write_bytes(b"stub")  # must exist; content unused (read is mocked)

    monkeypatch.setattr(
        hf, "_read_header_info", lambda *a, **k: {"MARKER NAME": "RHOF"}
    )
    monkeypatch.setattr(tv, "compare_rinex_to_tos", lambda *a, **k: comparison)
    # Regenerable ⇒ no rinex_org preservation branch.
    monkeypatch.setattr(
        rp,
        "check_regenerable",
        lambda *a, **k: SimpleNamespace(regenerable=True, reason=""),
    )

    captured: dict = {"only_fields": None}

    def fake_correct(
        target, station, *, observation_date, output_file, loglevel, only_fields
    ):
        captured["only_fields"] = set(only_fields)
        return output_file  # non-None ⇒ fixed

    monkeypatch.setattr(tr, "correct_rinex_from_tos", fake_correct)

    tos_cache = SimpleNamespace(
        get_session=lambda sid, dt: {"marker": "RHOF", "domes": "10216M001"}
    )
    result = hf.fix_headers_in_file(
        f,
        "RHOF",
        observation_date=datetime(2010, 4, 1),
        tos_cache=tos_cache,
        session_type="15s_24hr",
    )
    return result, captured


def _domes_comparison():
    return {
        "discrepancies": {"domes": {"rinex": "RHOF", "tos": "10216M001"}},
        "corrections": {"MARKER NUMBER": "10216M001"},
    }


def test_domes_only_discrepancy_is_fixed(monkeypatch, tmp_path):
    result, captured = _drive(monkeypatch, tmp_path, comparison=_domes_comparison())
    assert result["fixed"] is True
    assert result["changed_labels"] == ["MARKER NUMBER"]
    assert captured["only_fields"] == {"MARKER NUMBER"}
    # the old→new transition is recorded for the run summary
    assert result["changes"]["MARKER NUMBER"] == ("RHOF", "10216M001")


def test_domes_and_height_fixed_in_one_pass(monkeypatch, tmp_path):
    comparison = {
        "discrepancies": {
            "domes": {"rinex": "", "tos": "10216M001"},
            "antenna_height": {"rinex": 1.0070, "tos": 1.0140},
        },
        "corrections": {
            "MARKER NUMBER": "10216M001",
            "ANTENNA: DELTA H/E/N": "1.0140 0.0000 0.0000",
        },
    }
    result, captured = _drive(monkeypatch, tmp_path, comparison=comparison)
    assert result["fixed"] is True
    # one read → both fields fixed in a single corrector call
    assert captured["only_fields"] == {"MARKER NUMBER", "ANTENNA: DELTA H/E/N"}


def test_receiver_only_discrepancy_still_skipped(monkeypatch, tmp_path):
    # receiver/antenna remain excluded (validator flags them unconditionally).
    comparison = {
        "discrepancies": {"receiver": {"rinex": "x", "tos": "y"}},
        "corrections": {"REC # / TYPE / VERS": "y"},
    }
    result, captured = _drive(monkeypatch, tmp_path, comparison=comparison)
    assert result["fixed"] is False
    assert captured["only_fields"] is None  # corrector never called
