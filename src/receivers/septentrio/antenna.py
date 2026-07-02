"""Build identity-safe PolaRX5 antenna-identity config from stations.cfg.

``rec-config --set-antenna`` emits ONLY ``setAntennaOffset`` (+ boot save) —
it never touches the marker, NTRIP mounts, log sessions, or tracking. It
closes the metadata loop that ``cfg reconcile`` cannot: the RINEX
``ANT # / TYPE`` header is whatever the RECEIVER has configured (sbf2rin
echoes it verbatim), so after an antenna swap is recorded in TOS and
reconciled into stations.cfg, the box itself still emits the OLD antenna
until this push corrects it (the ODDF case: TOS/cfg say SEPPOLANT_X_MF,
headers keep saying TRM115000.00 — which the EPOS QC gate then flags).

Command syntax validated against a live PolaRx5 (fw 5.x) boot config::

    setAntennaOffset, Main, , , 0.6610
    setAntennaOffset, Main, , , , "TRM115000.00    NONE"
    setAntennaOffset, Main, , , , , "60243B0067"

i.e. ``setAntennaOffset, Main, dE, dN, dU, "<type+radome>", "<serial>"`` with
the antenna-type string packed IGS-style: 16-char model + 4-char radome = 20
chars exactly. Septentrio-only (PolaRx5 / mosaic command set); a NetR9/HTTP
counterpart would be a separate builder.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: The cfg-side "serial unknown" marker (mirrors the fleet stations.cfg
#: convention and TOS's synthetic serials) — pushed verbatim so future RINEX
#: headers carry an honest unknown-marker instead of a stale real serial.
UNKNOWN_SERIAL = "0000000000"


def _is_unknown_serial(serial: Optional[str]) -> bool:
    """True for empty / all-zeros / TOS-synthetic (``antenna-ODDF-20230706``)."""
    if serial is None:
        return True
    s = str(serial).strip()
    if not s:
        return True
    if set(s) == {"0"}:
        return True
    return "-" in s and s.split("-", 1)[0].isalpha()


def format_igs_antenna_field(antenna_type: str, radome: Optional[str] = None) -> str:
    """Pack model + radome into the IGS 20-char ``ANT # / TYPE`` string.

    16-char left-justified model + 4-char radome (blank radome → ``NONE``),
    e.g. ``("SEPPOLANT_X_MF", "NONE")`` → ``"SEPPOLANT_X_MF  NONE"`` and
    ``("TRM115000.00", None)`` → ``"TRM115000.00    NONE"`` (matches the
    live-extracted receiver string byte-for-byte).
    """
    model = (antenna_type or "").strip()
    if not model:
        raise ValueError("antenna type is required")
    if len(model) > 16:
        raise ValueError(f"antenna type {model!r} exceeds the IGS 16-char field")
    rad = (radome or "").strip().upper() or "NONE"
    if len(rad) > 4:
        raise ValueError(f"radome code {rad!r} exceeds the IGS 4-char field")
    return f"{model:<16}{rad:>4}"


def build_antenna_commands(
    antenna_type: str,
    radome: Optional[str],
    serial: Optional[str],
    up_m: float,
    east_m: float = 0.0,
    north_m: float = 0.0,
) -> List[str]:
    """Return the set* commands to configure the Main antenna identity.

    One full ``setAntennaOffset`` (all args explicit, so stale values can't
    survive in unset positions) + boot save::

        setAntennaOffset, Main, 0.0000, 0.0000, 0.6610, "SEPPOLANT_X_MF  NONE", "0000000000"
        eccf, Current, Boot

    An unknown serial (None / blank / zeros / TOS-synthetic) is pushed as
    :data:`UNKNOWN_SERIAL` — deliberately overwriting whatever the box holds,
    since a stale real serial (the previous antenna's) is worse than an
    honest unknown-marker in the RINEX header.
    """
    field = format_igs_antenna_field(antenna_type, radome)
    ser = UNKNOWN_SERIAL if _is_unknown_serial(serial) else str(serial).strip()
    if len(ser) > 20:
        raise ValueError(f"antenna serial {ser!r} exceeds the 20-char field")
    return [
        f"setAntennaOffset, Main, {east_m:.4f}, {north_m:.4f}, {up_m:.4f}, "
        f'"{field}", "{ser}"',
        "eccf, Current, Boot",
    ]


def build_antenna_commands_from_station_config(
    station_config: Dict[str, Any],
) -> List[str]:
    """Build the antenna push from a station's stations.cfg entry.

    Reads the reconciled (TOS-canonical) antenna fields — flat keys first,
    then the nested ``antenna`` section, mirroring
    ``cfg.reconciler._read_cfg_value``. Refuses when ``antenna_type`` or
    ``antenna_height`` is missing: pushing a blank type or silently zeroing
    the height would corrupt the very headers this verb exists to fix.
    """

    def _get(key: str) -> Optional[str]:
        val = station_config.get(key)
        if val is None:
            sub = key[len("antenna_") :]
            val = (station_config.get("antenna") or {}).get(sub)
        if val is None or str(val).strip() == "":
            return None
        return str(val).strip()

    antenna_type = _get("antenna_type")
    if antenna_type is None:
        raise ValueError("antenna_type missing from stations.cfg — reconcile first")
    height = _get("antenna_height")
    if height is None:
        raise ValueError(
            "antenna_height missing from stations.cfg — refusing to zero the "
            "receiver's ARP offset"
        )
    try:
        up_m = float(height)
        east_m = float(_get("antenna_east") or 0.0)
        north_m = float(_get("antenna_north") or 0.0)
    except ValueError as exc:
        raise ValueError(f"non-numeric antenna offset in stations.cfg: {exc}") from exc

    return build_antenna_commands(
        antenna_type=antenna_type,
        radome=_get("antenna_radome"),
        serial=_get("antenna_serial"),
        up_m=up_m,
        east_m=east_m,
        north_m=north_m,
    )
