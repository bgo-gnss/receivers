"""Tests for receivers.cfg.reconciler — three-way diff layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from receivers.cfg.field_manifest import (
    FIELDS,
    FieldSpec,
    _approx_eq,
    _receiver_type_eq,
    fields_by_key,
)
from receivers.cfg.reconciler import (
    Verdict,
    apply_diff,
    compare_station,
)
from receivers.cfg import tos_adapter


# ---------------------------------------------------------------------------
# field_manifest equality helpers
# ---------------------------------------------------------------------------


class TestEqualityHelpers:
    def test_receiver_type_fingerprint_match(self):
        assert _receiver_type_eq("PolaRX5", "PolaRx5")
        assert _receiver_type_eq("PolaRX5", "POLARX5")
        assert _receiver_type_eq("NetR9", "NETR9")

    def test_receiver_type_different_models(self):
        assert not _receiver_type_eq("PolaRX5", "NetR9")
        assert not _receiver_type_eq("NetR9", "G10")

    def test_receiver_type_with_none(self):
        assert not _receiver_type_eq(None, "PolaRX5")
        assert not _receiver_type_eq("PolaRX5", None)

    def test_approx_eq_trailing_zero(self):
        assert _approx_eq(4)("0.083", "0.0830")
        assert _approx_eq(2)("63.86", "63.860000")

    def test_approx_eq_below_tolerance(self):
        assert _approx_eq(2)("63.86", "63.861")
        assert not _approx_eq(3)("63.86", "63.861")

    def test_approx_eq_with_nones(self):
        assert not _approx_eq(2)(None, "63.86")
        assert not _approx_eq(2)("63.86", None)

    def test_approx_eq_invalid_floats_falls_back_to_strings(self):
        assert _approx_eq(2)("abc", "abc")
        assert not _approx_eq(2)("abc", "def")


# ---------------------------------------------------------------------------
# tos_adapter — current session selection
# ---------------------------------------------------------------------------


class TestTOSAdapter:
    def _station_with_two_sessions(self):
        return {
            "name": "Test Station",
            "lat": 64.5,
            "lon": -22.0,
            "altitude": 100.0,
            "device_history": [
                {  # closed session
                    "time_from": "2020-01-01",
                    "time_to": "2023-01-01",
                    "gnss_receiver": {
                        "model": "OldModel",
                        "serial_number": "OLD",
                        "firmware_version": "1.0",
                    },
                    "antenna": {
                        "model": "OLD_ANT",
                        "serial_number": "A0",
                        "antenna_height": 0.05,
                    },
                    "monument": {"monument_height": 0.0},
                    "radome": {"model": "NONE"},
                },
                {  # current
                    "time_from": "2023-01-01",
                    "time_to": None,
                    "gnss_receiver": {
                        "model": "PolaRx5",
                        "serial_number": "12345",
                        "firmware_version": "5.5.0",
                    },
                    "antenna": {
                        "model": "SEPCHOKE_B3E6",
                        "serial_number": "A1",
                        "antenna_height": 0.083,
                    },
                    "monument": {"monument_height": 0.05},
                    "radome": {"model": "SPKE"},
                },
            ],
        }

    def test_picks_current_session(self):
        s = self._station_with_two_sessions()
        sess = tos_adapter.current_session(s)
        assert sess is not None
        assert sess["gnss_receiver"]["serial_number"] == "12345"

    def test_no_current_session(self):
        s = {"device_history": [{"time_from": "x", "time_to": "y"}]}
        assert tos_adapter.current_session(s) is None

    def test_receiver_extractors(self):
        s = self._station_with_two_sessions()
        assert tos_adapter.current_receiver_model(s) == "PolaRx5"
        assert tos_adapter.current_receiver_serial(s) == "12345"
        assert tos_adapter.current_receiver_firmware(s) == "5.5.0"

    def test_antenna_height_is_composite(self):
        s = self._station_with_two_sessions()
        # 0.083 + 0.05 = 0.1330
        assert tos_adapter.current_antenna_height(s) == "0.1330"

    def test_antenna_height_no_monument(self):
        s = self._station_with_two_sessions()
        s["device_history"][1].pop("monument")
        assert tos_adapter.current_antenna_height(s) == "0.0830"

    def test_radome_default_none(self):
        s = self._station_with_two_sessions()
        s["device_history"][1].pop("radome")
        assert tos_adapter.current_radome_model(s) == "NONE"

    def test_coordinates(self):
        s = self._station_with_two_sessions()
        assert tos_adapter.station_latitude(s) == "64.500000"
        assert tos_adapter.station_longitude(s) == "-22.000000"
        assert tos_adapter.station_height(s) == "100.00"

    def test_zero_coords_treated_as_missing(self):
        # 0/0 in TOS means "not surveyed", not actual gulf-of-guinea.
        s = {"lat": 0, "lon": 0, "altitude": 0, "device_history": []}
        assert tos_adapter.station_latitude(s) is None
        assert tos_adapter.station_longitude(s) is None


# ---------------------------------------------------------------------------
# compare_station — verdicts
# ---------------------------------------------------------------------------


def _diff_for(diffs, key):
    for d in diffs:
        if d.cfg_key == key:
            return d
    raise KeyError(key)


class TestCompareStation:
    def test_ok_when_all_sources_agree(self):
        cfg = {"receiver_serial": "12345"}
        identity = {"serial_number": "12345"}
        tos = {
            "device_history": [
                {
                    "time_from": "x",
                    "time_to": None,
                    "gnss_receiver": {"serial_number": "12345"},
                }
            ]
        }
        diffs = compare_station("ELDC", cfg, identity, tos,
                                fields=["receiver_serial"])
        assert diffs[0].verdict == Verdict.OK

    def test_missing_when_cfg_empty_and_sources_have_value(self):
        cfg = {}
        identity = {"serial_number": "12345"}
        diffs = compare_station("ELDC", cfg, identity, None,
                                fields=["receiver_serial"])
        d = diffs[0]
        assert d.verdict == Verdict.MISSING
        assert d.suggestion == "12345"
        assert d.suggestion_source == "receiver"

    def test_conflict_when_cfg_disagrees_with_source(self):
        cfg = {"receiver_serial": "OLD123"}
        identity = {"serial_number": "NEW456"}
        diffs = compare_station("ELDC", cfg, identity, None,
                                fields=["receiver_serial"])
        assert diffs[0].verdict == Verdict.CONFLICT
        # No suggestion on conflict — caller decides.
        assert diffs[0].suggestion is None

    def test_sources_disagree_but_cfg_matches_one(self):
        # cfg matches receiver, but TOS disagrees with both.
        # Resolution: cfg matches receiver, conflict with TOS → CONFLICT.
        cfg = {"receiver_serial": "RX_VALUE"}
        identity = {"serial_number": "RX_VALUE"}
        tos = {
            "device_history": [
                {"time_from": "x", "time_to": None,
                 "gnss_receiver": {"serial_number": "TOS_VALUE"}}
            ]
        }
        diffs = compare_station("ELDC", cfg, identity, tos,
                                fields=["receiver_serial"])
        assert diffs[0].verdict == Verdict.CONFLICT

    def test_sources_disagree_when_cfg_matches_both_via_normalization(self):
        # cfg=PolaRX5 matches both PolaRx5 (rx) and PolaRX5TR (tos)? No —
        # PolaRX5TR isn't in fingerprint patterns. So cfg matches rx, not tos.
        # But if both sources are the same canonical type, no disagreement.
        cfg = {"receiver_type": "PolaRX5"}
        identity = {"receiver_model": "PolaRx5"}
        tos = {
            "device_history": [
                {"time_from": "x", "time_to": None,
                 "gnss_receiver": {"model": "POLARX5"}}
            ]
        }
        diffs = compare_station("ELDC", cfg, identity, tos,
                                fields=["receiver_type"])
        assert diffs[0].verdict == Verdict.OK

    def test_no_data_when_no_source_has_value(self):
        cfg = {}
        # Empty identity (probe succeeded but no fields)
        diffs = compare_station("ELDC", cfg, {}, None,
                                fields=["receiver_serial"],
                                queried_sources={"cfg", "receiver"})
        assert diffs[0].verdict == Verdict.NO_DATA

    def test_not_queryable_when_only_receiver_queried_for_tos_only_field(self):
        cfg = {}
        diffs = compare_station(
            "ELDC", cfg, {}, None,
            fields=["antenna_type"],   # tos-only field
            queried_sources={"cfg", "receiver"},
        )
        assert diffs[0].verdict == Verdict.NOT_QUERYABLE

    def test_suggestion_prefers_agreement(self):
        cfg = {}
        identity = {"serial_number": "12345"}
        tos = {
            "device_history": [
                {"time_from": "x", "time_to": None,
                 "gnss_receiver": {"serial_number": "12345"}}
            ]
        }
        d = _diff_for(
            compare_station("ELDC", cfg, identity, tos,
                            fields=["receiver_serial"]),
            "receiver_serial",
        )
        assert d.suggestion == "12345"
        assert d.suggestion_source == "agree"

    def test_suggestion_none_on_conflicting_sources(self):
        # cfg missing AND sources disagree → no suggestion.
        cfg = {}
        identity = {"serial_number": "RX"}
        tos = {
            "device_history": [
                {"time_from": "x", "time_to": None,
                 "gnss_receiver": {"serial_number": "TOS"}}
            ]
        }
        d = _diff_for(
            compare_station("ELDC", cfg, identity, tos,
                            fields=["receiver_serial"]),
            "receiver_serial",
        )
        assert d.verdict == Verdict.MISSING
        assert d.suggestion is None  # caller must pick

    def test_field_filter(self):
        cfg = {}
        diffs = compare_station("ELDC", cfg, {}, None,
                                fields=["receiver_serial"])
        assert len(diffs) == 1
        assert diffs[0].cfg_key == "receiver_serial"

    def test_default_fields_returns_all(self):
        diffs = compare_station("ELDC", {}, None, None)
        assert len(diffs) == len(FIELDS)


# ---------------------------------------------------------------------------
# apply_diff — file write
# ---------------------------------------------------------------------------


class TestApplyDiff:
    def _make_cfg(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "stations.cfg"
        p.write_text(body)
        return p

    def test_inserts_missing_key(self, tmp_path):
        cfg = self._make_cfg(
            tmp_path,
            "[ELDC]\nreceiver_type = PolaRX5\n\n[THOB]\nreceiver_type = PolaRX5\n",
        )
        spec = fields_by_key()["receiver_serial"]
        from receivers.cfg.reconciler import FieldDiff
        diff = FieldDiff(
            spec=spec, cfg_value=None, receiver_value="3001234",
            tos_value=None,
        )
        changed = apply_diff("ELDC", diff, "3001234", cfg_path=cfg)
        assert changed
        text = cfg.read_text()
        assert "receiver_serial = 3001234" in text
        # Ensure THOB section untouched
        assert text.count("receiver_type = PolaRX5") == 2

    def test_updates_existing_key(self, tmp_path):
        cfg = self._make_cfg(
            tmp_path,
            "[ELDC]\nreceiver_serial = OLD\n",
        )
        spec = fields_by_key()["receiver_serial"]
        from receivers.cfg.reconciler import FieldDiff
        diff = FieldDiff(
            spec=spec, cfg_value="OLD", receiver_value="NEW",
            tos_value=None,
        )
        changed = apply_diff("ELDC", diff, "NEW", cfg_path=cfg)
        assert changed
        assert "receiver_serial = NEW" in cfg.read_text()

    def test_noop_when_value_unchanged(self, tmp_path):
        cfg = self._make_cfg(
            tmp_path,
            "[ELDC]\nreceiver_serial = SAME\n",
        )
        spec = fields_by_key()["receiver_serial"]
        from receivers.cfg.reconciler import FieldDiff
        diff = FieldDiff(
            spec=spec, cfg_value="SAME", receiver_value="SAME",
            tos_value=None,
        )
        changed = apply_diff("ELDC", diff, "SAME", cfg_path=cfg)
        assert not changed

    def test_unknown_station_returns_false(self, tmp_path):
        cfg = self._make_cfg(tmp_path, "[ELDC]\nfoo = bar\n")
        spec = fields_by_key()["receiver_serial"]
        from receivers.cfg.reconciler import FieldDiff
        diff = FieldDiff(
            spec=spec, cfg_value=None, receiver_value="X", tos_value=None,
        )
        changed = apply_diff("XXXX", diff, "X", cfg_path=cfg)
        assert not changed
