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

# TOS device subtypes carried into EPOS as station items (legacy whitelist).
WHITELISTED_ITEMS = ("antenna", "receiver", "radome", "monument")

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
    """Delete this station's items + item_attributes + joins (surgical re-sync)."""
    cur.execute("SELECT id_item FROM station_item WHERE id_station = %s", (id_station,))
    item_ids = [r[0] for r in cur.fetchall()]
    if not item_ids:
        return
    cur.execute("DELETE FROM item_attribute WHERE id_item = ANY(%s)", (item_ids,))
    cur.execute("DELETE FROM station_item WHERE id_station = %s", (id_station,))
    cur.execute("DELETE FROM item WHERE id = ANY(%s)", (item_ids,))


def _monument_values(child_history: dict[str, Any]) -> dict[str, Any]:
    attrs = child_history.get("attributes", [])
    return {
        "description": "None",  # NOT NULL in schema; legacy placeholder
        "inscription": None,
        "height": get_attribute_value(attrs, "gps_monument_height"),
        "foundation": None,
        "foundation_depth": get_attribute_value(attrs, "foundation_depth"),
    }


def upsert_station(conn, station: dict[str, Any], client: Any) -> str:
    """Upsert one EPOS station (metadata + device-history items) in a transaction.

    Returns the marker. Raises on a hard failure (the caller records it and the
    transaction for this station is rolled back; other stations are unaffected).
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

        # Items: clear this station's items, then repopulate from history.
        # Guarded by a SAVEPOINT — the EPOS schema enforces a CONTROLLED
        # VOCABULARY (attribute ids 1=antenna_type … with a trigger that resolves
        # antenna/receiver/radome model strings to *_type.id in value_numeric).
        # The legacy code-based attribute insert does NOT satisfy that, so until
        # the TOS→EPOS-vocab mapping is built, an item failure rolls back ONLY the
        # items and the station-core metadata still commits. See plan T5b.
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
            logger.warning(
                "items skipped for %s (needs EPOS vocab mapping, T5b): %s",
                marker,
                items_error,
            )

    conn.commit()
    logger.info(
        "ETL %s %s (%d items)", marker, "inserted" if was_insert else "updated", n_items
    )
    return "inserted" if was_insert else "updated"


def _populate_items(cur, id_station: int, resolved_children: list) -> int:
    """Insert item/station_item/item_attribute rows for a station's device history.

    NOTE: writes ``item_attribute`` with the raw TOS attribute code as the
    attribute name and ``value_numeric=None`` — the legacy shape. The EPOS schema
    trigger on ``attribute.id=1`` (antenna_type) rejects this, so this is expected
    to fail until T5b maps TOS attributes onto the EPOS controlled vocabulary and
    resolves model strings to ``*_type.id``. Kept faithful so the gap is explicit.
    """
    n_items = 0
    for child, ch in resolved_children:
        id_item_type = get_or_create(
            cur, "item_type", {"name": ch["code_entity_subtype"]}
        )
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
            id_attribute = get_or_create(cur, "attribute", {"name": ca["code"]})
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
                    "value_numeric": None,
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
            outcome = upsert_station(conn, station, client)
            result.stations += 1
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
