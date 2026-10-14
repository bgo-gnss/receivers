"""TOS/config header correction for spliced stream dailies."""

from __future__ import annotations

import inspect
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .downsample import HeaderHook


def _accepts_cache(func) -> bool:
    try:
        return "tos_metadata_cache" in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


def make_tos_header_hook(
    station: str,
    day: date,
    *,
    station_config: Optional[dict] = None,
    tos_metadata_cache: Optional[dict] = None,
) -> HeaderHook:
    """Header correction for a spliced daily, the same call the converter makes.

    BNC writes its headers from a skeleton (``MARKER NUMBER`` = the 4-char id,
    ``ANT #`` = ``0000``, receiver frozen at whatever the skeleton held), so a
    splice inherits all of it. ``correct_rinex_from_tos`` rewrites the full set
    for the day (station config past ``rinex_config_valid_from``, TOS before),
    and applies the DOMES-only ``MARKER NUMBER`` rule. ``OBSERVER / AGENCY`` is
    injected as the converter does, since the TOS path does not emit it. Share
    one ``tos_metadata_cache`` across a station's days: one TOS fetch, not one
    per day.
    """

    def hook(obs: Path) -> None:
        from tostools.rinex import correct_rinex_from_tos

        extra = None
        rinex_cfg = (station_config or {}).get("rinex", {}) or {}
        observer = str(rinex_cfg.get("observer") or "").strip()
        agency = str(rinex_cfg.get("agency") or "").strip()
        if observer or agency:
            extra = {"OBSERVER / AGENCY": [observer, agency]}
        kwargs = dict(
            rinex_file=obs,
            station_id=station,
            observation_date=datetime(day.year, day.month, day.day),
            output_file=obs,
            station_config=station_config,
            extra_corrections=extra,
        )
        # Older tostools lack the kwarg and raise TypeError (the converter
        # guards the same way, converter_base._tos_cache_kw_supported).
        if _accepts_cache(correct_rinex_from_tos):
            kwargs["tos_metadata_cache"] = tos_metadata_cache
        out = correct_rinex_from_tos(**kwargs)
        if out is None:
            raise RuntimeError("TOS header correction returned nothing")

    return hook
