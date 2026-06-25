"""Tests for septentrio.tracking — constellation spec → set* commands."""

from __future__ import annotations

import pytest

from receivers.septentrio.tracking import (
    build_tracking_commands,
    normalize_constellations,
)


def test_normalize_basic():
    assert normalize_constellations("gps+glonass") == ["gps", "glonass"]


def test_normalize_aliases_and_dedupe():
    assert normalize_constellations("GPS+glo+gln") == ["gps", "glonass"]
    assert normalize_constellations("gps,gal,bds") == ["gps", "galileo", "beidou"]


def test_normalize_unknown_raises():
    with pytest.raises(ValueError):
        normalize_constellations("gps+navic")


def test_normalize_empty_raises():
    with pytest.raises(ValueError):
        normalize_constellations("")


def test_build_gps_glonass_identity_safe():
    cmds = build_tracking_commands("gps+glonass")
    assert cmds[0].startswith("setSignalTracking, GPSL1CA")
    assert "GLOL1CA" in cmds[0]
    # No Galileo/BeiDou tracked
    assert "GAL" not in cmds[0] and "BDS" not in cmds[0]
    assert cmds[1].startswith("setSignalUsage, , GPSL1CA")
    assert cmds[-1] == "eccf, Current, Boot"
    # CRUCIAL: never emits identity-changing commands
    joined = "\n".join(cmds)
    assert "setMarker" not in joined
    assert "setNtrip" not in joined
    assert "TEST" not in joined


def test_build_includes_requested_constellations():
    cmds = build_tracking_commands("gps+glonass+galileo+beidou")
    assert "GALE1BC" in cmds[0] and "BDSB1I" in cmds[0]
