"""Observer/agency resolution attached to the TOS session (offline).

The receivers session providers resolve the station's TOS owner org → RINEX
OBSERVER / AGENCY strings via agencies.yaml, so the shared validator can check
that field. No network — the AgencyResolver is injected.
"""

from __future__ import annotations

from receivers.dissemination import tos_access as ta
from receivers.dissemination.agencies import AgencyResolver

_YAML = {
    "agencies": {
        "Veðurstofa Íslands": {
            "observer": "GNSSatIMO",
            "agency_label": "Vedurstofa Islands",
        }
    },
    "defaults": {"operator_agency": "Veðurstofa Íslands"},
}


def test_resolve_known_org(monkeypatch):
    monkeypatch.setattr(ta, "_AGENCY_RESOLVER", AgencyResolver.from_dict(_YAML))
    assert ta._resolve_observer_agency("Veðurstofa Íslands") == (
        "GNSSatIMO",
        "Vedurstofa Islands",
    )


def test_resolve_unknown_org_falls_back_to_operator_default(monkeypatch):
    monkeypatch.setattr(ta, "_AGENCY_RESOLVER", AgencyResolver.from_dict(_YAML))
    assert ta._resolve_observer_agency("Some Other Org") == (
        "GNSSatIMO",
        "Vedurstofa Islands",
    )


def test_resolve_empty_when_no_agencies_yaml(monkeypatch):
    # Empty resolver (no agencies.yaml deployed) → ("", "") → validator skips.
    monkeypatch.setattr(ta, "_AGENCY_RESOLVER", AgencyResolver({}, {}))
    assert ta._resolve_observer_agency("anything") == ("", "")


def test_resolve_never_raises(monkeypatch):
    class Boom:
        def resolve(self, org):
            raise RuntimeError("boom")

    monkeypatch.setattr(ta, "_AGENCY_RESOLVER", Boom())
    assert ta._resolve_observer_agency("x") == ("", "")
