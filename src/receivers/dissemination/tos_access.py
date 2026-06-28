"""TOS access for dissemination — the EPOS station filter and QC session provider.

Two jobs, both reading TOS through :mod:`tostools` (so they ride the
``/tos/internal`` URL migration — the legacy epos-gnss scripts hard-coded the now
-dead ``/tos/v1/`` paths):

1. **EPOS include-filter** — which stations to disseminate: those flagged
   ``in_network_epos = true`` in TOS *and* carrying the minimum required
   attributes (ported from the legacy ``checkMinimumRequirements``).
2. **QC session provider** — supplies the header-QC gate (T2) with the TOS
   ``device_history`` session covering a given observation date.

The bulk geophysical listing uses the legacy bodyless GET on the search endpoint
(there is no per-call identifier), routed through ``canonical_tos_url`` for the
URL fix. Everything else uses the public :class:`tostools.api.tos_client.TOSClient`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from .qc_gate import select_session

logger = logging.getLogger("receivers.dissemination.tos")

# Minimum TOS attributes a station must carry to be disseminated to EPOS
# (ported from legacy tosToDatabase REQUIRED_ATTRIBUTES).
REQUIRED_ATTRIBUTES = (
    "marker",
    "lat",
    "lon",
    "altitude",
    "bedrock_condition",
    "bedrock_type",
    "geological_characteristic",
    "name",
    "date_start",
)


def get_attribute_value(attributes: list[dict[str, Any]], code: str) -> Optional[str]:
    """Return the value of the ``code`` attribute, or None (legacy semantics)."""
    for attr in attributes or []:
        if attr.get("code") == code:
            return attr.get("value")
    return None


def is_epos_flagged(station: dict[str, Any]) -> bool:
    """True if the station's ``in_network_epos`` attribute is the string 'true'."""
    val = get_attribute_value(station.get("attributes", []), "in_network_epos")
    return val is not None and val.lower() == "true"


def missing_required_attributes(
    station: dict[str, Any], required: tuple[str, ...] = REQUIRED_ATTRIBUTES
) -> list[str]:
    """Required attributes absent from ``station`` (empty list ⇒ meets minimum)."""
    attrs = station.get("attributes", [])
    return [code for code in required if get_attribute_value(attrs, code) is None]


def list_geophysical_stations(
    base_url: Optional[str] = None, timeout: int = 30
) -> list[dict[str, Any]]:
    """Bulk-fetch every geophysical station from TOS (legacy bodyless GET).

    Routed through ``canonical_tos_url`` so it uses the live ``/tos/internal``
    endpoint, not the dead ``/tos/v1/`` path the legacy scripts hard-coded.
    """
    import requests
    from tostools.api._http import canonical_tos_url
    from tostools.api.tos_client import DEFAULT_TOS_URL

    url = canonical_tos_url(
        base_url or DEFAULT_TOS_URL, "entity/search/station/geophysical/"
    )
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "objects" in data:
        objects: list[dict[str, Any]] = data["objects"]
        return objects
    return data if isinstance(data, list) else []


def epos_stations(
    stations: Optional[list[dict[str, Any]]] = None,
    *,
    required: tuple[str, ...] = REQUIRED_ATTRIBUTES,
    base_url: Optional[str] = None,
) -> list[dict[str, Any]]:
    """EPOS-eligible stations: ``in_network_epos=true`` AND minimum attributes.

    Pass ``stations`` to filter an already-fetched list (testable offline);
    otherwise the full geophysical list is fetched from TOS.
    """
    if stations is None:
        stations = list_geophysical_stations(base_url=base_url)
    eligible: list[dict[str, Any]] = []
    for station in stations:
        if not is_epos_flagged(station):
            continue
        missing = missing_required_attributes(station, required)
        if missing:
            marker = get_attribute_value(station.get("attributes", []), "marker")
            logger.warning(
                "EPOS station %s skipped — missing TOS attributes: %s",
                marker,
                ", ".join(missing),
            )
            continue
        eligible.append(station)
    return eligible


def epos_markers(
    stations: Optional[list[dict[str, Any]]] = None,
    *,
    required: tuple[str, ...] = REQUIRED_ATTRIBUTES,
    base_url: Optional[str] = None,
) -> list[str]:
    """Upper-case 4-char markers of EPOS-eligible stations."""
    out = []
    for station in epos_stations(stations, required=required, base_url=base_url):
        marker = get_attribute_value(station.get("attributes", []), "marker")
        if marker:
            out.append(marker.upper())
    return sorted(out)


def session_fingerprint(session: Optional[dict[str, Any]]) -> str:
    """A stable hash of the header-relevant TOS fields of a device session.

    Used as part of the convert-cache key so a TOS change that alters the RINEX
    header (marker / receiver / antenna / radome) invalidates exactly the affected
    converted files — the mechanism behind the retroactive header-correction
    re-push. Curated to header-affecting fields only, so unrelated TOS edits don't
    needlessly re-render. Empty string for a missing session.
    """
    import hashlib
    import json

    if not session:
        return ""

    def dev(d: Any, keys: tuple[str, ...]) -> dict[str, Any]:
        d = d or {}
        return {k: d.get(k) for k in keys}

    rel = {
        "marker": session.get("marker"),
        "receiver": dev(
            session.get("gnss_receiver"),
            ("model", "serial_number", "firmware_version"),
        ),
        "antenna": dev(
            session.get("antenna"), ("model", "serial_number", "antenna_height")
        ),
        "radome": dev(session.get("radome"), ("model",)),
    }
    blob = json.dumps(rel, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def make_session_provider(client: Any = None):
    """Build a QC session provider ``(station, observation_dt) -> session|None``.

    Looks up the station's TOS ``device_history`` and returns the session
    covering ``observation_dt`` (augmented with the station ``marker`` so the
    header-QC marker check works). Returns None on any TOS failure — the gate
    treats that as "no coverage" and refuses the push (fail-safe).
    """

    def provider(station: str, observation_dt: datetime) -> Optional[dict[str, Any]]:
        nonlocal client
        try:
            if client is None:
                from tostools.api.tos_client import TOSClient

                client = TOSClient()
            meta = client.get_complete_station_metadata(station)
        except Exception as exc:  # noqa: BLE001 - any TOS failure ⇒ fail-safe skip
            logger.warning("TOS session lookup failed for %s: %s", station, exc)
            return None
        if not meta:
            return None
        device_history = meta.get("device_history", []) or []
        session = select_session(device_history, observation_dt)
        if session is None:
            return None
        # compare_rinex_to_tos reads session["marker"]; it lives at station level.
        session.setdefault("marker", (meta.get("marker") or station).upper())
        return session

    return provider
