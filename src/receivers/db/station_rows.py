"""The ONE cfg→``stations`` mapping, shared by every writer of that table.

Why this module exists
----------------------
``stations`` had three independent writers that disagreed about what a row is:

* ``db/seeder.py`` — complete: all 17 columns including ``latitude`` /
  ``longitude`` / ``height``. Runs only on demand (``receivers db seed``).
* ``health/db_writer.py`` — auto-creates a row the first time the live
  pipeline touches a station, from a **12-column** subset that **omits the
  three position columns**.
* ``scheduling/bulk_scheduler._sync_station_status_to_db`` — a plain
  ``UPDATE`` of four fields, run at startup and on every ``stations.cfg``
  change.

The consequence, measured on rek-d01 2026-10-02: every station whose row was
created by the pipeline carries NULL coordinates, and nothing ever fills them,
because the only writer that knows about position never runs again. Four of
200 rows were in that state — VFLS and VFLN (added that day), **NPSK since
2026-08-09 and VOTT since 2026-06-17** — all four with coordinates sitting in
``stations.cfg`` the whole time. It is not visible as an outage because
``station_dashboard_data`` does ``COALESCE(s.latitude, m.latitude)``: the
dashboard silently falls back to the receiver's own live PVT fix, so the map
shows the station at a drifting single-epoch position instead of its
configured one.

So this module holds the mapping and the upsert, and the seeder and the
scheduler sync both call it. Adding a writer here without adopting it in the
same change is the orphaned-abstraction pattern this codebase keeps paying
for; ``db_writer``'s narrower insert is deliberately left alone (it owns
``ip_address`` from a live probe) and is made harmless by the sync filling in
the rest within a minute.

``gethostbyname`` is NOT done here
----------------------------------
``seed_stations`` resolves ``router_ip`` to an ``inet``, which is a blocking
DNS call per station — 200 of them. That is acceptable in a CLI seed and is
not acceptable on the scheduler's config-watcher thread, so ``resolve_ip`` is
opt-in and off by default. The scheduler leaves ``ip_address`` to
``db_writer``, which has the probe result anyway.
"""

from __future__ import annotations

import socket
from typing import Any, Mapping, Optional

#: Column order for :data:`STATION_UPSERT_SQL`. ``sid`` first; the rest match
#: :func:`station_row_from_cfg`'s return order.
STATION_COLUMNS = (
    "sid",
    "receiver_type",
    "power_type",
    "antenna_type",
    "marker_name",
    "marker_number",
    "observer",
    "agency",
    "ip_address",
    "http_port",
    "station_name",
    "station_owner",
    "station_status",
    "health_check",
    "latitude",
    "longitude",
    "height",
)

#: ``station_status`` and ``health_check`` are CFG-AUTHORITATIVE — cfg clearing
#: one back to active (NULL) must clear it in the DB, so they take EXCLUDED
#: verbatim. Everything else is COALESCE: cfg wins when cfg has a value, and an
#: absent cfg key never erases a value another writer established (notably
#: ``ip_address``, which ``db_writer`` fills from a live probe).
#:
#: The ``DO UPDATE … WHERE`` is what keeps a config change from rewriting all
#: ~200 rows and fanning all of them out to the pgdev mirror: an UPDATE only
#: happens for a row that actually differs. An INSERT is unconditional, which
#: is the whole point — that is the new-station case.
STATION_UPSERT_SQL = """
    INSERT INTO stations (
        sid, receiver_type, power_type, antenna_type,
        marker_name, marker_number, observer, agency,
        ip_address, http_port, station_name, station_owner,
        station_status, health_check,
        latitude, longitude, height
    )
    VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s,
        %s::inet, %s, %s, %s, %s, %s,
        %s, %s, %s
    )
    ON CONFLICT (sid) DO UPDATE SET
        receiver_type = COALESCE(EXCLUDED.receiver_type, stations.receiver_type),
        power_type    = COALESCE(EXCLUDED.power_type, stations.power_type),
        antenna_type  = COALESCE(EXCLUDED.antenna_type, stations.antenna_type),
        marker_name   = COALESCE(EXCLUDED.marker_name, stations.marker_name),
        marker_number = COALESCE(EXCLUDED.marker_number, stations.marker_number),
        observer      = COALESCE(EXCLUDED.observer, stations.observer),
        agency        = COALESCE(EXCLUDED.agency, stations.agency),
        ip_address    = COALESCE(EXCLUDED.ip_address, stations.ip_address),
        http_port     = COALESCE(EXCLUDED.http_port, stations.http_port),
        station_name  = COALESCE(EXCLUDED.station_name, stations.station_name),
        station_owner = COALESCE(EXCLUDED.station_owner, stations.station_owner),
        station_status = EXCLUDED.station_status,
        health_check   = EXCLUDED.health_check,
        latitude      = COALESCE(EXCLUDED.latitude, stations.latitude),
        longitude     = COALESCE(EXCLUDED.longitude, stations.longitude),
        height        = COALESCE(EXCLUDED.height, stations.height),
        updated_at    = NOW()
    WHERE stations.receiver_type  IS DISTINCT FROM COALESCE(EXCLUDED.receiver_type, stations.receiver_type)
       OR stations.power_type     IS DISTINCT FROM COALESCE(EXCLUDED.power_type, stations.power_type)
       OR stations.antenna_type   IS DISTINCT FROM COALESCE(EXCLUDED.antenna_type, stations.antenna_type)
       OR stations.marker_name    IS DISTINCT FROM COALESCE(EXCLUDED.marker_name, stations.marker_name)
       OR stations.marker_number  IS DISTINCT FROM COALESCE(EXCLUDED.marker_number, stations.marker_number)
       OR stations.observer       IS DISTINCT FROM COALESCE(EXCLUDED.observer, stations.observer)
       OR stations.agency         IS DISTINCT FROM COALESCE(EXCLUDED.agency, stations.agency)
       OR stations.ip_address     IS DISTINCT FROM COALESCE(EXCLUDED.ip_address, stations.ip_address)
       OR stations.http_port      IS DISTINCT FROM COALESCE(EXCLUDED.http_port, stations.http_port)
       OR stations.station_name   IS DISTINCT FROM COALESCE(EXCLUDED.station_name, stations.station_name)
       OR stations.station_owner  IS DISTINCT FROM COALESCE(EXCLUDED.station_owner, stations.station_owner)
       OR stations.station_status IS DISTINCT FROM EXCLUDED.station_status
       OR stations.health_check   IS DISTINCT FROM EXCLUDED.health_check
       OR stations.latitude       IS DISTINCT FROM COALESCE(EXCLUDED.latitude, stations.latitude)
       OR stations.longitude      IS DISTINCT FROM COALESCE(EXCLUDED.longitude, stations.longitude)
       OR stations.height         IS DISTINCT FROM COALESCE(EXCLUDED.height, stations.height)
    RETURNING (xmax = 0) AS is_insert
"""


