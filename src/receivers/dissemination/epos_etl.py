"""T5 — TOS → EPOS gnss-europe metadata ETL.

Ports the legacy ``tosToDatabase.py`` into receivers, with three deliberate
improvements (port analysis §6):

* **No global TRUNCATE.** The legacy script ``TRUNCATE item/contact CASCADE`` at
  the start of every run — a crash mid-run leaves EPOS empty. We upsert one
  station at a time inside a transaction, clearing only *that station's* items.
* **Parameterized SQL** (via :mod:`epos_db` helpers) — no string-built statements.
* **Modern pyproj** — ``Transformer`` instead of the removed pyproj-1
  ``transform(+init=EPSG:4326)`` call.

TOS reads go through :class:`tostools.api.tos_client.TOSClient` (so they ride the
``/tos/internal`` URL fix). Station selection is the T3 EPOS filter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .epos_db import get_or_create, insert_row, update_row
from .tos_access import (
    REQUIRED_ATTRIBUTES,
    epos_stations,
    get_attribute_value,
)

logger = logging.getLogger("receivers.dissemination.etl")

# TOS device subtypes carried into EPOS as station items. NB the receiver subtype
# is 'gnss_receiver' in TOS (not 'receiver').
WHITELISTED_ITEMS = ("antenna", "gnss_receiver", "radome", "monument")

# TOS device-attribute code → EPOS controlled-vocabulary attribute name, per TOS
# subtype. Codes not listed here have no EPOS attribute and are dropped (owner,
# comment, date_start, GPS-flag, …). The EPOS `attribute` table is a fixed-id
# vocabulary (1=antenna_type … 26); we resolve the id by name at runtime.
_ATTR_MAP: dict[str, dict[str, str]] = {
    "antenna": {
        "model": "antenna_type",
        "serial_number": "serial_number",
        "antenna_height": "height",
        "antenna_reference_point": "antenna_reference_point",
    },
    "gnss_receiver": {
        "model": "receiver_type",
        "serial_number": "serial_number",
        "firmware_version": "firmware_version",
        "software_version": "software_version",
    },
    "radome": {
        "model": "radome_type",
        "serial_number": "serial_number",
    },
    "monument": {
        "monument_height": "height",
        "serial_number": "serial_number",
    },
}

# EPOS attribute names whose value_numeric must reference a *_type.id — enforced by
# the schema's trg_set_{antenna,receiver,radome}_filter triggers. Maps the EPOS
# attribute name → the reference table to resolve the IGS model string against (by
# its `name` column).
_TYPE_RESOLVE: dict[str, str] = {
    "antenna_type": "antenna_type",
    "receiver_type": "receiver_type",
    "radome_type": "radome_type",
}

# ECEF (ITRF2008-ish) target CRS used by the legacy script for station x/y/z.
_GEOCENT_CRS = "+proj=geocent +ellps=GRS80 +units=m +no_defs"


@dataclass
class EtlResult:
    """Outcome of an ETL run."""

    stations: int = 0
    inserted: int = 0
    updated: int = 0
    items: int = 0
    errors: list[str] = field(default_factory=list)


def llh_to_xyz(lat: float, lon: float, alt: float) -> tuple[float, float, float]:
    """WGS84 lat/lon/alt → ECEF x/y/z (metres, 3-dp), via pyproj Transformer."""
    import pyproj

    transformer = pyproj.Transformer.from_crs("EPSG:4326", _GEOCENT_CRS, always_xy=True)
    x, y, z = transformer.transform(lon, lat, alt)
    return round(x, 3), round(y, 3), round(z, 3)


def _tos_get(client: Any, endpoint: str) -> Any:
    """One TOS GET through the client (rides canonical_tos_url / the URL fix)."""
    return client._make_request(endpoint)


def _clear_station_items(cur, id_station: int) -> None:
    """Delete this station's items + item_attributes + joins (surgical re-sync).

    The filter_{antenna,radome,receiver} rows (written by the schema triggers and
    FK-referencing item_attribute, no ON DELETE CASCADE) must go first.
    """
    cur.execute("SELECT id_item FROM station_item WHERE id_station = %s", (id_station,))
    item_ids = [r[0] for r in cur.fetchall()]
    if not item_ids:
        return
    cur.execute("SELECT id FROM item_attribute WHERE id_item = ANY(%s)", (item_ids,))
    ia_ids = [r[0] for r in cur.fetchall()]
    if ia_ids:
        for ftab in ("filter_antenna", "filter_radome", "filter_receiver"):
            cur.execute(
                f"DELETE FROM {ftab} WHERE id_item_attribute = ANY(%s)", (ia_ids,)
            )
    cur.execute("DELETE FROM item_attribute WHERE id_item = ANY(%s)", (item_ids,))
    cur.execute("DELETE FROM station_item WHERE id_station = %s", (id_station,))
    cur.execute("DELETE FROM item WHERE id = ANY(%s)", (item_ids,))


def _attribute_id(cur, epos_name: str) -> Optional[int]:
    """Resolve an EPOS controlled-vocabulary attribute id by name (seeded table)."""
    cur.execute("SELECT id FROM attribute WHERE name = %s", (epos_name,))
    row = cur.fetchone()
    return int(row[0]) if row else None


def _resolve_type_id(cur, table: str, igs_name: Optional[str]) -> Optional[int]:
    """Resolve an IGS model string to a {antenna,receiver,radome}_type.id by name.

    ``table`` comes only from the trusted :data:`_TYPE_RESOLVE` map (not user input).
    """
    if not igs_name:
        return None
    cur.execute(f"SELECT id FROM {table} WHERE name = %s", (igs_name,))
    row = cur.fetchone()
    return int(row[0]) if row else None


def _monument_values(child_history: dict[str, Any]) -> dict[str, Any]:
    attrs = child_history.get("attributes", [])
    return {
        "description": "None",  # NOT NULL in schema; legacy placeholder
        "inscription": None,
        "height": get_attribute_value(attrs, "gps_monument_height"),
        "foundation": None,
        "foundation_depth": get_attribute_value(attrs, "foundation_depth"),
    }


def upsert_station(conn, station: dict[str, Any], client: Any) -> tuple[str, int]:
    """Upsert one EPOS station (metadata + device-history items) in a transaction.

    Returns ``(outcome, n_items)`` where outcome is "inserted"/"updated". Raises on
    a hard failure (the caller records it and rolls back this station's
    transaction; other stations are unaffected).
    """
    attrs = station["attributes"]

    def gv(code: str) -> Optional[str]:
        return get_attribute_value(attrs, code)

    marker = (gv("marker") or "").upper()
    id_entity = station["id_entity"]

    # Fetch the station's device history once (children + their subtypes/attrs).
    history = _tos_get(client, f"history/entity/{id_entity}/") or {}
    children = history.get("children_connections", []) or []

    # Resolve each child's subtype/attributes, and find the current monument.
    resolved_children: list[tuple[dict, dict]] = []
    monument_vals: Optional[dict[str, Any]] = None
    for child in children:
        ch = _tos_get(client, f"history/entity/{child['id_entity_child']}/") or {}
        subtype = ch.get("code_entity_subtype")
        if subtype not in WHITELISTED_ITEMS:
            continue
        resolved_children.append((child, ch))
        if subtype == "monument":
            # Prefer the open (current) monument; otherwise last seen.
            if monument_vals is None or child.get("time_to") is None:
                monument_vals = _monument_values(ch)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, id_location, id_geological FROM station "
            "WHERE upper(marker) = %s",
            (marker,),
        )
        row = cur.fetchone()
        existing = (
            {"id": row[0], "id_location": row[1], "id_geological": row[2]}
            if row
            else None
        )

        id_agency = get_or_create(
            cur,
            "agency",
            {"abbreviation": "IMO"},
            {
                "name": "Icelandic Meteorological Office",
                "address": "Bústaðarvegur 7-9, 104, Reykjavík",
                "www": "www.vedur.is",
                "infos": None,
            },
        )
        id_state = get_or_create(cur, "state", {"id_country": 102, "name": "None"})
        id_city = get_or_create(cur, "city", {"id_state": id_state, "name": "None"})

        # Coordinates (ECEF + LLH).
        lat = float(gv("lat") or 0.0)
        lon = float(gv("lon") or 0.0)
        alt = float(gv("altitude") or 0.0)
        x, y, z = llh_to_xyz(lat, lon, alt)
        coord_vals = {"x": x, "y": y, "z": z, "lat": lat, "lon": lon, "altitude": alt}
        if existing:
            cur.execute(
                "SELECT id_coordinates FROM location WHERE id = %s",
                (existing["id_location"],),
            )
            id_coord = cur.fetchone()[0]
            update_row(cur, "coordinates", id_coord, coord_vals)
            id_location = existing["id_location"]
        else:
            id_coord = insert_row(cur, "coordinates", coord_vals)
            id_location = insert_row(
                cur,
                "location",
                {
                    "id_city": id_city,
                    "id_coordinates": id_coord,
                    "id_tectonic": None,
                    "description": None,
                },
            )

        continuity = gv("continuity") or "GNSS Continuous"
        id_station_type = get_or_create(
            cur, "station_type", {"name": "None"}, {"type": continuity}
        )

        id_monument = None
        if monument_vals is not None:
            id_monument = insert_row(cur, "monument", monument_vals)

        id_bedrock = insert_row(
            cur,
            "bedrock",
            {"condition": gv("bedrock_condition") or "", "type": gv("bedrock_type")},
        )
        geo_vals = {
            "id_bedrock": id_bedrock,
            "characteristic": gv("geological_characteristic") or "",
            "fracture_spacing": None,
            "fault_zone": None,
            "distance_to_fault": None,
        }
        if existing and existing["id_geological"]:
            update_row(cur, "geological", existing["id_geological"], geo_vals)
            id_geological = existing["id_geological"]
        else:
            id_geological = insert_row(cur, "geological", geo_vals)

        station_vals = {
            "name": gv("name") or marker,
            "marker": marker,
            "description": gv("description"),
            "date_from": gv("date_start"),
            "date_to": gv("date_end"),
            "id_station_type": id_station_type,
            "comment": gv("comment"),
            "id_location": id_location,
            "id_monument": id_monument,
            "id_geological": id_geological,
            "iers_domes": gv("iers_domes_number"),
            "cpd_num": None,
            "monument_num": 0,
            "receiver_num": 0,
            "country_code": None,
        }
        if existing:
            update_row(cur, "station", existing["id"], station_vals)
            id_station = existing["id"]
            was_insert = False
        else:
            id_station = insert_row(cur, "station", station_vals)
            was_insert = True

        # Contact (best-effort, SAVEPOINT-guarded — a contact glitch must not
        # poison the station-core transaction).
        cur.execute("SAVEPOINT contact_sp")
        try:
            _upsert_contact(cur, client, id_entity, id_agency, id_station)
            cur.execute("RELEASE SAVEPOINT contact_sp")
        except Exception as exc:  # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT contact_sp")
            logger.debug("contact upsert skipped for %s: %s", marker, exc)

        # Items: clear this station's items, then repopulate from history via the
        # EPOS controlled-vocabulary mapping (_populate_items). SAVEPOINT-guarded so
        # an unexpected schema-trigger failure rolls back ONLY the items and the
        # station-core metadata still commits.
        _clear_station_items(cur, id_station)
        n_items = 0
        items_error: Optional[str] = None
        cur.execute("SAVEPOINT items_sp")
        try:
            n_items = _populate_items(cur, id_station, resolved_children)
            cur.execute("RELEASE SAVEPOINT items_sp")
        except Exception as exc:  # noqa: BLE001 - isolate items from station-core
            cur.execute("ROLLBACK TO SAVEPOINT items_sp")
            items_error = str(exc).splitlines()[0]
            n_items = 0
            logger.warning("items skipped for %s: %s", marker, items_error)

    conn.commit()
    outcome = "inserted" if was_insert else "updated"
    logger.info("ETL %s %s (%d items)", marker, outcome, n_items)
    return outcome, n_items


def _populate_items(cur, id_station: int, resolved_children: list) -> int:
    """Insert item/station_item/item_attribute rows for a station's device history.

    Maps each TOS device attribute onto the EPOS controlled vocabulary
    (:data:`_ATTR_MAP`) and, for the trigger-controlled type attributes, resolves
    the IGS model string to ``*_type.id`` in ``value_numeric``
    (:data:`_TYPE_RESOLVE`). Unmapped TOS codes are dropped; an unresolved model is
    logged and that one attribute skipped (so the station's other items still load
    rather than tripping the schema trigger).
    """
    n_items = 0
    for child, ch in resolved_children:
        subtype = ch["code_entity_subtype"]
        code_map = _ATTR_MAP.get(subtype, {})
        id_item_type = get_or_create(cur, "item_type", {"name": subtype})
        id_item = insert_row(
            cur,
            "item",
            {
                "id_item_type": id_item_type,
                "id_contact_as_producer": None,
                "id_contact_as_owner": None,
                "comment": str(ch.get("id_entity", "")),
            },
        )
        insert_row(
            cur,
            "station_item",
            {
                "id_station": id_station,
                "id_item": id_item,
                "date_from": child.get("time_from"),
                "date_to": child.get("time_to"),
            },
        )
        for ca in ch.get("attributes", []):
            epos_name = code_map.get(ca.get("code"))
            if not epos_name:
                continue  # TOS code with no EPOS vocabulary slot
            id_attribute = _attribute_id(cur, epos_name)
            if id_attribute is None:
                logger.warning("EPOS attribute %r not in vocabulary", epos_name)
                continue
            value_numeric = None
            if epos_name in _TYPE_RESOLVE:
                value_numeric = _resolve_type_id(
                    cur, _TYPE_RESOLVE[epos_name], ca.get("value")
                )
                if value_numeric is None:
                    logger.warning(
                        "%s %r not in %s reference — skipping attribute",
                        epos_name,
                        ca.get("value"),
                        _TYPE_RESOLVE[epos_name],
                    )
                    continue  # avoid the trg_set_*_filter NOT-NULL trigger
            insert_row(
                cur,
                "item_attribute",
                {
                    "id_item": id_item,
                    "id_attribute": id_attribute,
                    "date_from": ca.get("date_from"),
                    "date_to": ca.get("date_to"),
                    "value_varchar": ca.get("value"),
                    "value_date": None,
                    "value_numeric": value_numeric,
                },
            )
        n_items += 1
    return n_items


def _upsert_contact(cur, client: Any, id_entity: int, id_agency: int, id_station: int):
    entity_contacts = _tos_get(client, f"entity_contacts/{id_entity}/") or []
    if not entity_contacts:
        return
    ec = entity_contacts[0]
    contact = _tos_get(client, f"contact/{ec['id_contact']}/") or {}
    id_contact = insert_row(
        cur,
        "contact",
        {
            "name": contact.get("name"),
            "title": contact.get("job_title"),
            "email": contact.get("email"),
            "phone": contact.get("phone_primary"),
            "gsm": contact.get("phone_secondary"),
            "comment": contact.get("comment"),
            "id_agency": id_agency,
            "role": ec.get("role"),
        },
    )
    insert_row(
        cur,
        "station_contact",
        {"id_station": id_station, "id_contact": id_contact, "role": ec.get("role")},
    )


def run_etl(
    conn,
    *,
    markers: Optional[list[str]] = None,
    client: Any = None,
    required: tuple[str, ...] = REQUIRED_ATTRIBUTES,
) -> EtlResult:
    """ETL the EPOS-eligible stations (optionally restricted to ``markers``).

    One station per transaction; a failing station is recorded and skipped, the
    rest proceed. Returns an :class:`EtlResult` summary.
    """
    if client is None:
        from tostools.api.tos_client import TOSClient

        client = TOSClient()

    eligible = epos_stations(required=required)
    if markers:
        want = {m.upper() for m in markers}
        eligible = [
            s
            for s in eligible
            if (get_attribute_value(s["attributes"], "marker") or "").upper() in want
        ]

    result = EtlResult()
    for station in eligible:
        marker = (get_attribute_value(station["attributes"], "marker") or "").upper()
        try:
            outcome, n_items = upsert_station(conn, station, client)
            result.stations += 1
            result.items += n_items
            if outcome == "inserted":
                result.inserted += 1
            else:
                result.updated += 1
        except Exception as exc:  # noqa: BLE001 - isolate per-station failures
            conn.rollback()
            msg = f"{marker}: {exc}"
            logger.error("ETL failed for %s", msg)
            result.errors.append(msg)
    return result
