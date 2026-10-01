"""`receivers cfg` must resolve a marker to a GPS station, never another one.

Markers are **not** unique in TOS. Marker ``soho`` carries two
``geophysical`` entities: 5356 (``DOAS``, a volcanic-gas station at
Sólheimaheiði) and 4416 (``GPS stöð``, receiver 3075357). ``TOSWriter``'s
``find_station_by_marker`` searches the geophysical domain first and returned
the **first hit**, which is 5356.

That matters here more than anywhere else in the toolchain, because
``_resolve_station`` is the front door to the WRITE verbs: seventeen call
sites including ``install-device``, ``replace-antenna``, ``add-monument``,
``update-device`` and ``reconcile --push-tos``, plus ``move-device --to``
separately. An operator typing SOHO would have read from, and written to, a
gas station. Audited 2026-10-01 against the gps-tos-corrections record: no
such write has landed.

The filter is both of the levels TOS confusingly gives the same name —
``code_entity_subtype == 'geophysical'`` AND the station attribute
``subtype == 'GPS stöð'``. Neither alone works: SIL seismic and DOAS stations
are ``geophysical`` too.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from receivers.cfg.operations import CfgOperationError, _resolve_station


def _entity(eid: int, entity_type: str, subtype: Optional[str]) -> Dict[str, Any]:
    attrs: List[Dict[str, Any]] = []
    if subtype is not None:
        attrs.append({"code": "subtype", "value": subtype, "date_to": None})
    return {
        "id_entity": eid,
        "code_entity_subtype": entity_type,
        "attributes": attrs,
    }


#: The live collision, in the order the geophysical domain returns it.
SOHO_CANDIDATES = [
    _entity(5356, "geophysical", "DOAS"),
    _entity(4416, "geophysical", "GPS stöð"),
]
VLFS_CANDIDATES = [_entity(96, "meteorological", "Úrkomustöð")]


class _FakeWriter:
    """Enough of TOSWriter for `find_station_by_marker` to run for real.

    The production method is exercised, not stubbed — a stub would prove only
    that a kwarg was forwarded, and the thing worth pinning is which entity
    comes back.
    """

    def __init__(self, candidates):
        self._candidates = candidates
        self.marker_predicates: List[Any] = []

    # --- the bits find_station_by_marker touches ---
    def _request(self, method, path, data=None, _force_send=False):
        if path.startswith("/entity/search/station/geophysical"):
            return [
                dict(
                    c,
                    attributes=c["attributes"]
                    + [{"code": "marker", "value": "soho", "date_to": None}],
                )
                for c in self._candidates
                if c["code_entity_subtype"] == "geophysical"
            ]
        if path.startswith("/entity/search/station/meteorological"):
            return [
                dict(
                    c,
                    attributes=c["attributes"]
                    + [{"code": "marker", "value": "vlfs", "date_to": None}],
                )
                for c in self._candidates
                if c["code_entity_subtype"] == "meteorological"
            ]
        if path.startswith("/entity/search/station/"):
            return []
        if path == "/basic_search/":
            return []
        raise AssertionError(f"unexpected request: {method} {path}")

    def get_entity_history(self, eid):
        for c in self._candidates:
            if c["id_entity"] == int(eid):
                return c
        return None

    def find_station_by_marker(self, marker, type_filter="stöð", *, predicate=None):
        from tostools.api.tos_writer import TOSWriter

        self.marker_predicates.append(predicate)
        return TOSWriter.find_station_by_marker(
            self, marker, type_filter, predicate=predicate
        )

    def _find_station_by_marker_via_basic_search(self, needle, type_filter, **kw):
        return None


def test_resolves_the_gps_station_not_the_first_hit():
    """SOHO must reach 4416. 5356 is a gas station with no receiver."""
    writer = _FakeWriter(SOHO_CANDIDATES)
    assert _resolve_station(writer, "SOHO") == 4416


def test_the_unfiltered_lookup_would_have_returned_the_gas_station():
    """Pins WHY the filter is needed, so the fixture cannot quietly stop
    modelling the bug. Without a predicate the same fake yields 5356."""
    writer = _FakeWriter(SOHO_CANDIDATES)
    assert writer.find_station_by_marker("SOHO") == 5356


def test_a_station_of_another_discipline_is_refused():
    """`cfg` acts on GPS stations; a weather station on the same marker is
    not a destination for a GPS receiver."""
    writer = _FakeWriter(VLFS_CANDIDATES)
    with pytest.raises(CfgOperationError) as exc:
        _resolve_station(writer, "VLFS")
    msg = str(exc.value)
    assert "GPS station" in msg
    # The message must point somewhere useful, or an operator will assume
    # the marker is simply absent from TOS.
    assert "tos station show VLFS" in msg


def test_the_resolver_actually_passes_a_predicate():
    """Mutating the call site, not just the helper: a `_resolve_station` that
    forgot the kwarg would still satisfy the tests above if the fake happened
    to order the GPS entity first."""
    writer = _FakeWriter(SOHO_CANDIDATES)
    _resolve_station(writer, "SOHO")
    assert writer.marker_predicates and writer.marker_predicates[0] is not None
