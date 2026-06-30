"""Tests for ``receivers cfg add-monument`` — the monument TOS-intake verb.

Mirrors test_add_antenna: the tostools builder + the
:func:`receivers.cfg.operations.add_monument` orchestration against a mocked
``TOSWriter`` (no network). Monuments carry the ``antenna_height`` offset, have
no model, and default to a synthetic ``monument-<STID>-<YYYYMMDD>`` serial.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tostools.device import build_monument_attributes, synthetic_serial

from receivers.cfg.operations import CfgOperationError, add_monument

VOTT_EID = 21559


def _writer(station_dict=None, child_subtype=None):
    w = MagicMock()
    w.dry_run = True
    w.find_station_by_marker.return_value = VOTT_EID
    station_dict = (
        station_dict
        if station_dict is not None
        else {
            "children_connections": [],
            "attributes": [
                {"code": "date_start", "value": "2026-05-01T00:00:00", "date_to": None}
            ],
        }
    )

    def _hist(eid):
        if int(eid) == VOTT_EID:
            return station_dict
        return {"code_entity_subtype": child_subtype}

    w.get_entity_history.side_effect = _hist
    w.create_device.return_value = {"id_entity": 50010}
    w.create_entity_connection.return_value = {"id_connection": 9}
    return w


def test_build_monument_attributes_shape():
    """Monument shape matches fleet data: no model, no status."""
    attrs = build_monument_attributes("s", "owner", "2026-05-01", "0.0")
    codes = {a["code"] for a in attrs}
    # monument-scoped height code (antenna_height is antenna-scoped → TOS 400)
    assert codes == {"serial_number", "owner", "date_start", "monument_height"}
    assert "model" not in codes and "status" not in codes
    assert "antenna_height" not in codes
    assert (
        synthetic_serial("monument", "VOTT", "2026-05-01") == "monument-VOTT-20260501"
    )


def test_add_monument_synthetic_serial_and_join():
    w = _writer()
    r = add_monument(
        w, station_id="VOTT", height="0.0", date_start="2026-05-01T00:00:00"
    )
    assert r.operation == "add-monument"
    assert r.serial == "monument-VOTT-20260501"
    assert r.tos_changes["synthetic_serial"] is True
    assert w.create_device.call_args.args[0] == "monument"
    w.create_entity_connection.assert_called_once_with(
        VOTT_EID, 50010, "2026-05-01T00:00:00"
    )
    # provenance comment auto-added for synthetic serial
    assert any(a["code"] == "comment" for a in r.tos_changes["monument_attributes"])


def test_add_monument_height_attribute():
    w = _writer()
    r = add_monument(
        w, station_id="VOTT", height="0.0123", date_start="2026-05-01T00:00:00"
    )
    h = [
        a
        for a in r.tos_changes["monument_attributes"]
        if a["code"] == "monument_height"
    ]
    assert h and h[0]["value"] == "0.0123"


def test_add_monument_explicit_serial_not_synthetic():
    w = _writer()
    r = add_monument(
        w, station_id="VOTT", serial="MON123", date_start="2026-05-01T00:00:00"
    )
    assert r.serial == "MON123"
    assert r.tos_changes["synthetic_serial"] is False


def test_add_monument_guards_existing_open_monument():
    w = _writer(
        station_dict={
            "children_connections": [{"id_entity_child": 7, "time_to": None}],
            "attributes": [],
        },
        child_subtype="monument",
    )
    with pytest.raises(CfgOperationError, match="already has an open monument"):
        add_monument(w, station_id="VOTT", date_start="2026-05-01T00:00:00")
    w.create_device.assert_not_called()


def test_add_monument_bare_date_promoted_to_noon():
    w = _writer()
    r = add_monument(w, station_id="VOTT", date_start="2026-05-01")
    assert r.date == "2026-05-01T12:00:00"


def test_add_monument_defaults_date_to_station_date_start():
    w = _writer()
    r = add_monument(w, station_id="VOTT")
    assert r.date == "2026-05-01T00:00:00"
    assert r.serial == "monument-VOTT-20260501"


def test_audit_log_op_creations_writes_model(tmp_path):
    """--commit path: _audit_log_op_creations logs each created device (with its
    model) to additions/device_additions.jsonl. Mirrors add-receiver --commit."""
    import json as _json
    from unittest.mock import patch

    from tostools import tos as tosmod

    from receivers.cli.cfg import _audit_log_op_creations

    repo = tmp_path / "corrections"
    repo.mkdir()

    class _R:
        tos_changes = {
            "monument_create": {"id_entity": 21609},
            "monument_attributes": [
                {"code": "serial_number", "value": "monument-HAFC-20210309"},
                {"code": "model", "value": "GPS stál-fjórfótur"},
                {"code": "date_start", "value": "2021-03-09T12:00:00"},
            ],
        }

    with (
        patch.object(tosmod, "_git_commit_file", return_value=False),
        patch("tostools.archive.tos_corrections_dir", return_value=repo),
    ):
        _audit_log_op_creations(_R(), source="receivers cfg add-monument", note="HAFC")

    rec = _json.loads(
        (repo / "additions" / "device_additions.jsonl").read_text().strip()
    )
    assert rec["subtype"] == "monument"
    assert rec["model"] == "GPS stál-fjórfótur"
    assert rec["id_entity"] == 21609
    assert rec["source"] == "receivers cfg add-monument"
