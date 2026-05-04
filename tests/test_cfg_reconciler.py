"""Tests for receivers.cfg.reconciler — three-way diff layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from receivers.cfg import tos_adapter
from receivers.cfg.field_manifest import (
    FIELDS,
    FieldSpec,
    _abs_tol,
    _approx_eq,
    _identity,
    _meters_to_lat_deg,
    _meters_to_lon_deg,
    _receiver_type_eq,
    _receiver_type_to_cfg,
    _strip_placeholder,
    fields_by_key,
    position_equality_for,
    with_position_tolerance,
)
from receivers.cfg.reconciler import (
    Verdict,
    apply_diff,
    compare_station,
)

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


class TestStripPlaceholder:
    def test_real_serial_passes_through(self):
        assert _strip_placeholder("4103914") == "4103914"
        assert _strip_placeholder("  4103914  ") == "4103914"

    def test_known_word_placeholders(self):
        assert _strip_placeholder("Unknown") is None
        assert _strip_placeholder("UNKNOWN") is None
        assert _strip_placeholder("unknown") is None
        assert _strip_placeholder("N/A") is None
        assert _strip_placeholder("n/a") is None
        assert _strip_placeholder("None") is None
        assert _strip_placeholder("—") is None
        assert _strip_placeholder("-") is None

    def test_all_zero_serials(self):
        assert _strip_placeholder("0000000000") is None
        assert _strip_placeholder("000000") is None
        assert _strip_placeholder("0") is None
        assert _strip_placeholder("00") is None

    def test_serial_with_leading_zeros_preserved(self):
        # A genuine serial that happens to have a 0 is not all-zero.
        assert _strip_placeholder("00012345") == "00012345"

    def test_empty_and_none(self):
        assert _strip_placeholder("") is None
        assert _strip_placeholder("   ") is None
        assert _strip_placeholder(None) is None

    def test_placeholder_normalization_in_compare_station(self):
        """GJAC-style: cfg='0000000000', rx='Unknown' should be MISSING, not CONFLICT."""
        from receivers.cfg.reconciler import compare_station

        cfg = {"antenna_serial": "0000000000"}
        identity = {"antenna_serial": "Unknown"}
        diffs = compare_station(
            "GJAC",
            cfg,
            identity,
            None,
            fields=["antenna_serial"],
            queried_sources={"cfg", "receiver"},
        )
        assert diffs[0].cfg_value is None
        assert diffs[0].receiver_value is None
        # Both sources admit ignorance — no real data, not a conflict
        assert diffs[0].verdict == Verdict.NO_DATA


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
        diffs = compare_station("ELDC", cfg, identity, tos, fields=["receiver_serial"])
        assert diffs[0].verdict == Verdict.OK

    def test_missing_when_cfg_empty_and_sources_have_value(self):
        cfg = {}
        identity = {"serial_number": "12345"}
        diffs = compare_station("ELDC", cfg, identity, None, fields=["receiver_serial"])
        d = diffs[0]
        assert d.verdict == Verdict.MISSING
        assert d.suggestion == "12345"
        assert d.suggestion_source == "receiver"

    def test_conflict_when_cfg_disagrees_with_source(self):
        cfg = {"receiver_serial": "OLD123"}
        identity = {"serial_number": "NEW456"}
        diffs = compare_station("ELDC", cfg, identity, None, fields=["receiver_serial"])
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
                {
                    "time_from": "x",
                    "time_to": None,
                    "gnss_receiver": {"serial_number": "TOS_VALUE"},
                }
            ]
        }
        diffs = compare_station("ELDC", cfg, identity, tos, fields=["receiver_serial"])
        assert diffs[0].verdict == Verdict.CONFLICT

    def test_sources_disagree_when_cfg_matches_both_via_normalization(self):
        # cfg=PolaRX5 matches both PolaRx5 (rx) and PolaRX5TR (tos)? No —
        # PolaRX5TR isn't in fingerprint patterns. So cfg matches rx, not tos.
        # But if both sources are the same canonical type, no disagreement.
        cfg = {"receiver_type": "PolaRX5"}
        identity = {"receiver_model": "PolaRx5"}
        tos = {
            "device_history": [
                {
                    "time_from": "x",
                    "time_to": None,
                    "gnss_receiver": {"model": "POLARX5"},
                }
            ]
        }
        diffs = compare_station("ELDC", cfg, identity, tos, fields=["receiver_type"])
        assert diffs[0].verdict == Verdict.OK

    def test_no_data_when_no_source_has_value(self):
        cfg = {}
        # Empty identity (probe succeeded but no fields)
        diffs = compare_station(
            "ELDC",
            cfg,
            {},
            None,
            fields=["receiver_serial"],
            queried_sources={"cfg", "receiver"},
        )
        assert diffs[0].verdict == Verdict.NO_DATA

    def test_not_queryable_when_only_receiver_queried_for_tos_only_field(self):
        cfg = {}
        diffs = compare_station(
            "ELDC",
            cfg,
            {},
            None,
            fields=["station_name"],  # tos-only field (long-form Icelandic name)
            queried_sources={"cfg", "receiver"},
        )
        assert diffs[0].verdict == Verdict.NOT_QUERYABLE

    def test_suggestion_prefers_agreement(self):
        cfg = {}
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
        d = _diff_for(
            compare_station("ELDC", cfg, identity, tos, fields=["receiver_serial"]),
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
                {
                    "time_from": "x",
                    "time_to": None,
                    "gnss_receiver": {"serial_number": "TOS"},
                }
            ]
        }
        d = _diff_for(
            compare_station("ELDC", cfg, identity, tos, fields=["receiver_serial"]),
            "receiver_serial",
        )
        assert d.verdict == Verdict.MISSING
        assert d.suggestion is None  # caller must pick

    def test_field_filter(self):
        cfg = {}
        diffs = compare_station("ELDC", cfg, {}, None, fields=["receiver_serial"])
        assert len(diffs) == 1
        assert diffs[0].cfg_key == "receiver_serial"

    def test_default_fields_returns_all(self):
        diffs = compare_station("ELDC", {}, None, None)
        assert len(diffs) == len(FIELDS)

    def test_receiver_authoritative_false_suppresses_receiver_only_suggestion(self):
        """Antenna fields are flag-only — receiver alone never auto-fills cfg."""
        cfg = {}
        identity = {"antenna_type": "SEPCHOKE_B3E6", "antenna_serial": "262509"}
        diffs = compare_station(
            "ORFC",
            cfg,
            identity,
            None,
            fields=["antenna_type", "antenna_serial"],
            queried_sources={"cfg", "receiver"},
        )
        # Verdict is MISSING (cfg empty, receiver has value) but suggestion is None
        for d in diffs:
            assert d.verdict == Verdict.MISSING
            assert d.suggestion is None
            assert d.suggestion_source is None

    def test_receiver_authoritative_true_still_suggests_from_receiver(self):
        """receiver_serial keeps default authoritative=True — receiver-only OK."""
        cfg = {}
        identity = {"serial_number": "4103914"}
        diffs = compare_station(
            "ORFC",
            cfg,
            identity,
            None,
            fields=["receiver_serial"],
            queried_sources={"cfg", "receiver"},
        )
        assert diffs[0].suggestion == "4103914"
        assert diffs[0].suggestion_source == "receiver"


# ---------------------------------------------------------------------------
# Position tolerance — meters-based equality for lat/lon/height
# ---------------------------------------------------------------------------


class TestPositionTolerance:
    def test_abs_tol_within_threshold(self):
        # 1.5 m at Iceland latitude in degrees: 1.5/111111 ≈ 1.35e-5
        assert _abs_tol(2e-5)("63.855149", "63.855162")  # 1.45 m
        assert _abs_tol(2.0)("102.55", "104.00")  # 1.45 m height

    def test_abs_tol_outside_threshold(self):
        assert not _abs_tol(2.0)("102.55", "106.00")  # 3.45 m height
        assert not _abs_tol(1e-5)("63.855149", "63.855170")  # 2.3 m

    def test_meters_to_lat_deg(self):
        # 1 m latitude ≈ 9e-6° anywhere
        assert abs(_meters_to_lat_deg(1.0) - 9e-6) < 1e-7

    def test_meters_to_lon_deg_at_iceland(self):
        # 1 m longitude at 64° lat ≈ 2.05e-5°
        assert abs(_meters_to_lon_deg(1.0) - 2.05e-5) < 1e-6

    def test_position_equality_for_lat(self):
        eq = position_equality_for("latitude", 2.0)
        assert eq("63.855149", "63.855160")  # 1.2 m
        assert not eq("63.855149", "63.855200")  # 5.7 m

    def test_position_equality_for_height(self):
        eq = position_equality_for("height", 2.0)
        assert eq("102.55", "104.00")
        assert not eq("102.55", "106.00")

    def test_position_equality_for_unsupported_field(self):

        with pytest.raises(ValueError):
            position_equality_for("antenna_height", 2.0)

    def test_with_position_tolerance_overrides_lat_lon_height(self):
        specs = with_position_tolerance(5.0)
        by_key = {s.cfg_key: s for s in specs}
        # 4 m diff in height should pass at 5 m tolerance
        assert by_key["height"].values_equal("100.00", "104.00")
        # but fail at default 2 m
        default_height = next(f for f in FIELDS if f.cfg_key == "height")
        assert not default_height.values_equal("100.00", "104.00")

    def test_with_position_tolerance_preserves_other_fields(self):
        specs = with_position_tolerance(5.0)
        by_key = {s.cfg_key: s for s in specs}
        # Non-position fields keep their original equality
        assert by_key["receiver_type"].equal is not None
        assert by_key["antenna_height"].equal is not None
        # Length matches FIELDS
        assert len(specs) == len(FIELDS)


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
            spec=spec,
            cfg_value=None,
            receiver_value="3001234",
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
            spec=spec,
            cfg_value="OLD",
            receiver_value="NEW",
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
            spec=spec,
            cfg_value="SAME",
            receiver_value="SAME",
            tos_value=None,
        )
        changed = apply_diff("ELDC", diff, "SAME", cfg_path=cfg)
        assert not changed

    def test_unknown_station_returns_false(self, tmp_path):
        cfg = self._make_cfg(tmp_path, "[ELDC]\nfoo = bar\n")
        spec = fields_by_key()["receiver_serial"]
        from receivers.cfg.reconciler import FieldDiff

        diff = FieldDiff(
            spec=spec,
            cfg_value=None,
            receiver_value="X",
            tos_value=None,
        )
        changed = apply_diff("XXXX", diff, "X", cfg_path=cfg)
        assert not changed


# ---------------------------------------------------------------------------
# cfg_format — write-time vocabulary normalization (bug #9)
# ---------------------------------------------------------------------------


class TestReceiverTypeCfgFormat:
    """The cfg_format hook maps source vocabularies to cfg vocabulary on write.

    Without this, accepting a TOS suggestion writes "SEPT POLARX5" verbatim
    while the codebase expects "PolaRX5" (108 of 173 stations use the short
    form). Caused KOTC/KVIC corruption during the bulk cleanup until manually
    repaired with sed.
    """

    def test_maps_tos_igs_name_to_short_cfg_form(self):
        assert _receiver_type_to_cfg("SEPT POLARX5") == "PolaRX5"

    def test_maps_capitalisation_variants(self):
        # Receiver banners and TOS records use mixed case; all canonicalise.
        assert _receiver_type_to_cfg("PolaRx5") == "PolaRX5"
        assert _receiver_type_to_cfg("POLARX5") == "PolaRX5"
        assert _receiver_type_to_cfg("PolaRX5") == "PolaRX5"

    def test_maps_trimble_variants_to_netr_short_forms(self):
        assert _receiver_type_to_cfg("TRIMBLE NETR9") == "NetR9"
        assert _receiver_type_to_cfg("NETRS") == "NetRS"

    def test_passes_through_unknown_value_unchanged(self):
        # Unknown receiver types must not be silently corrupted to None.
        assert _receiver_type_to_cfg("unknown_thing") == "unknown_thing"

    def test_handles_none(self):
        assert _receiver_type_to_cfg(None) is None

    def test_field_manifest_wires_cfg_format_on_receiver_type(self):
        spec = fields_by_key()["receiver_type"]
        assert spec.cfg_format is _receiver_type_to_cfg

    def test_default_cfg_format_is_identity(self):
        # Other fields keep _identity so values pass through verbatim.
        for key in ("receiver_serial", "receiver_firmware_version", "antenna_serial"):
            assert fields_by_key()[key].cfg_format is _identity

    def test_with_position_tolerance_preserves_cfg_format(self):
        # The override path used to drop cfg_format; would silently revert
        # to identity for receiver_type when a position tolerance was passed.
        specs_by_key = {s.cfg_key: s for s in with_position_tolerance(2.0)}
        assert specs_by_key["receiver_type"].cfg_format is _receiver_type_to_cfg


# ---------------------------------------------------------------------------
# CLI _progress helper — keeps stdout clean for --json (bug #6 supporting fix)
# ---------------------------------------------------------------------------


class TestProgressRouting:
    """Progress lines must go to stderr in JSON mode so they don't pollute
    the JSON document on stdout — without this fix, ``receivers cfg reconcile
    --json`` produces 200+ lines of "↳ STATION: querying TOS…" before the
    JSON, requiring the caller to grep/skip to find the actual document.
    """

    def test_json_mode_routes_to_stderr(self, capsys):
        from receivers.cli.cfg import _progress

        _progress("hello", json_mode=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "hello" in captured.err

    def test_text_mode_routes_to_stdout(self, capsys):
        from receivers.cli.cfg import _progress

        _progress("hello", json_mode=False)
        captured = capsys.readouterr()
        assert "hello" in captured.out
        assert captured.err == ""


# ---------------------------------------------------------------------------
# _probe_station — parallel probe unit tests
# ---------------------------------------------------------------------------


class TestProbeStation:
    """_probe_station returns (receiver_identity, tos_data) and is thread-safe."""

    def test_returns_none_when_both_sources_fail(self, monkeypatch):
        from receivers.cli.cfg import _probe_station

        monkeypatch.setattr("receivers.cli.cfg._query_receiver_identity", lambda *_: None)
        monkeypatch.setattr("receivers.cli.cfg._query_tos", lambda *_: None)

        rx, tos = _probe_station("ELDC", {}, ["receiver", "tos"], json_mode=True)
        assert rx is None
        assert tos is None

    def test_returns_data_from_both_sources(self, monkeypatch):
        from receivers.cli.cfg import _probe_station

        rx_data = {"receiver_model": "PolaRX5", "serial_number": "12345"}
        tos_data = {"name": "Eldey", "device_history": []}
        monkeypatch.setattr("receivers.cli.cfg._query_receiver_identity", lambda *_: rx_data)
        monkeypatch.setattr("receivers.cli.cfg._query_tos", lambda *_: tos_data)

        rx, tos = _probe_station("ELDC", {}, ["receiver", "tos"], json_mode=True)
        assert rx == rx_data
        assert tos == tos_data

    def test_skips_receiver_when_not_in_sources(self, monkeypatch):
        from receivers.cli.cfg import _probe_station

        called: list = []
        monkeypatch.setattr(
            "receivers.cli.cfg._query_receiver_identity", lambda *_: called.append("rx") or {}
        )
        tos_data = {"name": "Eldey"}
        monkeypatch.setattr("receivers.cli.cfg._query_tos", lambda *_: tos_data)

        _, tos = _probe_station("ELDC", {}, ["tos"], json_mode=True)
        assert called == []
        assert tos == tos_data

    def test_skips_tos_when_not_in_sources(self, monkeypatch):
        from receivers.cli.cfg import _probe_station

        called: list = []
        monkeypatch.setattr("receivers.cli.cfg._query_tos", lambda *_: called.append("tos") or {})
        rx_data = {"receiver_model": "PolaRX5"}
        monkeypatch.setattr("receivers.cli.cfg._query_receiver_identity", lambda *_: rx_data)

        rx, _ = _probe_station("ELDC", {}, ["receiver"], json_mode=True)
        assert called == []
        assert rx == rx_data

    def test_adhoc_skips_receiver_probe(self, monkeypatch):
        from receivers.cli.cfg import _probe_station

        called = []
        monkeypatch.setattr(
            "receivers.cli.cfg._query_receiver_identity", lambda *_: called.append("rx") or {}
        )
        monkeypatch.setattr("receivers.cli.cfg._query_tos", lambda *_: None)

        cfg = {"_adhoc": True}
        rx, _ = _probe_station("ELDC", cfg, ["receiver", "tos"], json_mode=True)
        assert called == []
        assert rx is None

    def test_parallel_probes_produce_same_results_as_sequential(self, monkeypatch):
        """Parallel probe phase collects same data as sequential calls."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from receivers.cli.cfg import _probe_station

        station_ids = ["ELDC", "THOB", "GJAC"]
        # Each station gets a distinct identity and TOS record
        rx_map = {sid: {"serial_number": f"SN-{sid}"} for sid in station_ids}
        tos_map = {sid: {"name": f"Name-{sid}"} for sid in station_ids}

        monkeypatch.setattr(
            "receivers.cli.cfg._query_receiver_identity",
            lambda sid, _: rx_map.get(sid),
        )
        monkeypatch.setattr(
            "receivers.cli.cfg._query_tos",
            lambda sid: tos_map.get(sid),
        )

        sources = ["receiver", "tos"]
        sequential = {
            sid: _probe_station(sid, {}, sources, json_mode=True) for sid in station_ids
        }

        parallel: dict = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(_probe_station, sid, {}, sources, True, False): sid
                for sid in station_ids
            }
            for future in as_completed(futures):
                sid = futures[future]
                parallel[sid] = future.result()

        assert parallel == sequential
