"""Tests for ``receivers cfg set-continuity`` — continuity transition verb.

Exercises :func:`receivers.cfg.operations.set_continuity` against a mocked
``TOSWriter``. Continuity is a station-level attribute that transitions
campaign↔continuous over time; ``correct_current`` relabels a mislabeled open
period before transitioning (the VOTT fix).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from receivers.cfg.operations import CfgOperationError, set_continuity

VOTT_EID = 21559


def _writer(open_value=None):
    w = MagicMock()
    w.dry_run = True
    w.find_station_by_marker.return_value = VOTT_EID
    existing = (
        [
            {
                "id_attribute_value": 8801,
                "code": "continuity",
                "value": open_value,
                "date_from": "2012-06-03T00:00:00",
                "date_to": None,
            }
        ]
        if open_value is not None
        else []
    )
    w.get_attribute_values.return_value = existing
    w.transition_attribute_value.return_value = {"closed": {}, "opened": {}}
    w.patch_attribute_value.return_value = {}
    return w


def test_transition_to_continuous():
    w = _writer(open_value="campaign")
    r = set_continuity(w, station_id="VOTT", from_date="2017-06-01", value="continuous")
    assert r.operation == "set-continuity"
    w.transition_attribute_value.assert_called_once_with(
        VOTT_EID, "continuity", "continuous", "2017-06-01T12:00:00"
    )
    # No correction requested → no patch.
    w.patch_attribute_value.assert_not_called()


def test_bare_date_promoted_to_noon():
    w = _writer(open_value="campaign")
    r = set_continuity(w, station_id="VOTT", from_date="2017-06-01", value="continuous")
    assert r.date == "2017-06-01T12:00:00"


def test_full_datetime_preserved():
    w = _writer(open_value="campaign")
    r = set_continuity(
        w, station_id="VOTT", from_date="2017-06-01T09:30:00", value="continuous"
    )
    assert r.date == "2017-06-01T09:30:00"


def test_correct_current_relabels_then_transitions():
    # VOTT case: open period wrongly 'continuous' since 2012; relabel to campaign.
    w = _writer(open_value="continuous")
    r = set_continuity(
        w,
        station_id="VOTT",
        from_date="2017-06-01",
        value="continuous",
        correct_current="campaign",
    )
    # Open period value PATCHed to 'campaign' first...
    w.patch_attribute_value.assert_called_once_with(8801, value="campaign")
    # ...then the transition closes it at the date and opens 'continuous'.
    w.transition_attribute_value.assert_called_once_with(
        VOTT_EID, "continuity", "continuous", "2017-06-01T12:00:00"
    )
    assert "corrected_current" in r.tos_changes


def test_correct_current_noop_when_value_already_right():
    # Open period already 'campaign' → no PATCH, just transition.
    w = _writer(open_value="campaign")
    set_continuity(
        w,
        station_id="VOTT",
        from_date="2017-06-01",
        value="continuous",
        correct_current="campaign",
    )
    w.patch_attribute_value.assert_not_called()


def test_invalid_value_raises():
    w = _writer()
    with pytest.raises(CfgOperationError, match="continuity value"):
        set_continuity(w, station_id="VOTT", from_date="2017-06-01", value="bogus")


def test_invalid_correct_current_raises():
    w = _writer()
    with pytest.raises(CfgOperationError, match="correct-current"):
        set_continuity(
            w,
            station_id="VOTT",
            from_date="2017-06-01",
            value="continuous",
            correct_current="bogus",
        )
