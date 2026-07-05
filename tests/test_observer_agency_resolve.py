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


# ---------------------------------------------------------------------------
# _owner_org_at — date-scoped owner (per_time_from/per_time_to). A station that
# changed hands must resolve the operator responsible AT the observation date.
# ---------------------------------------------------------------------------

from datetime import datetime  # noqa: E402

from receivers.dissemination.tos_access import _owner_org_at  # noqa: E402


def _owner(org, tf, tt=None):
    return {
        "role_is": "Eigandi stöðvar",
        "organization": org,
        "per_time_from": tf,
        "per_time_to": tt,
    }


def test_owner_single_open_period():
    c = [_owner("Veðurstofa Íslands", "2001-07-19T00:00:00")]
    assert _owner_org_at(c, datetime(2015, 4, 1)) == "Veðurstofa Íslands"


def test_owner_changed_hands_is_date_scoped():
    c = [
        _owner("Landmælingar Íslands", "2005-01-01T00:00:00", "2020-06-01T00:00:00"),
        _owner("NATT", "2020-06-01T00:00:00"),
    ]
    assert _owner_org_at(c, datetime(2010, 1, 1)) == "Landmælingar Íslands"
    assert _owner_org_at(c, datetime(2022, 1, 1)) == "NATT"
    # handover day → new owner (per_time_to is exclusive)
    assert _owner_org_at(c, datetime(2020, 6, 1)) == "NATT"


def test_owner_before_first_period_falls_back_to_recent():
    c = [_owner("NATT", "2020-06-01T00:00:00")]
    assert _owner_org_at(c, datetime(2010, 1, 1)) == "NATT"


def test_no_owner_contact_returns_empty():
    c = [{"role_is": "Rekstraraðili stöðvar", "organization": "Op"}]
    assert _owner_org_at(c, datetime(2015, 1, 1)) == ""


def test_operator_role_ignored_for_owner():
    c = [
        _owner("Owner Org", "2001-01-01T00:00:00"),
        {"role_is": "Rekstraraðili stöðvar", "organization": "Operator Org"},
    ]
    assert _owner_org_at(c, datetime(2015, 1, 1)) == "Owner Org"
