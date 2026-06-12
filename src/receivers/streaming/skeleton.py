"""RINEX header skeletons (.SKL) — stored on disk, refreshed from TOS on change.

BNC fills every hourly RINEX header from a per-station skeleton file
(``<rnxPath>/<SID>.SKL``, referenced by ``rnxSkel=SKL``). The skeleton is the
**stored active header**: BNC reads it for every file write, so the write path never
touches TOS. A separate periodic refresh updates the stored skeleton from TOS only
when the station's equipment metadata changed (antenna/receiver swap, firmware update).

Design: the skeleton is a *template* — its static lines (COMMENT, APPROX POSITION XYZ,
WAVELENGTH FACT, END OF HEADER) are preserved; only the equipment-dependent lines
(MARKER, OBSERVER/AGENCY, REC #/TYPE/VERS, ANT #/TYPE, ANTENNA DELTA) are (re)filled
from TOS. Equipment names are IGS-standardised via ``tostools.standards.igs_equipment``.

This avoids the geodetic→ECEF conversion (position lives in the stored skeleton from
the original survey) and keeps the per-file write path TOS-free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: RINEX-2 header label column (0-indexed): data in [0:60], label in [60:80].
_LABEL_COL = 60


@dataclass
class SkeletonMetadata:
    """Equipment-dependent RINEX header fields sourced from TOS."""

    marker_name: Optional[str] = None
    marker_number: Optional[str] = None
    observer: Optional[str] = None
    agency: Optional[str] = None
    rec_serial: Optional[str] = None
    rec_type: Optional[str] = None  # IGS-standard receiver name
    rec_version: Optional[str] = None
    ant_serial: Optional[str] = None
    ant_type: Optional[str] = None  # IGS-standard antenna name
    ant_radome: Optional[str] = None
    antenna_h: Optional[float] = None
    antenna_e: Optional[float] = None
    antenna_n: Optional[float] = None


def _fmt_marker(value: str) -> str:
    return f"{value:<60}"


def _fmt_observer_agency(observer: str, agency: str) -> str:
    return f"{observer:<20}{agency:<40}"


def _fmt_rec(serial: str, rtype: str, version: str) -> str:
    return f"{serial:<20}{rtype:<20}{version:<20}"


def _fmt_ant(serial: str, model: str, radome: str) -> str:
    # antenna number cols 1-20; antenna model cols 21-35 + radome cols 37-40
    return f"{serial:<20}{model:<15} {radome:<4}".ljust(60)


def _fmt_delta(h: float, e: float, n: float) -> str:
    return f"{h:14.4f}{e:14.4f}{n:14.4f}".ljust(60)


def fill_skeleton(template: str, meta: SkeletonMetadata) -> str:
    """Return ``template`` with equipment lines (re)filled from ``meta``.

    Lines whose TOS value is ``None`` keep the template's existing data. Static
    lines (COMMENT, APPROX POSITION XYZ, WAVELENGTH FACT, END OF HEADER, …) are
    always preserved. Label columns (61-80) are never altered.
    """
    out: List[str] = []
    for raw in template.splitlines():
        data = raw[:_LABEL_COL]
        label = raw[_LABEL_COL:].rstrip()
        new_data = _refill(label.strip(), data, meta)
        out.append(f"{new_data:<{_LABEL_COL}}{label}".rstrip())
    text = "\n".join(out)
    return text + "\n" if template.endswith("\n") else text


def _refill(label: str, data: str, meta: SkeletonMetadata) -> str:
    """Rebuild the data portion for an equipment line, else keep ``data``."""
    if label == "MARKER NAME" and meta.marker_name:
        return _fmt_marker(meta.marker_name)
    if label == "MARKER NUMBER" and meta.marker_number:
        return _fmt_marker(meta.marker_number)
    if label == "OBSERVER / AGENCY" and (meta.observer or meta.agency):
        return _fmt_observer_agency(meta.observer or "", meta.agency or "")
    if label == "REC # / TYPE / VERS" and (
        meta.rec_serial or meta.rec_type or meta.rec_version
    ):
        return _fmt_rec(
            meta.rec_serial or "", meta.rec_type or "", meta.rec_version or ""
        )
    if label == "ANT # / TYPE" and (meta.ant_serial or meta.ant_type):
        return _fmt_ant(
            meta.ant_serial or "", meta.ant_type or "", meta.ant_radome or "NONE"
        )
    if label == "ANTENNA: DELTA H/E/N" and meta.antenna_h is not None:
        return _fmt_delta(meta.antenna_h, meta.antenna_e or 0.0, meta.antenna_n or 0.0)
    return data


def refresh_skeleton(existing: str, meta: SkeletonMetadata) -> Tuple[str, bool]:
    """Refill ``existing`` from ``meta``; return (new_text, changed)."""
    updated = fill_skeleton(existing, meta)
    return updated, updated != existing


def metadata_from_tos(station: Dict[str, Any], *, station_id: str) -> SkeletonMetadata:
    """Map a TOS ``get_complete_station_metadata`` dict to skeleton fields.

    Reuses the battle-tested ``receivers.cfg.tos_adapter`` accessors and
    IGS-name standardisation from tostools.
    """
    from tostools.standards.igs_equipment import (
        to_igs_antenna,
        to_igs_radome,
        to_igs_receiver,
    )

    from ..cfg import tos_adapter as ta

    def _f(value: Optional[str]) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    # Fall back to the raw TOS value when the IGS table has no mapping — better a
    # valid raw name than a blank header. (The tostools IGS table currently misses
    # e.g. TRM115000.10 and maps mosaic-X5 to "SEPT MOSAICX5" vs the rcvr_ant.tab
    # spelling "SEPT MOSAIC-X5" — tracked as a tostools fix.)
    rec_model = ta.current_receiver_model(station)
    ant_model = ta.current_antenna_model(station)
    return SkeletonMetadata(
        marker_name=station_id,
        marker_number=station_id,
        rec_serial=ta.current_receiver_serial(station),
        rec_type=to_igs_receiver(rec_model) or rec_model,
        rec_version=ta.current_receiver_firmware(station),
        ant_serial=ta.current_antenna_serial(station),
        ant_type=to_igs_antenna(ant_model) or ant_model,
        ant_radome=to_igs_radome(ta.current_radome_model(station)) or "NONE",
        antenna_h=_f(ta.current_antenna_height(station)),
    )