def safe_float(value: Any) -> Optional[float]:
    """Float or None. ``""`` is None, not 0.0 — an absent cfg key reads empty."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def station_row_from_cfg(
    sid: str, raw: Mapping[str, Any], *, resolve_ip: bool = False
) -> tuple:
    """Build the :data:`STATION_UPSERT_SQL` parameter tuple from a cfg section.

    Args:
        sid: 4-char station id.
        raw: the ``station`` sub-dict of ``gps_parser``'s
            ``getStationInfo(sid)`` — i.e. the ``stations.cfg`` section.
        resolve_ip: resolve ``router_ip`` to an address via DNS. **Blocking,
            one lookup per station** — on for the CLI seed, off on the
            scheduler's config-watcher thread (see the module docstring).

    Returns:
        A tuple in :data:`STATION_COLUMNS` order.
    """
    receiver_type = raw.get("receiver_type") or None
    power_type = raw.get("power_type") or None
    antenna_type = raw.get("antenna_type") or None
    marker_name = raw.get("rinex_marker_name") or None
    marker_number = raw.get("rinex_marker_number") or None
    observer = raw.get("rinex_observer") or None
    agency = raw.get("rinex_agency") or None
    station_name = raw.get("station_name") or None
    station_status = raw.get("station_status") or None
    health_check = raw.get("health_check") or None

    # 'active' is the default and is stored as NULL, so the two authoritative
    # fields agree with the scheduler's own normalisation. Without this an
    # explicit `station_status = active` in cfg would write the literal string
    # and every consumer treats a non-NULL status as "not operational".
    if station_status and station_status.strip().lower() == "active":
        station_status = None
    if health_check and health_check.strip().lower() == "active":
        health_check = None

    station_owner = raw.get("station_owner") or None
    if not station_owner and agency and agency != "IMO":
        station_owner = agency
    if not station_owner:
        station_owner = "IMO"

    # The SID is not a name. Storing it makes every label in Grafana read the
    # 4-char id twice.
    if station_name == sid:
        station_name = None

    ip_address = None
    if resolve_ip:
        ip_raw = raw.get("router_ip") or None
        if ip_raw:
            try:
                ip_address = socket.gethostbyname(ip_raw)
            except socket.gaierror:
                ip_address = None

    http_port = None
    http_port_raw = raw.get("receiver_httpport")
    if http_port_raw is not None:
        try:
            http_port = int(http_port_raw)
        except (ValueError, TypeError):
            pass

    return (
        sid,
        receiver_type,
        power_type,
        antenna_type,
        marker_name,
        marker_number,
        observer,
        agency,
        ip_address,
        http_port,
        station_name,
        station_owner,
        station_status,
        health_check,
        safe_float(raw.get("latitude")),
        safe_float(raw.get("longitude")),
        safe_float(raw.get("height")),
    )
