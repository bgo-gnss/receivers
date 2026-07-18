"""Passive-station gates: station_role=passive must be skipped everywhere.

Stage 1 of the passive-station rollout (GLOBAL_SITES_investigation.md §4.4):
consumers gain the gates BEFORE any passive entry exists in stations.cfg, so
these tests pin the skip behavior with synthetic configs only.
"""

import configparser

import pytest

from receivers.config_utils import (
    _parse_cfg_bool,
    is_passive_role,
    parse_station_role,
)
from receivers.db.seeder import _seedable_station_ids


class TestParseStationRole:
    def test_missing_and_empty_default_to_active(self):
        assert parse_station_role(None) == "active"
        assert parse_station_role("") == "active"
        assert parse_station_role("   ") == "active"

    def test_explicit_roles(self):
        assert parse_station_role("active") == "active"
        assert parse_station_role("passive") == "passive"
        assert parse_station_role("  PASSIVE  ") == "passive"

    def test_inline_comment_stripped(self):
        assert parse_station_role("passive # ZIMM, IGS core") == "passive"

    def test_unknown_value_fails_open_to_active(self, caplog):
        # A typo must never drop an operated station from the schedulers.
        with caplog.at_level("WARNING"):
            assert parse_station_role("pasive") == "active"
        assert "Unknown station_role" in caplog.text

    def test_is_passive_role(self):
        assert is_passive_role("passive") is True
        assert is_passive_role("active") is False
        assert is_passive_role(None) is False


class TestParseCfgBool:
    @pytest.mark.parametrize(
        ("value", "default", "expected"),
        [
            ("true", False, True),
            ("YES", False, True),
            ("1", False, True),
            ("false", True, False),
            ("no", True, False),
            (None, True, True),
            (None, False, False),
            ("", True, True),
            ("true # comment", False, True),
        ],
    )
    def test_values(self, value, default, expected):
        assert _parse_cfg_bool(value, default) is expected

    def test_garbage_falls_back_to_default(self, caplog):
        with caplog.at_level("WARNING"):
            assert _parse_cfg_bool("maybe", True) is True
            assert _parse_cfg_bool("maybe", False) is False


class _FakeGpsParser:
    """Minimal stand-in for gps_parser.ConfigParser: only .config is used."""

    def __init__(self, text: str):
        self.config = configparser.ConfigParser()
        self.config.read_string(text)


class TestSeedableStationIds:
    def test_passive_sections_excluded(self, caplog):
        parser = _FakeGpsParser("""
[DEFAULTS]
foo=bar

[ACTV]
receiver_type=PolaRX5

[NORL]
station_name=No role key at all

[ZIMM]
station_role=passive
is_reference_site=true
is_in_iceland=false

[KELY]
station_role = passive # former IGS, Greenland

[lowr]
receiver_type=ignored, not uppercase-4
""")
        with caplog.at_level("INFO"):
            ids = _seedable_station_ids(parser)
        assert ids == ["ACTV", "NORL"]
        assert "skipped 2 passive" in caplog.text

    def test_no_passive_sections_is_silent_noop(self, caplog):
        parser = _FakeGpsParser("[ACTV]\nreceiver_type=PolaRX5\n")
        with caplog.at_level("INFO"):
            assert _seedable_station_ids(parser) == ["ACTV"]
        assert "passive" not in caplog.text
