"""Tests for ``receivers cfg import-campaigns`` — station.info → TOS importer.

Exercises :func:`receivers.cfg.operations.import_campaigns` against a mocked
``TOSWriter`` (no network), using real-format VOTT campaign lines. Verifies
closed-session creation, synthetic serials, idempotent skip, the metadata-only
constraint, and optional monuments.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from receivers.cfg.operations import CfgOperationError, import_campaigns

VOTT_EID = 21559

# Real-format VOTT lines (cols verbatim from station.info.sopac.apr05).
VOTT_LINES = """\
*SITE  Station Name      Session Start      Session Stop       Ant Ht   HtCod  Ant N    Ant E    Receiver Type         Vers                  SwVer  Receiver SN           Antenna Type     Dome   Antenna SN
 VOTT  Vottur            2012 155 00 00 00  2012 165 00 00 00   0.0000  DHARP   0.0000  -0.0004  TRIMBLE 5700          2.01                   2.01  0220331856            TRM41249.00      NONE   60004115
 VOTT  Vottur            2014 150 00 00 00  2014 160 00 00 00   0.0000  DHARP   0.0000   0.0000  TRIMBLE NETR9         NP 4.62 / SP 4.62      4.62  5229K50746            TRM57971.00      NONE   5000117697
 VOTT  Vottur            2016 150 00 00 00  2016 170 00 00 00   0.0000  DHARP   0.0000   0.0000  TRIMBLE 5700          2.30                   2.30  0224093032            TRM41249.00      NONE   0000000000
