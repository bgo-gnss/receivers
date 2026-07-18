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


def _drive(monkeypatch, tmp_path, *, comparison, session=None, correct_hardware=frozenset()):
    """Run fix_headers_in_file on one real (empty) file with everything mocked.

    Returns (result, captured) where captured["only_fields"] is the set handed
    to the corrector (None if the corrector was never called).
    """
    if session is None:
        session = {"marker": "RHOF", "domes": "10216M001"}
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
        target,
        station,
        *,
        observation_date,
        output_file,
        loglevel,
        only_fields,
        extra_corrections=None,
    ):
        captured["only_fields"] = set(only_fields)
        captured["extra_corrections"] = extra_corrections
        return output_file  # non-None ⇒ fixed

    monkeypatch.setattr(tr, "correct_rinex_from_tos", fake_correct)

    tos_cache = SimpleNamespace(get_session=lambda sid, dt: session)
    result = hf.fix_headers_in_file(
        f,
        "RHOF",
        observation_date=datetime(2010, 4, 1),
        tos_cache=tos_cache,
        session_type="15s_24hr",
        correct_hardware=correct_hardware,
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


def test_no_domes_strip_routes_marker_number_to_corrector(monkeypatch, tmp_path):
    # New policy path: a no-DOMES station whose archived header still carries a
    # legacy 4-char MARKER NUMBER. The validator flags it with an empty "tos"
    # (display-only strip flag); header_fix must still route MARKER NUMBER into
    # the corrector's only_fields, where the corrector recomputes the real strip.
    comparison = {
        "discrepancies": {"domes": {"rinex": "RHOF", "tos": ""}},
        "corrections": {"MARKER NUMBER": ""},
    }
    result, captured = _drive(
        monkeypatch,
        tmp_path,
        comparison=comparison,
        session={"marker": "RHOF", "domes": ""},  # no DOMES
    )
    assert result["fixed"] is True
    assert captured["only_fields"] == {"MARKER NUMBER"}
    # the strip is recorded as a legacy-id → (empty) transition for the summary
    assert result["changes"]["MARKER NUMBER"] == ("RHOF", "")


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


def test_receiver_only_discrepancy_is_flagged_not_written(monkeypatch, tmp_path):
    # receiver/antenna are FLAG-only: reported for review, never auto-written.
    comparison = {
        "discrepancies": {"receiver": {"rinex": "x sn=1", "tos": "y sn=2"}},
        "corrections": {"REC # / TYPE / VERS": ["2", "y", ""]},
    }
    result, captured = _drive(monkeypatch, tmp_path, comparison=comparison)
    assert result["fixed"] is False
    assert captured["only_fields"] is None  # corrector never called
    # but the mismatch IS recorded so the run summary can surface it
    assert result["flagged"]["receiver"] == ("x sn=1", "y sn=2")


def test_correct_receiver_optin_writes_rec_type(monkeypatch, tmp_path):
    # With correct_hardware={"receiver"} (the --correct-receiver opt-in) the
    # normally flag-only receiver is promoted to correctable and routed to the
    # corrector's only_fields as REC # / TYPE / VERS.
    comparison = {
        "discrepancies": {"receiver": {"rinex": "TRIMBLE NETRS", "tos": "SEPT POLARX5"}},
        "corrections": {"REC # / TYPE / VERS": ["3001", "SEPT POLARX5", "5.5.0"]},
    }
    result, captured = _drive(
        monkeypatch, tmp_path, comparison=comparison,
        correct_hardware=frozenset({"receiver"}),
    )
    assert result["fixed"] is True
    assert captured["only_fields"] == {"REC # / TYPE / VERS"}
    assert result["changes"]["REC # / TYPE / VERS"] == ("TRIMBLE NETRS", "SEPT POLARX5")
    # not left in the flag-only bucket
    assert "receiver" not in result["flagged"]


def test_correct_receiver_optin_leaves_antenna_flag_only(monkeypatch, tmp_path):
    # Opting in ONLY receiver must not promote antenna — it stays flag-only.
    comparison = {
        "discrepancies": {
            "receiver": {"rinex": "TRIMBLE NETRS", "tos": "SEPT POLARX5"},
            "antenna": {"rinex": "OLD", "tos": "NEW"},
        },
        "corrections": {
            "REC # / TYPE / VERS": ["3001", "SEPT POLARX5", "5.5.0"],
            "ANT # / TYPE": ["999", "NEW"],
        },
    }
    result, captured = _drive(
        monkeypatch, tmp_path, comparison=comparison,
        correct_hardware=frozenset({"receiver"}),
    )
    assert captured["only_fields"] == {"REC # / TYPE / VERS"}  # antenna NOT written
    assert result["flagged"]["antenna"] == ("OLD", "NEW")


def test_observer_agency_fixed_with_injected_value(monkeypatch, tmp_path):
    # observer_agency is correctable; the resolved value is injected into the
    # corrector via extra_corrections (the corrector can't reach agencies.yaml).
    comparison = {
        "discrepancies": {
            "observer_agency": {
                "rinex": "SFS/BGO/SJ / ETH/IMO",
                "tos": "GNSSatIMO / Vedurstofa Islands",
            }
        },
        "corrections": {"OBSERVER / AGENCY": ["GNSSatIMO", "Vedurstofa Islands"]},
    }
    result, captured = _drive(
        monkeypatch,
        tmp_path,
        comparison=comparison,
        session={
            "marker": "RHOF",
            "domes": "10216M001",
            "observer": "GNSSatIMO",
            "agency": "Vedurstofa Islands",
        },
    )
    assert result["fixed"] is True
    assert captured["only_fields"] == {"OBSERVER / AGENCY"}
    assert captured["extra_corrections"] == {
        "OBSERVER / AGENCY": ["GNSSatIMO", "Vedurstofa Islands"]
    }


def test_flagged_receiver_alongside_fixed_domes(monkeypatch, tmp_path):
    # A file can be BOTH fixed (domes) and flagged (receiver) in one pass.
    comparison = {
        "discrepancies": {
            "domes": {"rinex": "RHOF", "tos": "10216M001"},
            "receiver": {"rinex": "a", "tos": "b"},
        },
        "corrections": {
            "MARKER NUMBER": "10216M001",
            "REC # / TYPE / VERS": ["b"],
        },
    }
    result, captured = _drive(monkeypatch, tmp_path, comparison=comparison)
    assert result["fixed"] is True
    assert captured["only_fields"] == {"MARKER NUMBER"}  # receiver NOT written
    assert result["flagged"]["receiver"] == ("a", "b")


# _nominal_interval_seconds — session_type → expected sampling rate
def test_nominal_interval_seconds():
    assert hf._nominal_interval_seconds("15s_24hr") == 15.0
    assert hf._nominal_interval_seconds("1Hz_1hr") == 1.0
    assert hf._nominal_interval_seconds("30s_24hr") == 30.0
    assert hf._nominal_interval_seconds("status_1hr") is None
    assert hf._nominal_interval_seconds(None) is None
    assert hf._nominal_interval_seconds("") is None
