"""Field manifest: declarative mapping from cfg keys to source extractors.

Each :class:`FieldSpec` describes one row of the reconciliation table:

* ``cfg_key`` — the key as it appears in ``stations.cfg``
* ``label`` — short human-readable name for prompts and tables
* ``receiver_extract`` — pulls the value from the receiver health identity
  dict; ``None`` means the field can't be derived from a live receiver
* ``tos_extract`` — pulls the value from a TOS station record; ``None``
  means the field is not present in TOS
* ``equal`` — custom equality check (defaults to case-insensitive string
  compare after normalisation); used for fuzzy matches like
  ``PolaRX5 == PolaRx5``
* ``normalize`` — value transform applied before storing/comparing

Adding a new reconcilable field is a matter of appending a ``FieldSpec`` to
``FIELDS`` here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from . import tos_adapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _strip_lower_eq(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return False
    return a.strip().lower() == b.strip().lower()


def _receiver_type_eq(a: Optional[str], b: Optional[str]) -> bool:
    """Receiver type equality through the fingerprint matcher.

    ``PolaRX5`` (configured) and ``PolaRx5`` (reported) are considered equal
    because both resolve to the same canonical type via
    :func:`receiver_fingerprint.identify_receiver_type`.
    """
    if a is None or b is None:
        return False
    if a.strip().lower() == b.strip().lower():
        return True
    try:
        from ..health.receiver_fingerprint import identify_receiver_type
    except ImportError:
        return False
    type_a = identify_receiver_type({"receiver_model": a})
    type_b = identify_receiver_type({"receiver_model": b})
    if type_a is None or type_b is None:
        return False
    return type_a == type_b


def _approx_eq(decimals: int) -> Callable[[Optional[str], Optional[str]], bool]:
    """Equality up to ``decimals`` decimal places, for floats stored as strings."""

    def _check(a: Optional[str], b: Optional[str]) -> bool:
        if a is None or b is None:
            return False
        try:
            return round(float(a), decimals) == round(float(b), decimals)
        except (TypeError, ValueError):
            return a.strip() == b.strip()

    return _check


# ---------------------------------------------------------------------------
# FieldSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    cfg_key: str
    label: str
    receiver_extract: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
    tos_extract: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
    equal: Optional[Callable[[Optional[str], Optional[str]], bool]] = None
    normalize: Callable[[Optional[str]], Optional[str]] = _strip
    description: str = ""

    def values_equal(self, a: Optional[str], b: Optional[str]) -> bool:
        a_n = self.normalize(a)
        b_n = self.normalize(b)
        if a_n is None and b_n is None:
            return True
        if a_n is None or b_n is None:
            return False
        if self.equal is not None:
            return self.equal(a_n, b_n)
        return _strip_lower_eq(a_n, b_n)


# ---------------------------------------------------------------------------
# Field manifest
# ---------------------------------------------------------------------------

FIELDS: List[FieldSpec] = [
    # Receiver identity — both receiver and TOS can supply these.
    FieldSpec(
        cfg_key="receiver_type",
        label="Receiver Type",
        receiver_extract=lambda identity: identity.get("receiver_model"),
        tos_extract=tos_adapter.current_receiver_model,
        equal=_receiver_type_eq,
        description="Receiver model/type (e.g. PolaRX5, NetR9)",
    ),
    FieldSpec(
        cfg_key="receiver_serial",
        label="Receiver Serial",
        receiver_extract=lambda identity: identity.get("serial_number"),
        tos_extract=tos_adapter.current_receiver_serial,
        description="Receiver serial number",
    ),
    FieldSpec(
        cfg_key="receiver_firmware_version",
        label="Receiver Firmware",
        receiver_extract=lambda identity: identity.get("firmware_version"),
        tos_extract=tos_adapter.current_receiver_firmware,
        description="Active firmware version reported by the receiver",
    ),
    # Antenna fields — TOS only.
    FieldSpec(
        cfg_key="antenna_type",
        label="Antenna Type",
        tos_extract=tos_adapter.current_antenna_model,
        description="IGS-style antenna model code",
    ),
    FieldSpec(
        cfg_key="antenna_serial",
        label="Antenna Serial",
        tos_extract=tos_adapter.current_antenna_serial,
        description="Antenna serial number",
    ),
    FieldSpec(
        cfg_key="antenna_radome",
        label="Radome",
        tos_extract=tos_adapter.current_radome_model,
        description="Radome model code (NONE if absent)",
    ),
    FieldSpec(
        cfg_key="antenna_height",
        label="Antenna Height",
        tos_extract=tos_adapter.current_antenna_height,
        equal=_approx_eq(4),
        description="Antenna height above mark including monument offset (m)",
    ),
    # Coordinates and name — TOS only.
    FieldSpec(
        cfg_key="latitude",
        label="Latitude",
        tos_extract=tos_adapter.station_latitude,
        equal=_approx_eq(6),
        description="Decimal-degree latitude",
    ),
    FieldSpec(
        cfg_key="longitude",
        label="Longitude",
        tos_extract=tos_adapter.station_longitude,
        equal=_approx_eq(6),
        description="Decimal-degree longitude",
    ),
    FieldSpec(
        cfg_key="height",
        label="Height",
        tos_extract=tos_adapter.station_height,
        equal=_approx_eq(2),
        description="Ellipsoidal height (m)",
    ),
    FieldSpec(
        cfg_key="station_name",
        label="Station Name",
        tos_extract=tos_adapter.station_name,
        description="Long-form station name",
    ),
]


def fields_by_key() -> Dict[str, FieldSpec]:
    return {f.cfg_key: f for f in FIELDS}


def all_keys() -> List[str]:
    return [f.cfg_key for f in FIELDS]
