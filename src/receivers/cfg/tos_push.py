"""Helpers for pushing field values from cfg/receiver into TOS.

Used by the ``receivers cfg reconcile --push-tos`` flow. All writes go through
:class:`tostools.api.tos_writer.TOSWriter` which enforces ``dry_run`` by default.

Entity ID resolution
--------------------
TOS uses ``id_entity`` as the primary key for every entity (stations, devices).
The station entity ID is now propagated through ``TOSClient.get_complete_station_metadata``
as ``tos_data["id_entity"]``.  Device entity IDs (gnss_receiver, antenna, radome)
require an extra API call: we fetch the station's entity history, inspect
``children_connections``, and match by ``code_entity_subtype``.

Push guardrails (CLI-layer, not here)
--------------------------------------
Whether a push is appropriate for a given diff is a policy decision made in
``receivers.cli.cfg`` before calling these helpers:

* Only ``spec.receiver_authoritative`` fields can be auto-pushed from receiver
  values without extra confirmation.
* Position fields (lat/lon/altitude) should never be pushed from PVT estimates.
* ``spec.tos_writable`` must be True (``tos_attribute_code`` must be set).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from tostools.api.tos_writer import DryRunResult, TOSWriter

from .field_manifest import FieldSpec

logger = logging.getLogger(__name__)


def resolve_entity_id(
    writer: TOSWriter,
    station_entity_id: int,
    target_entity: str,
) -> Optional[int]:
    """Return the TOS ``id_entity`` for *target_entity* connected to the station.

    Args:
        writer: Authenticated :class:`TOSWriter` instance.
        station_entity_id: Entity ID of the geophysical station.
        target_entity: One of ``"station"``, ``"gnss_receiver"``, ``"antenna"``,
            ``"radome"``.

    Returns:
        The entity ID, or ``None`` if the target could not be resolved.
    """
    if target_entity == "station":
        return station_entity_id

    history = writer.get_entity_history(station_entity_id)
    if not history:
        logger.warning(
            "resolve_entity_id: no history for station %d", station_entity_id
        )
        return None

    for conn in history.get("children_connections", []):
        if conn.get("time_to") is not None:
            continue  # closed session — skip
        child_id = conn.get("id_entity_child")
        if child_id is None:
            continue
        child_history = writer.get_entity_history(child_id)
        if child_history and child_history.get("code_entity_subtype") == target_entity:
            return child_id

    logger.warning(
        "resolve_entity_id: no open %s connection found for station %d",
        target_entity,
        station_entity_id,
    )
    return None


def push_field_to_tos(
    writer: TOSWriter,
    spec: FieldSpec,
    value: str,
    tos_data: Dict[str, Any],
    date_from: str,
) -> DryRunResult | Any:
    """Push a reconciled field value to TOS via :meth:`TOSWriter.upsert_attribute_value`.

    Args:
        writer: Authenticated :class:`TOSWriter` (dry_run propagates from instance).
        spec: The field specification — must have ``tos_writable == True``.
        value: The value to write (in cfg/receiver vocabulary; ``tos_format`` is
            applied before the write).
        tos_data: Processed station dict from ``TOSClient.get_complete_station_metadata``.
            Must contain ``id_entity`` (available since tostools ≥ 0.3.1).
        date_from: ISO-8601 timestamp for the start of validity. For device
            attributes (firmware, serial) this should be the actual change date
            if known; default to *now* only with an operator warning.

    Returns:
        The response from :meth:`TOSWriter.upsert_attribute_value` — either the
        API response dict, ``None`` (no change needed), or a
        :class:`~tostools.api.tos_writer.DryRunResult`.

    Raises:
        ValueError: If ``spec`` is not TOS-writable or ``tos_data`` has no ``id_entity``.
        RuntimeError: If the target entity ID cannot be resolved.
    """
    if not spec.tos_writable:
        raise ValueError(
            f"push_field_to_tos: {spec.cfg_key!r} has no tos_attribute_code — not writable"
        )
    # After tos_writable check, both fields are non-None; assert to satisfy type narrowing.
    attribute_code: str = spec.tos_attribute_code  # type: ignore[assignment]
    target_entity_type: str = spec.tos_target_entity  # type: ignore[assignment]

    station_entity_id = tos_data.get("id_entity")
    if station_entity_id is None:
        raise ValueError(
            "push_field_to_tos: tos_data has no 'id_entity' — "
            "upgrade tostools to ≥ 0.3.1 which propagates the station entity ID"
        )

    target_entity_id = resolve_entity_id(writer, station_entity_id, target_entity_type)
    if target_entity_id is None:
        raise RuntimeError(
            f"push_field_to_tos: could not resolve entity ID for "
            f"{target_entity_type!r} on station entity {station_entity_id}"
        )

    tos_value = spec.tos_format(value)
    if tos_value is None:
        raise ValueError(
            f"push_field_to_tos: tos_format returned None for {spec.cfg_key!r}={value!r}"
        )

    logger.info(
        "push_field_to_tos: %s → TOS entity %d attr %r = %r (date_from=%s)",
        spec.cfg_key,
        target_entity_id,
        attribute_code,
        tos_value,
        date_from,
    )

    return writer.upsert_attribute_value(
        id_entity=target_entity_id,
        code=attribute_code,
        value=tos_value,
        date_from=date_from,
    )


def push_component_to_tos(
    writer: TOSWriter,
    entity: str,
    attribute_code: str,
    value: str,
    tos_data: Dict[str, Any],
    date_from: str,
) -> DryRunResult | Any:
    """Push one component of a composite field to TOS.

    Used for antenna_height/east/north where the cfg value is the sum of
    two separate TOS attributes (antenna + monument).  Each component is
    pushed independently.

    Args:
        writer: Authenticated :class:`TOSWriter` instance.
        entity: TOS entity subtype, e.g. ``"antenna"`` or ``"monument"``.
        attribute_code: TOS attribute code, e.g. ``"antenna_height"``.
        value: The raw value string to write (no tos_format applied — caller
            is responsible for correct formatting).
        tos_data: Processed station dict containing ``id_entity``.
        date_from: ISO-8601 timestamp for attribute validity start.
    """
    station_entity_id = tos_data.get("id_entity")
    if station_entity_id is None:
        raise ValueError("push_component_to_tos: tos_data has no 'id_entity'")

    target_entity_id = resolve_entity_id(writer, station_entity_id, entity)
    if target_entity_id is None:
        raise RuntimeError(
            f"push_component_to_tos: could not resolve entity ID for {entity!r}"
        )

    logger.info(
        "push_component_to_tos: %s.%s → TOS entity %d = %r (date_from=%s)",
        entity,
        attribute_code,
        target_entity_id,
        value,
        date_from,
    )

    return writer.upsert_attribute_value(
        id_entity=target_entity_id,
        code=attribute_code,
        value=value,
        date_from=date_from,
    )
