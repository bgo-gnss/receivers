"""Unit tests for tos_push and FieldSpec TOS-write metadata."""

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from receivers.cfg.field_manifest import (
    FIELDS,
    FieldSpec,
    fields_by_key,
    with_position_tolerance,
)
from receivers.cfg.tos_push import push_field_to_tos, resolve_entity_id

# ---------------------------------------------------------------------------
# FieldSpec.tos_writable
# ---------------------------------------------------------------------------


def test_writable_fields_count():
    writable = [f for f in FIELDS if f.tos_writable]
    assert len(writable) == 10, f"Expected 10 writable fields, got {len(writable)}"


def test_non_writable_composite_fields():
    manifest = fields_by_key()
    for key in ("antenna_height", "antenna_east", "antenna_north"):
        assert not manifest[key].tos_writable, f"{key} should not be TOS-writable"


def test_receiver_fields_writable():
    manifest = fields_by_key()
    for key, expected_code, expected_entity in [
        ("receiver_firmware_version", "firmware_version", "gnss_receiver"),
        ("receiver_serial", "serial_number", "gnss_receiver"),
        ("receiver_type", "model", "gnss_receiver"),
    ]:
        spec = manifest[key]
        assert spec.tos_writable, f"{key} should be TOS-writable"
        assert spec.tos_attribute_code == expected_code
        assert spec.tos_target_entity == expected_entity


def test_antenna_fields_writable():
    manifest = fields_by_key()
    assert manifest["antenna_type"].tos_attribute_code == "model"
    assert manifest["antenna_type"].tos_target_entity == "antenna"
    assert manifest["antenna_serial"].tos_attribute_code == "serial_number"
    assert manifest["antenna_radome"].tos_target_entity == "radome"


def test_station_fields_writable():
    manifest = fields_by_key()
    assert manifest["station_name"].tos_attribute_code == "name"
    assert manifest["station_name"].tos_target_entity == "station"
    assert manifest["latitude"].tos_attribute_code == "lat"
    assert manifest["longitude"].tos_attribute_code == "lon"
    assert manifest["height"].tos_attribute_code == "altitude"


def test_with_position_tolerance_preserves_tos_fields():
    specs = with_position_tolerance(5.0)
    by_key = {s.cfg_key: s for s in specs}
    lat = by_key["latitude"]
    assert lat.tos_attribute_code == "lat"
    assert lat.tos_target_entity == "station"
    lon = by_key["longitude"]
    assert lon.tos_attribute_code == "lon"


# ---------------------------------------------------------------------------
# resolve_entity_id
# ---------------------------------------------------------------------------


def _make_writer_with_history(
    station_id: int,
    history: Dict[str, Any],
    children: Optional[Dict[int, Dict[str, Any]]] = None,
) -> MagicMock:
    writer = MagicMock()

    def _get_entity_history(eid: int):
        if eid == station_id:
            return history
        if children and eid in children:
            return children[eid]
        return None

    writer.get_entity_history.side_effect = _get_entity_history
    return writer


def test_resolve_station_entity_returns_station_id():
    writer = MagicMock()
    assert resolve_entity_id(writer, 100, "station") == 100
    writer.get_entity_history.assert_not_called()


def test_resolve_gnss_receiver_entity():
    history = {
        "children_connections": [
            {"time_to": None, "id_entity_child": 200},
            {"time_to": "2024-01-01", "id_entity_child": 999},  # closed — skip
        ]
    }
    children = {
        200: {"code_entity_subtype": "gnss_receiver"},
    }
    writer = _make_writer_with_history(100, history, children)
    result = resolve_entity_id(writer, 100, "gnss_receiver")
    assert result == 200


def test_resolve_returns_none_when_no_open_connection_matches():
    history = {
        "children_connections": [
            {"time_to": None, "id_entity_child": 201},
        ]
    }
    children = {201: {"code_entity_subtype": "antenna"}}  # not gnss_receiver
    writer = _make_writer_with_history(100, history, children)
    result = resolve_entity_id(writer, 100, "gnss_receiver")
    assert result is None


def test_resolve_skips_closed_connections():
    history = {
        "children_connections": [
            {"time_to": "2023-01-01", "id_entity_child": 300},  # closed
        ]
    }
    children = {300: {"code_entity_subtype": "gnss_receiver"}}
    writer = _make_writer_with_history(100, history, children)
    result = resolve_entity_id(writer, 100, "gnss_receiver")
    assert result is None


def test_resolve_returns_none_when_history_missing():
    writer = MagicMock()
    writer.get_entity_history.return_value = None
    result = resolve_entity_id(writer, 100, "gnss_receiver")
    assert result is None


# ---------------------------------------------------------------------------
# push_field_to_tos
# ---------------------------------------------------------------------------


def _spec_for(key: str) -> FieldSpec:
    return fields_by_key()[key]


def _mock_writer(dry_run: bool = True) -> MagicMock:
    writer = MagicMock()
    writer.dry_run = dry_run
    from tostools.api.tos_writer import DryRunResult

    writer.upsert_attribute_value.return_value = DryRunResult(
        method="POST",
        endpoint="/attribute_values",
        payload={"id_entity": 200, "code": "firmware_version", "value": "5.5.0"},
    )
    return writer


def test_push_raises_on_non_writable_field():
    spec = _spec_for("antenna_height")  # not writable
    writer = _mock_writer()
    with pytest.raises(ValueError, match="not writable"):
        push_field_to_tos(writer, spec, "1.5000", {"id_entity": 100}, "2025-01-01T00:00:00")


def test_push_raises_when_no_id_entity():
    spec = _spec_for("receiver_firmware_version")
    writer = _mock_writer()
    with pytest.raises(ValueError, match="id_entity"):
        push_field_to_tos(writer, spec, "5.5.0", {}, "2025-01-01T00:00:00")


def test_push_raises_when_entity_id_cannot_be_resolved():
    spec = _spec_for("receiver_firmware_version")
    writer = _mock_writer()
    writer.get_entity_history.return_value = {"children_connections": []}
    tos_data = {"id_entity": 100}
    with pytest.raises(RuntimeError, match="could not resolve entity ID"):
        push_field_to_tos(writer, spec, "5.5.0", tos_data, "2025-01-01T00:00:00")


def test_push_firmware_version_calls_upsert():
    spec = _spec_for("receiver_firmware_version")
    history = {"children_connections": [{"time_to": None, "id_entity_child": 200}]}
    child = {"code_entity_subtype": "gnss_receiver"}
    writer = _mock_writer()
    writer.get_entity_history.side_effect = (
        lambda eid: history if eid == 100 else child if eid == 200 else None
    )
    tos_data = {"id_entity": 100}
    result = push_field_to_tos(writer, spec, "5.5.0", tos_data, "2025-03-01T00:00:00")
    writer.upsert_attribute_value.assert_called_once_with(
        id_entity=200,
        code="firmware_version",
        value="5.5.0",
        date_from="2025-03-01T00:00:00",
    )
    assert result is not None


def test_push_station_name_uses_station_entity_directly():
    spec = _spec_for("station_name")
    writer = _mock_writer()
    tos_data = {"id_entity": 100}
    push_field_to_tos(writer, spec, "Eldeyjardalur", tos_data, "2025-01-01T00:00:00")
    # station entity — no get_entity_history call needed
    writer.get_entity_history.assert_not_called()
    writer.upsert_attribute_value.assert_called_once_with(
        id_entity=100,
        code="name",
        value="Eldeyjardalur",
        date_from="2025-01-01T00:00:00",
    )
