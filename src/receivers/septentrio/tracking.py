"""Build identity-safe PolaRX5 signal-tracking config from a constellation spec.

`rec-config --tracking gps+glonass` emits ONLY ``setSignalTracking`` /
``setSignalUsage`` (+ boot save) — it never touches the marker, NTRIP mountpoint,
log sessions, or RTCM output. Limiting tracking to fewer constellations cuts the
receiver's RF + processing power (the win for wind/solar stations), without
disturbing a live stream feed (unlike the full TEST_* templates which reset the
marker to TEST and the mountpoint to TEST0).
"""

from __future__ import annotations

from typing import List

#: Septentrio signal groups per constellation (PolaRX5 fw 5.x naming).
CONSTELLATION_SIGNALS = {
    "gps": "GPSL1CA+GPSL1PY+GPSL2PY+GPSL2C+GPSL5+GPSL1C",
    "glonass": "GLOL1CA+GLOL1P+GLOL2P+GLOL2CA+GLOL3",
    "galileo": "GALE1BC+GALE6BC+GALE5a+GALE5b+GALE5",
    "beidou": "BDSB1I+BDSB2I+BDSB3I+BDSB1C+BDSB2a+BDSB2b",
}

_ALIASES = {
    "glo": "glonass",
    "gln": "glonass",
    "gal": "galileo",
    "gale": "galileo",
    "bds": "beidou",
    "bei": "beidou",
}


def normalize_constellations(spec: str) -> List[str]:
    """Parse a ``gps+glonass`` style spec into canonical constellation names."""
    names = [c.strip().lower() for c in spec.replace(",", "+").split("+") if c.strip()]
    names = [_ALIASES.get(c, c) for c in names]
    unknown = [c for c in names if c not in CONSTELLATION_SIGNALS]
    if unknown:
        raise ValueError(
            f"unknown constellation(s): {unknown}; "
            f"choose from {sorted(CONSTELLATION_SIGNALS)} (aliases: {sorted(_ALIASES)})"
        )
    if not names:
        raise ValueError("empty tracking spec — e.g. gps+glonass")
    # de-dupe, preserve order
    seen: set[str] = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def build_tracking_commands(spec: str) -> List[str]:
    """Return the set* commands to track/use exactly ``spec`` constellations.

    e.g. ``build_tracking_commands("gps+glonass")`` →
        ["setSignalTracking, GPSL1CA+…+GLOL3",
         "setSignalUsage, , GPSL1CA+…+GLOL3",
         "eccf, Current, Boot"]
    """
    cons = normalize_constellations(spec)
    signals = "+".join(CONSTELLATION_SIGNALS[c] for c in cons)
    return [
        f"setSignalTracking, {signals}",
        f"setSignalUsage, , {signals}",
        "eccf, Current, Boot",
    ]
