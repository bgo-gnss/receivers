"""Tests for `receivers.operational_scope`.

The load-bearing property is the FALLBACK DIRECTION: an absent
``operational_status`` means UNKNOWN, and unknown must resolve toward *polling*.
Getting it backwards silently drops stations from the fleet — the one failure
mode that produces no error, only an absence indistinguishable from a station
someone intentionally retired.
"""

from __future__ import annotations

from receivers.operational_scope import (
    OPERATIONAL_STATUS_VALUES,
    is_operated,
    is_third_party_operated,
    operational_status,
)


class TestOperationalStatus:
    def test_normalises_case_and_whitespace(self):
        assert operational_status({"operational_status": "  Operated "}) == "operated"
        assert operational_status({"operational_status": "EXTERNAL"}) == "external"

    def test_absent_is_none_not_operated(self):
        """Absent must be None — NOT 'operated'. The distinction is the whole
        point: callers resolve it explicitly via is_operated()."""
        assert operational_status({}) is None
        assert operational_status({"operational_status": None}) is None
        assert operational_status({"operational_status": ""}) is None
        assert operational_status({"operational_status": "   "}) is None

    def test_vocabulary_matches_the_model_doc(self):
        assert OPERATIONAL_STATUS_VALUES == ("operated", "external", "reference")


class TestIsOperated:
    def test_operated_is_true(self):
        assert is_operated({"operational_status": "operated"}) is True

    def test_external_is_false(self):
        assert is_operated({"operational_status": "external"}) is False

    def test_reference_is_false(self):
        """Reference stations are data-source-only (GLOBK ties) — not operated
        by us, and not monitored."""
        assert is_operated({"operational_status": "reference"}) is False


class TestFallbackDirection:
    """The safety-critical behaviour: the legacy proxy only decides when the
    canonical signal is ABSENT, and absence resolves toward operated."""

    def test_absent_status_falls_back_to_legacy_proxy(self):
        """A cfg-only station (no TOS attribute) still honours health_check."""
        assert is_operated({"health_check": "passive"}) is False
        assert is_operated({"health_check": "active"}) is True

    def test_absent_everything_is_operated(self):
        """UNKNOWN WITH NOTHING TO FALL BACK ON MUST RESOLVE TOWARD POLLING.

        This is the regression guard for the silent-deactivation hazard: if a
        station has neither signal, treating it as not-operated would stop
        polling it with no error anywhere. Worst case here is that a station
        which should have been excluded keeps being polled — noisy and visible.
        """
        assert is_operated({}) is True
        assert is_operated({"health_check": None}) is True
        assert is_operated({"unrelated": "field"}) is True

    def test_canonical_signal_wins_over_the_proxy(self):
        """Once operational_status exists it decides, even when the legacy
        proxy disagrees — that is the migration."""
        assert is_operated(
            {"operational_status": "operated", "health_check": "passive"}
        ) is True
        assert is_operated(
            {"operational_status": "external", "health_check": "active"}
        ) is False


class TestThirdPartyInverse:
    def test_is_exactly_the_inverse(self):
        for cfg in (
            {},
            {"operational_status": "operated"},
            {"operational_status": "external"},
            {"operational_status": "reference"},
            {"health_check": "passive"},
            {"health_check": "active"},
        ):
            assert is_third_party_operated(cfg) is (not is_operated(cfg))


class TestBehaviourPreservation:
    """The swap must not change today's polling set.

    The 4 stations with cfg `health_check = passive` (KRAC/MYVA/THRC/TORK) are
    exactly the 4 TOS marks `external`, so both signals select the identical
    set. These cases pin the two shapes those stations actually have:
    MYVA already carries operational_status, the other three do not yet.
    """

    def test_myva_shaped_station_still_excluded(self):
        """Has the new attribute AND the old one."""
        assert is_operated(
            {"operational_status": "external", "health_check": "passive"}
        ) is False

    def test_krac_shaped_station_still_excluded(self):
        """Old proxy only — no operational_status in cfg yet."""
        assert is_operated({"health_check": "passive"}) is False

    def test_ordinary_station_still_included(self):
        assert is_operated(
            {"operational_status": "operated", "health_check": "active"}
        ) is True
