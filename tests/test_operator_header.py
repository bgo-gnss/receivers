"""OBSERVER / AGENCY resolved from station_operator via agencies.yaml (offline).

The single de-hardcoded source for the RINEX header OBSERVER / AGENCY: the cfg
``station_operator`` code → the operating agency's GNSSat* observer + English
agency name. Never the retired personal-initial rinex_observer/rinex_agency.
"""

from __future__ import annotations

from receivers import config_utils as cu
from receivers.dissemination.agencies import AgencyResolver

_YAML = {
    "agencies": {
        "Veðurstofa Íslands": {
            "english_name": "Icelandic Meteorological Office",
            "abbrev": "IMO",
            "abbrev_is": "VÍ",
            "observer": "GNSSatIMO",
            "agency_label": "Vedurstofa Islands",
        },
        "Landmælingar Íslands": {
            "english_name": "Natural Science Institute of Iceland",
            "abbrev": "NSII",
            "abbrev_is": "NATT",
            "observer": "GNSSatNATT",
            "agency_label": "NATT",
        },
        "Jarðvísindastofnun": {  # english_name > 40 chars → short fallback
            "english_name": "Institute of Earth Sciences, University of Iceland",
            "abbrev": "IES",
            "observer": "GNSSatIES",
            "agency_label": "IES",
        },
    },
    "defaults": {"operator_agency": "Veðurstofa Íslands"},
}


def test_rinex_agency_prefers_full_english_name_within_40():
    r = AgencyResolver.from_dict(_YAML)
    assert r.resolve_by_code("IMO").rinex_agency == "Icelandic Meteorological Office"
    assert (
        r.resolve_by_code("NATT").rinex_agency == "Natural Science Institute of Iceland"
    )
    # IES english_name is 50 chars → falls back to the short agency_label
    assert r.resolve_by_code("IES").rinex_agency == "IES"


def test_resolve_by_code_english_and_icelandic_abbrev():
    r = AgencyResolver.from_dict(_YAML)
    assert r.resolve_by_code("IMO").observer == "GNSSatIMO"  # English abbrev
    assert r.resolve_by_code("natt").observer == "GNSSatNATT"  # Icelandic, case-insens
    assert r.resolve_by_code("KAUST") is None  # foreign owner, not an operator
    assert r.resolve_by_code(None) is None


def test_operator_header_known_codes(monkeypatch):
    monkeypatch.setattr(cu, "_AGENCY_RESOLVER", AgencyResolver.from_dict(_YAML))
    assert cu._resolve_operator_header("IMO") == (
        "GNSSatIMO",
        "Icelandic Meteorological Office",
    )
    assert cu._resolve_operator_header("NATT") == (
        "GNSSatNATT",
        "Natural Science Institute of Iceland",
    )


def test_operator_header_unknown_or_missing_falls_back_to_imo(monkeypatch):
    # KAUST/PSU/UI foreign-owned stations are IMO-operated → GNSSatIMO.
    monkeypatch.setattr(cu, "_AGENCY_RESOLVER", AgencyResolver.from_dict(_YAML))
    assert cu._resolve_operator_header("KAUST") == (
        "GNSSatIMO",
        "Icelandic Meteorological Office",
    )
    assert cu._resolve_operator_header(None) == (
        "GNSSatIMO",
        "Icelandic Meteorological Office",
    )


def test_operator_header_never_raises_no_config(monkeypatch):
    monkeypatch.setattr(cu, "_AGENCY_RESOLVER", AgencyResolver({}, {}))
    # empty resolver → hardcoded IMO fallback, never an exception
    assert cu._resolve_operator_header("IMO") == (
        "GNSSatIMO",
        "Icelandic Meteorological Office",
    )
