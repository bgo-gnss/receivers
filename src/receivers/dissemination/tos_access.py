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


def _attribute_active_at(attr: dict[str, Any], when: str) -> bool:
    """True if ``attr``'s [date_from, date_to) period covers the instant ``when``.

    Dates are TOS ISO strings (``YYYY-MM-DDTHH:MM:SS``) or None (open). ``when`` is
    an ISO string, so plain string comparison is correct (same fixed-width format).
    A closed period (date_to in the past — including the zero-duration
    ``date_from == date_to`` data-entry artifact) is NOT active.
    """
    date_from = attr.get("date_from")
    date_to = attr.get("date_to")
    if date_from is not None and when < date_from:
        return False
    if date_to is not None and when >= date_to:
        return False
    return True


def is_epos_flagged(station: dict[str, Any], *, at: Optional[str] = None) -> bool:
    """True if the station is **currently** in EPOS.

    Requires an ``in_network_epos = 'true'`` attribute whose period is *currently
    active* (open ``date_to``, or ``date_to`` in the future). A station whose only
    ``in_network_epos = true`` attribute is a closed/expired period — e.g. KRAC's
    zero-duration ``2023-10-23 → 2023-10-23`` artifact — is NOT in EPOS and must not
    be disseminated. ``at`` (ISO string, default now) makes the check testable.
    """
    when = at or datetime.now().isoformat()
    for attr in station.get("attributes", []) or []:
        if attr.get("code") != "in_network_epos":
            continue
        value = attr.get("value")
        if (
            value is not None
            and value.lower() == "true"
            and _attribute_active_at(attr, when)
        ):
            return True
    return False


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
        "domes": session.get("domes"),
        "owner_org": session.get("owner_org"),
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


# Header-affecting device components and the fields that go into each component's
# fingerprint. ``marker`` is station-scoped (a change affects the whole history);
# the device components are period-scoped (a change affects only that device's
# current period). Mirrors :func:`session_fingerprint`'s field selection.
_COMPONENT_FIELDS: dict[str, tuple[str, ...]] = {
    "gnss_receiver": ("model", "serial_number", "firmware_version"),
    "antenna": ("model", "serial_number", "antenna_height"),
    "radome": ("model",),
}


