"""Forcing a NetRS off its RINEX 2 pin must be loud.

The pin is a DEFAULT, not a prohibition: `--version 3` is honoured. But a NetRS
is GPS-only, so up-converting recovers no constellations, and RINEX 3 codes its
CODELESS L2 as C2D — which GAMIT cannot map to P2 and deletes with "no P2
range". The override is therefore pure loss unless someone specifically wants
R3.

MEASURED 2026-09-16: BALD carried **81** RINEX 3 files across 2026 and HAUD one,
because `station onboard`'s "Re-rinex (R2->R3 from raw, recovers GLO/GAL/BDS)"
stage passes `--version 3` without looking at the receiver type. With
`--naming short` the output is an R3 file wearing an R2 filename, so nothing
downstream flagged it for weeks — the EPOS portal was the first place it showed,
because dissemination detects the SOURCE version and publishes R3 under long
names.

The silence is the bug this fixes. The conversion itself stays allowed.
"""

from __future__ import annotations

import pytest

from receivers.cli.main import netrs_version_override_warning as warn


class TestItFiresOnTheCaseThatCausedTheDamage:
    def test_netrs_with_explicit_version_3_warns(self):
        assert warn("netrs", 3) is not None

    def test_the_warning_names_the_actual_consequence(self):
        """An operator must be able to act on it without reading the source."""
        m = warn("netrs", 3)
        assert "C2D" in m, "must name the coding GAMIT drops"
        assert "GAMIT" in m
        assert "GPS-only" in m, "must say why up-converting gains nothing"
        # NOT a bare "--version" check: that substring is already in the
        # message's own prefix ("explicit --version 3"), so it passes even when
        # the advice is stripped. A mutation caught exactly that. Assert on the
        # REMEDY text instead.
        assert "drop --version" in m, "must say how to avoid it"
        assert "R2 pin" in m

    def test_version_4_also_warns(self):
        assert warn("netrs", 4) is not None

    def test_receiver_type_matching_is_case_insensitive(self):
        """stations.cfg writes 'NetRS'; the CLI lowercases, but do not rely on
        the caller having done it."""
        for t in ("NetRS", "NETRS", "netrs", "Trimble NetRS"):
            assert warn(t, 3) is not None, t


class TestItStaysQuietWhenItShould:
    def test_no_explicit_version_is_silent(self):
        """The pin applies and logs its own line — two messages would be noise."""
        assert warn("netrs", None) is None

    def test_explicit_version_2_is_silent(self):
        """Asking for exactly what the pin would choose is not an override."""
        assert warn("netrs", 2) is None

    def test_a_polarx5_is_never_warned(self):
        """R3 is correct for a modern receiver — HAUC is one, and is fine."""
        assert warn("polarx5", 3) is None

    def test_a_netr9_is_never_warned(self):
        assert warn("netr9", 3) is None

    def test_an_unknown_or_empty_receiver_type_is_silent(self):
        for t in (None, "", "unknown"):
            assert warn(t, 3) is None, repr(t)


class TestItDoesNotBlock:
    """The pin is a default, not a prohibition — this returns TEXT, never
    raises and never alters the chosen version."""

    def test_it_only_ever_returns_text_or_none(self):
        for t in ("netrs", "polarx5", None, ""):
            for v in (None, 2, 3, 4):
                r = warn(t, v)
                assert r is None or isinstance(r, str)
