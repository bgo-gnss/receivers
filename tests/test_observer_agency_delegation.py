"""observer/agency come from agencies.yaml, not sync.yaml — and the RINEX AGENCY
is the full ENGLISH institutional name (EPOS wants English), falling back to the
short form only when the English name overflows the 40-char field.
"""

from receivers.dissemination.agencies import AgencyInfo, AgencyResolver
from receivers.dissemination.config import DisseminationTarget
from receivers.dissemination.engine import EposDisseminate

_IMO = AgencyInfo(
    org="Veðurstofa Íslands",
    english_name="Icelandic Meteorological Office",  # 31 chars → fits
    observer="GNSSatIMO",
    agency_label="Vedurstofa Islands",
    abbrev="IMO",
)
_IES = AgencyInfo(
    org="Jarðvísindastofnun",
    english_name="Institute of Earth Sciences, University of Iceland",  # 50 → overflow
    observer="GNSSatIES",
    agency_label="IES",
    abbrev="IES",
)


def _resolver():
    return AgencyResolver(
        {_IMO.org: _IMO, _IES.org: _IES},
        {"operator_agency": "Veðurstofa Íslands"},
    )


def test_rinex_agency_prefers_english_name():
    assert _IMO.rinex_agency == "Icelandic Meteorological Office"


def test_rinex_agency_falls_back_when_over_40():
    assert _IES.rinex_agency == "IES"  # 50-char English name → short agency_label


def test_default_agency_is_the_imo_entity():
    d = _resolver().default_agency()
    assert d is not None and d.english_name == "Icelandic Meteorological Office"


def _engine():
    tgt = DisseminationTarget(
        name="epos",
        active=False,
        host="epos-portal.vedur.is",
        user="epos",
        dest="/mnt/epos_01/gps",
        source_root="/mnt_data/rawgpsdata",
        sessions=("15s_24hr",),
        exclude_stations=frozenset(),
    )
    return EposDisseminate(tgt, agency_resolver=_resolver())


def test_known_owner_gets_english_agency():
    assert _engine()._resolve_observer_agency({"owner_org": "Veðurstofa Íslands"}) == (
        "GNSSatIMO",
        "Icelandic Meteorological Office",
    )


def test_owner_with_long_english_name_gets_short_form():
    assert _engine()._resolve_observer_agency({"owner_org": "Jarðvísindastofnun"}) == (
        "GNSSatIES",
        "IES",
    )


def test_unknown_owner_falls_back_to_imo_english():
    assert _engine()._resolve_observer_agency({"owner_org": "Nope Ltd"}) == (
        "GNSSatIMO",
        "Icelandic Meteorological Office",
    )


def test_no_session_falls_back_to_imo_english():
    assert _engine()._resolve_observer_agency(None) == (
        "GNSSatIMO",
        "Icelandic Meteorological Office",
    )