def _to_dt(value: Any) -> Optional[datetime]:
    """Coerce a TOS date (datetime or ISO string) to datetime, or None."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "").strip()[:19])
    except ValueError:
        return None


def _latest_covering(
    device_history: list[dict[str, Any]], when: datetime, device_key: str
) -> Optional[dict[str, Any]]:
    """The session covering ``when`` that carries ``device_key`` with the latest
    ``time_from`` (the device's *current* period). None if no session covers it.

    Per-device (not the merged :func:`select_session`): a firmware update opens a
    new receiver session with a recent ``time_from``, which is exactly the floor of
    the date range that change affects."""
    best: Optional[dict[str, Any]] = None
    best_from: Optional[datetime] = None
    for session in device_history:
        if not session.get(device_key):
            continue
        start = _to_dt(session.get("time_from"))
        end = _to_dt(session.get("time_to"))
        if start is not None and when < start:
            continue
        if end is not None and when >= end:
            continue
        if best_from is None or (start is not None and start > best_from):
            best, best_from = session, start
    return best


def reactive_components(
    device_history: list[dict[str, Any]],
    when: datetime,
    *,
    marker: str = "",
    domes: str = "",
) -> dict[str, dict[str, Any]]:
    """Per-component state for the reactive range diff.

    Returns ``{component: {"fp": <hash>, "since": <ISO date|None>}}`` for ``marker``
    (station-scoped, no ``since``) and each header device. ``fp`` lets the reactive
    layer detect *which* component changed; ``since`` (the device's current-period
    ``time_from``) is the floor of the date range that component's change affects.
    """
    import hashlib
    import json

    def _hash(obj: Any) -> str:
        blob = json.dumps(obj, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    components: dict[str, dict[str, Any]] = {
        "marker": {"fp": _hash({"marker": marker, "domes": domes}), "since": None}
    }
    for key, fields in _COMPONENT_FIELDS.items():
        session = _latest_covering(device_history, when, key)
        device = (session or {}).get(key) or {}
        start = _to_dt(session.get("time_from")) if session else None
        components[key] = {
            "fp": _hash({f: device.get(f) for f in fields}),
            "since": start.date().isoformat() if start else None,
        }
    return components


def history_fingerprint(
    device_history: list[dict[str, Any]],
    *,
    marker: str = "",
    domes: str = "",
    owner_org: str = "",
) -> str:
    """A stable hash over the header-relevant TOS fields of the *whole* history.

    Unlike :func:`session_fingerprint` (current session only), this folds in every
    session's header devices *and* its [time_from, time_to) period, so a retroactive
    correction to a **closed historical** session changes the fingerprint and the
    reactive detector classifies the station CHANGED. That closes the
    historical-session detection gap: current-session-only detection left an edit to
    a closed period invisible, so the affected historical files were never re-pushed.

    The convert cache (keyed per observation date on :func:`session_fingerprint`)
    still re-renders only the dates that actually differ; and when the change is
    purely historical (current-period components unchanged),
    :func:`receivers.dissemination.reactive.affected_floor` returns None ⇒ the
    sweep re-iterates the full backfill window and the cache gates the exact dates.

    Each session is sub-hashed independently (its normalized period + device fields)
    and the digests combined via a **sorted list of hex strings** — so there is no
    ``(date_from, date_to)`` tuple comparison, which would crash ``str < None`` on
    the always-open current period (the DYNC bug, tostools 66d2ad2). Period dates
    are normalized through :func:`_to_dt` so a datetime and its ISO-string form hash
    identically across reads. Empty string for an empty history (parity with
    :func:`session_fingerprint`).

    Interim boundary: mirrors :func:`session_fingerprint`'s field set, which omits
    ``monument_height`` — so a historical *monument-only* correction still goes
    undetected (DELTA H = antenna_ecc + monument_height). Fixing that touches the
    per-date cache key too and is deferred to the full per-period diff.
    """
    import hashlib
    import json

    if not device_history:
        return ""

    def dev(d: Any, keys: tuple[str, ...]) -> dict[str, Any]:
        d = d or {}
        return {k: d.get(k) for k in keys}

    digests: list[str] = []
    for session in device_history:
        start = _to_dt(session.get("time_from"))
        end = _to_dt(session.get("time_to"))
        rel: dict[str, Any] = {
            "from": start.isoformat() if start else None,
            "to": end.isoformat() if end else None,
        }
        for key, fields in _COMPONENT_FIELDS.items():
            rel[key] = dev(session.get(key), fields)
        blob = json.dumps(rel, sort_keys=True, default=str).encode()
        digests.append(hashlib.sha256(blob).hexdigest())

    combined = {
        "marker": marker,
        "domes": domes,
        "owner_org": owner_org,
        "sessions": sorted(digests),
    }
    blob = json.dumps(combined, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def make_history_fn(client: Any = None):
    """Build ``station, when -> history_fingerprint`` reading TOS device_history.

    Production wiring for reactive *detection* (history-wide, so retroactive edits
    to closed sessions are caught). Returns "" on any TOS failure — preserving the
    pre-existing transient-failure behaviour (the old session-provider path produced
    an empty fingerprint too); a flake on one station at worst triggers one wasted
    re-push that the next clean scan settles, never a lost change.
    """

    def fn(station: str, when: datetime) -> str:
        nonlocal client
        try:
            if client is None:
                from tostools.api.tos_client import TOSClient

                client = TOSClient()
            meta = client.get_complete_station_metadata(station)
        except (
            Exception
        ) as exc:  # noqa: BLE001 - TOS failure ⇒ "" (re-checked next scan)
            logger.warning("reactive history lookup failed for %s: %s", station, exc)
            return ""
        if not meta:
            return ""
        return history_fingerprint(
            meta.get("device_history", []) or [],
            marker=(meta.get("marker") or station).upper(),
            domes=(meta.get("iers_domes_number") or "").strip(),
            owner_org=(
                ((meta.get("contact") or {}).get("owner") or {}).get("organization")
                or ""
            ).strip(),
        )

    return fn


def make_components_fn(client: Any = None):
    """Build ``station, when -> reactive_components`` reading TOS device_history.

    Production wiring for the reactive range diff. Returns empty components on any
    TOS failure (the floor logic then falls back to the full backfill window)."""

    def fn(station: str, when: datetime) -> dict[str, dict[str, Any]]:
        nonlocal client
        try:
            if client is None:
                from tostools.api.tos_client import TOSClient

                client = TOSClient()
            meta = client.get_complete_station_metadata(station)
        except Exception as exc:  # noqa: BLE001 - TOS failure ⇒ empty (full window)
            logger.warning("reactive components lookup failed for %s: %s", station, exc)
            return {}
        if not meta:
            return {}
        return reactive_components(
            meta.get("device_history", []) or [],
            when,
            marker=(meta.get("marker") or station).upper(),
            domes=(meta.get("iers_domes_number") or "").strip(),
        )

    return fn


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
        # DOMES is station-level too — carried so the header finalizer can write it
        # into MARKER NUMBER (EPOS 4.1.7). Empty when the station has no DOMES.
        session.setdefault("domes", (meta.get("iers_domes_number") or "").strip())
        # Owner organization (station-level) drives the per-station RINEX
        # OBSERVER/AGENCY via agencies.yaml. Folded into session_fingerprint so a
        # re-designation (e.g. Landmælingar→NATT) re-renders the cached header.
        owner_org = (
            ((meta.get("contact") or {}).get("owner") or {}).get("organization") or ""
        ).strip()
        session.setdefault("owner_org", owner_org)
        return session

    return provider


class TOSSesionCache:
    """Cached TOS session provider — 1 TOS API call per station.

    Wraps :func:`gps_metadata` / :class:`TOSClient` with an in-memory cache:
    ``device_history`` and station-level metadata are fetched once and reused
    for every observation date. The cache lives for the life of the instance
    (typically one process / one CLI invocation).

    Use this for any fleet-wide sweep that queries the same station across
    many dates — ``receivers rinex --fix-headers``, header-QC gate, EPOS
    dissemination, site-log generation, fleet integrity checks.

    Example::

        cache = TOSSesionCache()
        for doy in range(1, 366):
            session = cache.get_session("RHOF", datetime(2026,1,1) + timedelta(doy-1))
            if session:
                ...
    """

    def __init__(self, client: Any = None) -> None:
        self._client = client
        self._device_history: dict[str, list[dict[str, Any]]] = {}
        self._metadata: dict[str, dict[str, Any]] = {}

    def _ensure_station(self, station: str) -> bool:
        """Fill the cache for ``station`` if not already cached. Returns True
        on success, False when TOS has no data / the lookup failed."""
        sid = station.upper()
        if sid in self._device_history:
            return True
        try:
            if self._client is None:
                from tostools.api.tos_client import TOSClient

                self._client = TOSClient()
            meta = self._client.get_complete_station_metadata(sid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TOS session cache: lookup failed for %s: %s", sid, exc)
            return False
        if not meta:
            logger.debug("TOS session cache: no metadata for %s", sid)
            return False
        self._device_history[sid] = meta.get("device_history", []) or []
        self._metadata[sid] = meta
        return True

    def get_session(
        self, station: str, observation_dt: datetime
    ) -> Optional[dict[str, Any]]:
        """Return the TOS device session covering ``observation_dt``.

        Fetches from TOS only on the first call for this station. Returns None
        when no session covers the date or TOS is unreachable — callers should
        treat that as "no coverage, skip" (fail-safe).

        The returned session is augmented with station-level fields (``marker``,
        ``domes``, ``owner_org`` — same as :func:`make_session_provider`).
        """
        sid = station.upper()
        if not self._ensure_station(sid):
            return None
        meta = self._metadata.get(sid, {})
        history = self._device_history.get(sid, [])
        session = select_session(history, observation_dt)
        if session is None:
            return None
        # compare_rinex_to_tos reads session["marker"] — it lives at station level.
        session.setdefault("marker", (meta.get("marker") or sid).upper())
        session.setdefault(
            "domes", (meta.get("iers_domes_number") or "").strip()
        )
        owner_org = (
            ((meta.get("contact") or {}).get("owner") or {}).get("organization")
            or ""
        ).strip()
        session.setdefault("owner_org", owner_org)
        return session

    def get_metadata(self, station: str) -> Optional[dict[str, Any]]:
        """Return the raw station metadata dict (marker, domes, contacts, etc.).

        Cached — 1 TOS call per station. Useful for consumers that need
        station-level fields beyond the device session."""
        sid = station.upper()
        if not self._ensure_station(sid):
            return None
        return self._metadata.get(sid)

    @property
    def station_count(self) -> int:
        return len(self._device_history)
