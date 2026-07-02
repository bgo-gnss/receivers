"""Tests for ``septentrio.antenna`` — the ``rec-config --set-antenna`` builder.

The 20-char antenna field packing is locked against a byte-for-byte sample
extracted from a live PolaRx5 boot config (``"TRM115000.00    NONE"``); the
ODDF fixture exercises the case the verb exists for (receiver still holds the
previous antenna's identity after a swap was reconciled into cfg).
"""

from __future__ import annotations

import pytest

from receivers.septentrio.antenna import (
    UNKNOWN_SERIAL,
    build_antenna_commands,
    build_antenna_commands_from_station_config,
    format_igs_antenna_field,
)


class TestFormatIgsAntennaField:
    def test_matches_live_extracted_receiver_string(self):
        # Byte-for-byte the string a live PolaRx5 reported: 12-char model
        # left-justified in 16 + right-aligned 4-char radome.
        assert format_igs_antenna_field("TRM115000.00", None) == "TRM115000.00    NONE"

    def test_seppolant_packing(self):
        out = format_igs_antenna_field("SEPPOLANT_X_MF", "NONE")
        assert out == "SEPPOLANT_X_MF  NONE"
        assert len(out) == 20

    def test_real_radome_code(self):
        assert format_igs_antenna_field("ASH701945C_M", "SCIS").endswith("SCIS")

    def test_blank_radome_becomes_none(self):
        assert format_igs_antenna_field("SEPPOLANT_X_MF", "  ").endswith("NONE")

    def test_too_long_type_rejected(self):
        with pytest.raises(ValueError):
            format_igs_antenna_field("X" * 17)

    def test_too_long_radome_rejected(self):
        with pytest.raises(ValueError):
            format_igs_antenna_field("SEPPOLANT_X_MF", "TOOBIG")

    def test_empty_type_rejected(self):
        with pytest.raises(ValueError):
            format_igs_antenna_field("")


class TestBuildAntennaCommands:
    def test_full_command_and_boot_save(self):
        cmds = build_antenna_commands(
            "SEPPOLANT_X_MF", "NONE", "0000000000", up_m=0.661
        )
        assert cmds == [
            "setAntennaOffset, Main, 0.0000, 0.0000, 0.6610, "
            '"SEPPOLANT_X_MF  NONE", "0000000000"',
            "eccf, Current, Boot",
        ]

    def test_unknown_serial_variants_become_marker(self):
        for serial in (None, "", "  ", "0000", "antenna-ODDF-20230706"):
            cmds = build_antenna_commands("SEPPOLANT_X_MF", None, serial, up_m=0.661)
            assert f'"{UNKNOWN_SERIAL}"' in cmds[0], serial

    def test_real_serial_preserved(self):
        cmds = build_antenna_commands("TRM115000.00", None, "60243B0067", up_m=0.661)
        assert '"60243B0067"' in cmds[0]

    def test_offsets_formatted(self):
        cmds = build_antenna_commands(
            "SEPPOLANT_X_MF", None, "S1", up_m=1.051, east_m=0.01, north_m=-0.02
        )
        assert ", 0.0100, -0.0200, 1.0510," in cmds[0]


class TestFromStationConfig:
    def _oddf_cfg(self):
        # ODDF's reconciled stations.cfg entry (flat keys, post-reconcile).
        return {
            "antenna_type": "SEPPOLANT_X_MF",
            "antenna_radome": "NONE",
            "antenna_serial": "0000000000",
            "antenna_height": "0.6610",
        }

    def test_oddf_case(self):
        cmds = build_antenna_commands_from_station_config(self._oddf_cfg())
        assert '"SEPPOLANT_X_MF  NONE"' in cmds[0]
        assert '"0000000000"' in cmds[0]
        assert ", 0.6610," in cmds[0]
        assert cmds[-1] == "eccf, Current, Boot"

    def test_nested_antenna_section(self):
        cfg = {
            "antenna": {
                "type": "SEPPOLANT_X_MF",
                "radome": "NONE",
                "serial": "12345",
                "height": "1.0510",
            }
        }
        cmds = build_antenna_commands_from_station_config(cfg)
        assert '"12345"' in cmds[0]
        assert ", 1.0510," in cmds[0]

    def test_missing_type_refused(self):
        cfg = self._oddf_cfg()
        del cfg["antenna_type"]
        with pytest.raises(ValueError, match="antenna_type missing"):
            build_antenna_commands_from_station_config(cfg)

    def test_missing_height_refused(self):
        # Never silently zero the ARP offset — the exact corruption this verb fixes.
        cfg = self._oddf_cfg()
        del cfg["antenna_height"]
        with pytest.raises(ValueError, match="antenna_height missing"):
            build_antenna_commands_from_station_config(cfg)

    def test_non_numeric_height_refused(self):
        cfg = self._oddf_cfg()
        cfg["antenna_height"] = "high"
        with pytest.raises(ValueError, match="non-numeric"):
            build_antenna_commands_from_station_config(cfg)