"""


@pytest.fixture
def station_info(tmp_path):
    p = tmp_path / "station.info.sopac.apr05"
    p.write_text(VOTT_LINES, encoding="utf-8")
    return p


def _writer(children=None):
    """Mocked TOSWriter: fresh station, every device new."""
    w = MagicMock()
    w.dry_run = True
    w.find_station_by_marker.return_value = VOTT_EID
    station_dict = {"children_connections": children or [], "attributes": []}

    def _hist(eid):
        if int(eid) == VOTT_EID:
            return station_dict
        return {}

    w.get_entity_history.side_effect = _hist
    w.find_device_by_serial.return_value = None  # all devices new
    # Hand back a distinct id per created device so joins are exercised.
    ids = iter(range(50000, 50100))
    w.create_device.side_effect = lambda *a, **k: {"id_entity": next(ids)}
    w.create_entity_connection.return_value = {}
    return w


def test_imports_all_occupations_with_closed_joins(station_info):
    w = _writer()
    r = import_campaigns(w, station_id="VOTT", station_info_path=station_info)
    assert r.operation == "import-campaigns"
    summary = r.tos_changes["summary"]
    assert summary == {"total": 3, "created": 3, "skipped": 0}

    # Receiver + antenna created per occupation (3 × 2 = 6 devices, no radome/mon).
    subtypes = [c.args[0] for c in w.create_device.call_args_list]
    assert subtypes.count("gnss_receiver") == 3
    assert subtypes.count("antenna") == 3
    assert "monument" not in subtypes
    assert "radome" not in subtypes  # all domes are NONE

    # Every join is CLOSED — both time_from and time_to passed.
    for call in w.create_entity_connection.call_args_list:
        parent, child, time_from, time_to = call.args
        assert parent == VOTT_EID
        assert time_from and time_to  # closed period
    # First occupation's receiver join uses the exact occupation window.
    first = w.create_entity_connection.call_args_list[0].args
    assert first[2] == "2012-06-03T00:00:00"
    assert first[3] == "2012-06-13T00:00:00"


def test_metadata_only_no_cfg_writes(station_info):
    w = _writer()
    r = import_campaigns(w, station_id="VOTT", station_info_path=station_info)
    assert r.cfg_changes == {}  # campaigns never touch stations.cfg


def test_synthetic_antenna_serial_when_unknown(station_info):
    w = _writer()
    r = import_campaigns(w, station_id="VOTT", station_info_path=station_info)
    occs = r.tos_changes["occupations"]
    # 2016 occupation had antenna SN 0000000000 → normalised → synthetic.
    occ_2016 = next(o for o in occs if o["time_from"].startswith("2016"))
    assert occ_2016["antenna"]["synthetic_serial"] is True
    assert occ_2016["antenna"]["serial"].startswith("antenna-VOTT-")
    # 2012 occupation had a real antenna serial.
    occ_2012 = next(o for o in occs if o["time_from"].startswith("2012"))
    assert occ_2012["antenna"]["synthetic_serial"] is False
    assert occ_2012["antenna"]["serial"] == "60004115"


def test_receiver_firmware_attribute_carried(station_info):
    w = _writer()
    import_campaigns(w, station_id="VOTT", station_info_path=station_info)
    # The gnss_receiver attribute list includes firmware_version from "Vers".
    rx_calls = [
        c for c in w.create_device.call_args_list if c.args[0] == "gnss_receiver"
    ]
    attrs = rx_calls[0].args[1]
    fw = [a for a in attrs if a["code"] == "firmware_version"]
    assert fw and fw[0]["value"] == "2.01"
    # Device attributes stay OPEN — only the station↔device join is closed.
    assert fw[0]["date_to"] is None


def test_device_attributes_stay_open(station_info):
    """Identity attrs (serial/model/owner/status) are intrinsic → date_to=None.

    Only the join carries the occupation window. Closing identity attrs would
    wrongly assert the unit stopped having that serial, and could break the
    serial-based idempotency re-lookup.
    """
    w = _writer()
    import_campaigns(w, station_id="VOTT", station_info_path=station_info)
    for call in w.create_device.call_args_list:
        for attr in call.args[1]:
            assert attr["date_to"] is None, attr


def test_with_monument_creates_monument_per_occupation(station_info):
    w = _writer()
    r = import_campaigns(
        w, station_id="VOTT", station_info_path=station_info, with_monument=True
    )
    subtypes = [c.args[0] for c in w.create_device.call_args_list]
    assert subtypes.count("monument") == 3
    occ = r.tos_changes["occupations"][0]
    assert "monument" in occ


def test_idempotent_skip_existing_session(station_info):
    # Station already has the 2012 receiver session (serial + start date match).
    children = [{"id_entity_child": 70001, "time_from": "2012-06-03T00:00:00"}]
    w = _writer(children=children)

    def _hist(eid):
        if int(eid) == VOTT_EID:
            return {"children_connections": children, "attributes": []}
        if int(eid) == 70001:
            return {
                "code_entity_subtype": "gnss_receiver",
                "attributes": [
                    {
                        "code": "serial_number",
                        "value": "0220331856",
                        "date_to": None,
                    }
                ],
            }
        return {}

    w.get_entity_history.side_effect = _hist
    r = import_campaigns(w, station_id="VOTT", station_info_path=station_info)
    summary = r.tos_changes["summary"]
    assert summary["skipped"] == 1
    assert summary["created"] == 2
    skipped = [
        o for o in r.tos_changes["occupations"] if o["status"].startswith("skip")
    ]
    assert skipped[0]["time_from"].startswith("2012")


def test_force_reimports_existing(station_info):
    children = [{"id_entity_child": 70001, "time_from": "2012-06-03T00:00:00"}]
    w = _writer(children=children)

    def _hist(eid):
        if int(eid) == VOTT_EID:
            return {"children_connections": children, "attributes": []}
        if int(eid) == 70001:
            return {
                "code_entity_subtype": "gnss_receiver",
                "attributes": [
                    {"code": "serial_number", "value": "0220331856", "date_to": None}
                ],
            }
        return {}

    w.get_entity_history.side_effect = _hist
    r = import_campaigns(
        w, station_id="VOTT", station_info_path=station_info, force=True
    )
    assert r.tos_changes["summary"]["skipped"] == 0
    assert r.tos_changes["summary"]["created"] == 3


def test_no_occupations_raises(tmp_path):
    p = tmp_path / "empty.info"
    p.write_text("*SITE header only\n", encoding="utf-8")
    w = _writer()
    with pytest.raises(CfgOperationError, match="No station.info occupations"):
        import_campaigns(w, station_id="VOTT", station_info_path=p)
