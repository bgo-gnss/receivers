"""Tests for ``receivers cfg add-station`` — TOS → stations.cfg scaffolder.

Exercises :func:`receivers.cfg.operations.add_station` against a mocked
``TOSClient`` (no network). Verifies field mapping (reusing tos_adapter),
telemetry extraction from the SIM/modem, type-defaulted ports, data-quality
warnings, the no-open-session guard, and an actual section write.
"""

from __future__ import annotations

import configparser
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from receivers.cfg.operations import CfgOperationError, add_station

VOTT_EID = 21559
SIM_EID = 21565
MODEM_EID = 18213


def _station_dict():
    return {
        "id_entity": VOTT_EID,
        "name": "Vöttur",
        "lat": 64.2714986,
        "lon": -17.1809165,
        "altitude": 1114.01,
        "device_history": [
            {
                # TOS hands time_from back as a datetime (not a string).
                "time_from": datetime(2026, 5, 1, 0, 0, 0),
                "time_to": None,
                "gnss_receiver": {
                    "model": "TRIMBLE NETRS",
                    "serial_number": "4729135857",
                },
                "antenna": {
                    "model": "AS-ANT3BCAL",
                    "serial_number": "antenna-VOTT-20260501",
                    "antenna_height": 0.0,
                },
                "monument": {
                    "serial_number": "monument-VOTT-20120603",
                    "monument_height": 0.0,
                },
            }
        ],
    }


def _client(station=None, with_telemetry=True):
    c = MagicMock()
    c.get_complete_station_metadata.return_value = (
        station if station is not None else _station_dict()
    )

    children = []
    if with_telemetry:
        children = [
            {"id_entity_child": SIM_EID, "time_to": None},
            {"id_entity_child": MODEM_EID, "time_to": None},
        ]

    def _hist(eid):
        eid = int(eid)
        if eid == VOTT_EID:
            return {"children_connections": children, "attributes": []}
        if eid == SIM_EID:
            return {
                "code_entity_subtype": "sim_card",
                "attributes": [
                    {"code": "ip_address", "value": "10.4.2.163", "date_to": None}
                ],
            }
        if eid == MODEM_EID:
            return {
                "code_entity_subtype": "modem_gsm",
                "attributes": [
                    {"code": "model", "value": "Teltonika RUT240", "date_to": None}
                ],
            }
        return {}

    c.get_entity_history.side_effect = _hist
    return c


def test_field_mapping_from_tos():
    r = add_station(_client(), station_id="VOTT")
    f = r.cfg_changes
    assert f["station_id"] == "VOTT"
    assert f["station_name"] == "Vöttur"
    assert f["receiver_type"] == "NetRS"  # TRIMBLE NETRS → canonical short
    assert f["receiver_serial"] == "4729135857"
    assert f["antenna_type"] == "AS-ANT3BCAL"
    assert f["antenna_serial"] == "antenna-VOTT-20260501"
    assert f["antenna_radome"] == "NONE"  # no radome entity → NONE
    assert f["antenna_height"] == "0.0000"  # composite antenna+monument
    # Position is 6-dp formatted by tos_adapter (matches stations.cfg convention).
    assert f["latitude"] == "64.271499"
    assert f["longitude"] == "-17.180916"
    assert f["height"] == "1114.01"
    assert f["rinex_marker_name"] == "VOTT"
    assert f["rinex_config_valid_from"] == "2026-05-01"


def test_telemetry_from_sim_and_modem():
    r = add_station(_client(), station_id="VOTT")
    assert r.cfg_changes["router_ip"] == "10.4.2.163"  # SIM ip_address
    assert r.cfg_changes["router_type"] == "RUT240"  # modem model, shortened


def test_type_defaulted_ports():
    r = add_station(_client(), station_id="VOTT")
    # NetRS → httpport only (no ftp/control).
    assert r.cfg_changes["receiver_httpport"] == "8060"
    assert "receiver_ftpport" not in r.cfg_changes
    assert "receiver_controlport" not in r.cfg_changes


def test_warns_zero_antenna_height():
    # AS-ANT3BCAL is in ANTENNA_IGS (added earlier), so it does NOT warn as
    # non-IGS; the zero antenna_height placeholder does warn.
    r = add_station(_client(), station_id="VOTT")
    warns = " ".join(r.tos_changes["warnings"])
    assert "antenna_height is 0.0" in warns


def test_warns_non_igs_antenna():
    st = _station_dict()
    st["device_history"][0]["antenna"]["model"] = "BOGUS_ANT_99"
    st["device_history"][0]["antenna"]["antenna_height"] = 1.5  # non-zero
    r = add_station(_client(station=st), station_id="VOTT")
    warns = " ".join(r.tos_changes["warnings"])
    assert "not an IGS" in warns


def test_router_ip_override_skips_sim_lookup():
    r = add_station(_client(), station_id="VOTT", router_ip="10.9.9.9")
    assert r.cfg_changes["router_ip"] == "10.9.9.9"
    assert not any("router_ip could not" in w for w in r.tos_changes["warnings"])


def test_missing_router_ip_warns():
    r = add_station(_client(with_telemetry=False), station_id="VOTT")
    assert "router_ip" not in r.cfg_changes
    assert any("router_ip could not" in w for w in r.tos_changes["warnings"])


def test_no_open_session_raises():
    st = _station_dict()
    st["device_history"][0]["time_to"] = "2020-01-01T00:00:00"  # closed → no open
    with pytest.raises(CfgOperationError, match="no open device session"):
        add_station(_client(station=st), station_id="VOTT")


def test_station_not_in_tos_raises():
    c = MagicMock()
    c.get_complete_station_metadata.return_value = None
    with pytest.raises(CfgOperationError, match="No TOS station"):
        add_station(c, station_id="VOTT")


def test_dry_run_does_not_write(tmp_path):
    cfg = tmp_path / "stations.cfg"
    cfg.write_text("[FEDG]\nreceiver_type = NetRS\n", encoding="utf-8")
    add_station(_client(), station_id="VOTT", cfg_path=cfg, dry_run=True)
    assert "[VOTT]" not in cfg.read_text()


def test_live_write_appends_section(tmp_path):
    cfg = tmp_path / "stations.cfg"
    cfg.write_text("[FEDG]\nreceiver_type = NetRS\n", encoding="utf-8")
    r = add_station(_client(), station_id="VOTT", cfg_path=cfg, dry_run=False)
    assert r.dry_run is False
    parsed = configparser.ConfigParser()
    parsed.read(cfg)
    assert parsed.has_section("VOTT")
    assert parsed["VOTT"]["receiver_serial"] == "4729135857"
    assert parsed["VOTT"]["router_ip"] == "10.4.2.163"
    # Existing section untouched.
    assert parsed.has_section("FEDG")


def test_live_write_refuses_existing_section(tmp_path):
    cfg = tmp_path / "stations.cfg"
    cfg.write_text("[VOTT]\nreceiver_type = NetRS\n", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        add_station(_client(), station_id="VOTT", cfg_path=cfg, dry_run=False)
