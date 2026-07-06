"""Plan corrective moves for misfiled/misnamed raw archive files.

For each candidate the TRUE identity is decoded from the file content
(``teqc +meta`` — the receiver's embedded records): observation date, and
where available the antenna position and embedded station code. The plan
fixes everything the filename/path claims wrongly:

* **wrong date** — decoded first epoch ≠ filename date (e.g. the RHOF
  ``2000/2001`` batches holding 2010/2011 data);
* **wrong station** (``verify_station=True``) — the antenna position matches
  a DIFFERENT station's surveyed coordinates; the file moves to that
  station's tree and is renamed accordingly. Position decides (bgo's rule:
  coordinates confirm identity — embedded codes and filenames are claims);
  a position matching NO station within the gate is reported, never moved.
* **wrong extension** — content format's canonical extension differs (e.g.
  Septentrio SBF bytes in a ``.atc`` name → ``.sbf`` so extension-keyed
  tooling picks the right chain).

Planning is read-only (works off the read-only mount). Execution goes
through :func:`~receivers.archive.relocate.relocate_archive_files` (rawdata
gateway, dry-run default, never overwrites).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from .raw_format import (
    CANONICAL_EXT,
    MONTH_DIRS,
    TRIMBLE,
    UNKNOWN,
    build_raw_name,
    classify_raw,
    parse_raw_name,
    teqc_meta,
)

logger = logging.getLogger("receivers.archive.sort")

# Files smaller than this are stubs (0-byte / truncated header fragments seen
# in the .atc sweeps) — flagged, never relocated.
MIN_RAW_BYTES = 4096

# Position-identity gate: SAME metric as the converter's RINEX-header check
# (one knob: receivers.cfg [rinex] position_gate_m; default 30 m).
STATION_GATE_M = 10.0


def resolve_position_gate_m(override=None) -> float:
    """explicit override > receivers.cfg [rinex] position_gate_m > 30 m."""
    if override is not None:
        return float(override)
    try:
        from ..config.receivers_config import get_receivers_config

        v = get_receivers_config().get_rinex_config().get("position_gate_m")
        if v is not None:
            return float(v)
    except Exception:  # noqa: BLE001 - config optional
        pass
    return STATION_GATE_M


@dataclass(frozen=True)
class MovePlan:
    src_rel: str
    dst_rel: str
    fmt: str
    decoded_start: object  # datetime
    claimed: object  # datetime
    reasons: tuple = ()  # subset of ('wrong-date','wrong-station','wrong-ext')
    true_station: str = ""
    station_dist_m: Optional[float] = None


@dataclass(frozen=True)
class SkipInfo:
    rel: str
    reason: str
    detail: str = ""


def fleet_coordinates() -> dict:
    """station -> (lat, lon) for the whole fleet, from stations.cfg."""
    import configparser

    import gps_parser

    path = gps_parser.ConfigParser().get_stations_config_path()
    cp = configparser.ConfigParser()
    cp.read(path)
    fleet: dict = {}
    for sec in cp.sections():
        if len(sec) != 4 or not sec.isupper():
            continue
        lat, lon = cp[sec].get("latitude"), cp[sec].get("longitude")
        if lat and lon:
            try:
                fleet[sec] = (float(lat), float(lon))
            except ValueError:
                continue
    return fleet


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_station(lat: float, lon: float, fleet: dict) -> tuple[Optional[str], float]:
    """(station, distance_m) of the fleet station closest to (lat, lon)."""
    best, best_d = None, float("inf")
    for sta, (slat, slon) in fleet.items():
        d = _haversine_m(lat, lon, slat, slon)
        if d < best_d:
            best, best_d = sta, d
    return best, best_d


def _expected_rel(
    rel: str, decoded_start, new_name: str, *, station: Optional[str] = None
) -> Optional[str]:
    """Correct archive path: fix year/month dirs + filename (+ station dir),
    keep session/category segments as they are."""
    parts = rel.split("/")
    if len(parts) != 6:
        return None
    _y, _mon, path_sta, session, category, _name = parts
    return "/".join(
        [
            f"{decoded_start:%Y}",
            MONTH_DIRS[decoded_start.month],
            (station or path_sta).upper(),
            session,
            category,
            new_name,
        ]
    )


def plan_relocations(
    root: Path,
    rel_files: list[str],
    *,
    min_bytes: int = MIN_RAW_BYTES,
    verify_station: bool = False,
    station_gate_m: float = STATION_GATE_M,
) -> tuple[list[MovePlan], list[SkipInfo]]:
    """Classify + decode each file under ``root`` and propose corrective moves.

    Returns ``(plans, skips)``: plans only for files whose decoded identity
    (date / station / content-format) disagrees with the filename/path claim;
    everything else lands in skips with a reason. With ``verify_station`` a
    decoded position matching a different station RELOCATES the file there;
    a position matching no station within the gate is reported
    (``unknown-station``) and never moved.
    """
    root = Path(root)
    fleet = fleet_coordinates() if verify_station else {}
    plans: list[MovePlan] = []
    skips: list[SkipInfo] = []
    for rel in rel_files:
        path = root / rel
        name = path.name
        parsed = parse_raw_name(name)
        if parsed is None:
            skips.append(SkipInfo(rel, "unparseable-name"))
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            skips.append(SkipInfo(rel, "unreadable", str(exc)))
            continue
        if size < min_bytes:
            skips.append(SkipInfo(rel, "stub", f"{size} bytes < {min_bytes}"))
            continue
        fmt = classify_raw(path)
        if fmt == UNKNOWN:
            skips.append(SkipInfo(rel, "unknown-format"))
            continue
        meta = teqc_meta(path, fmt) if fmt != TRIMBLE else None
        if meta is None or meta.start is None:
            reason = "no-date-decoder" if fmt == TRIMBLE else "decode-failed"
            skips.append(SkipInfo(rel, reason, fmt))
            continue
        start = meta.start

        reasons: list[str] = []
        path_station = rel.split("/")[2] if len(rel.split("/")) == 6 else parsed.station

        # Station identity: the decoded position decides.
        true_station = ""
        dist: Optional[float] = None
        if verify_station and meta.lat is not None and meta.lon is not None:
            near, dist = nearest_station(meta.lat, meta.lon, fleet)
            if near is None or dist > station_gate_m:
                skips.append(
                    SkipInfo(
                        rel,
                        "unknown-station",
                        f"position ({meta.lat:.5f},{meta.lon:.5f}) matches no "
                        f"station within {station_gate_m:.0f} m "
                        f"(nearest {near} at {dist / 1000:.1f} km)",
                    )
                )
                continue
            if near != path_station.upper():
                reasons.append("wrong-station")
                true_station = near

        if start.date() != parsed.claimed.date():
            reasons.append("wrong-date")

        canon_ext = CANONICAL_EXT.get(fmt)
        new_ext = None
        if canon_ext and not parsed.ext.lower().startswith(canon_ext):
            reasons.append("wrong-ext")
            new_ext = canon_ext + (".gz" if parsed.ext.lower().endswith(".gz") else "")

        if not reasons:
            skips.append(SkipInfo(rel, "verified-correct", fmt))
            continue

        new_name = build_raw_name(
            parsed, start, station=true_station or None, ext=new_ext
        )
        dst_rel = _expected_rel(rel, start, new_name, station=true_station or None)
        if dst_rel is None:
            skips.append(SkipInfo(rel, "unexpected-layout"))
            continue
        plans.append(
            MovePlan(
                src_rel=rel,
                dst_rel=dst_rel,
                fmt=fmt,
                decoded_start=start,
                claimed=parsed.claimed,
                reasons=tuple(reasons),
                true_station=true_station or path_station.upper(),
                station_dist_m=dist,
            )
        )
        logger.info("remediation: %s [%s] -> %s", rel, ",".join(reasons), dst_rel)
    return plans, skips
