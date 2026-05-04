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


# Common placeholder strings used in stations.cfg / receivers when a real
# value is unknown. Treating these as ``None`` turns "cfg='0000000000' vs
# rx='Unknown'" from a CONFLICT into a MISSING — both sides admit ignorance.
_SERIAL_PLACEHOLDERS = frozenset({"unknown", "n/a", "na", "none", "—", "-"})


def _strip_placeholder(value: Optional[str]) -> Optional[str]:
    """Strip + treat known serial-number placeholders as missing.

    Used for ``receiver_serial`` and ``antenna_serial`` where operators and
    receivers both surface "I don't have a real value" via well-known
    sentinels (``"0000000000"``, ``"Unknown"``, ``""``).
    """
    s = _strip(value)
    if s is None:
        return None
    if s.lower() in _SERIAL_PLACEHOLDERS:
        return None
    # All-zero serials of any length (0, 00, 000000, 0000000000, …).
    if s and set(s) == {"0"}:
        return None
    return s


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


def _receiver_type_to_cfg(value: Optional[str]) -> Optional[str]:
    """Map IGS-style receiver names to the short cfg vocabulary on write.

    TOS surfaces ``"SEPT POLARX5"`` while ``stations.cfg`` (and the rest of
    the codebase) uses the short form ``"PolaRX5"``. Without this mapping,
    accepting a TOS suggestion writes an unrecognised value that breaks
    type-detection.

    Returns the canonical short form when the value matches a known
    fingerprint pattern; otherwise returns the input unchanged so unknown
    types aren't silently corrupted.
    """
    if value is None:
        return None
    try:
        from ..health.receiver_fingerprint import identify_receiver_type
    except ImportError:
        return value
    canonical = identify_receiver_type({"receiver_model": value})
    return canonical if canonical is not None else value


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


def _abs_tol(tol: float) -> Callable[[Optional[str], Optional[str]], bool]:
    """Equality with an absolute tolerance, for floats stored as strings.

    Used for position fields where receiver values come from a real-time
    PVT solution (~1-2 m accuracy) and only need to roughly match the
    surveyed coordinates in TOS to confirm "right station".
    """

    def _check(a: Optional[str], b: Optional[str]) -> bool:
        if a is None or b is None:
            return False
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return a.strip() == b.strip()

    return _check


# Default position tolerance (degrees for lat/lon, meters for height).
# At Iceland latitude (~64°): 2e-5° lat ≈ 2.2 m; 5e-5° lon ≈ 2.4 m.
# CLI passes a meters-based override; values_equal is rebuilt at call time.
DEFAULT_POSITION_TOLERANCE_M = 2.0


def _meters_to_lat_deg(meters: float) -> float:
    return meters / 111111.0  # ~1 deg latitude = 111.111 km


def _meters_to_lon_deg(meters: float, latitude_deg: float = 64.0) -> float:
    import math

    return meters / (111111.0 * max(math.cos(math.radians(latitude_deg)), 0.01))


def position_equality_for(field_key: str, tolerance_m: float) -> Callable[[Optional[str], Optional[str]], bool]:
    """Build an equality predicate for a position field at the given tolerance.

    The CLI passes a meters value; lat/lon are converted to degree tolerances
    using an Iceland-centric latitude (64°) for the longitude factor — good
    enough for a sanity check across the network.
    """
    if field_key == "latitude":
        return _abs_tol(_meters_to_lat_deg(tolerance_m))
    if field_key == "longitude":
        return _abs_tol(_meters_to_lon_deg(tolerance_m))
    if field_key == "height":
        return _abs_tol(tolerance_m)
    raise ValueError(f"position_equality_for: unsupported field {field_key!r}")


# ---------------------------------------------------------------------------
# FieldSpec
# ---------------------------------------------------------------------------


def _identity(value: Optional[str]) -> Optional[str]:
    """Default :attr:`FieldSpec.cfg_format` — write source values verbatim."""
    return value


@dataclass(frozen=True)
class FieldSpec:
    cfg_key: str
    label: str
    receiver_extract: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
    tos_extract: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
    equal: Optional[Callable[[Optional[str], Optional[str]], bool]] = None
    normalize: Callable[[Optional[str]], Optional[str]] = _strip
    # Vocabulary mapping applied at WRITE time, not comparison time. Used for
    # fields where the source vocabulary differs from cfg's (e.g. TOS uses the
    # IGS name "SEPT POLARX5"; cfg uses "PolaRX5"). Default is identity.
    cfg_format: Callable[[Optional[str]], Optional[str]] = _identity
    description: str = ""
    # If False, the receiver value is for QC/flagging only — never used as
    # an auto-fill suggestion when cfg is missing. Used for fields where the
    # receiver carries operator-typed values (antenna metadata) or PVT
    # solutions that should not overwrite surveyed coordinates from TOS.
    receiver_authoritative: bool = True

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
        cfg_format=_receiver_type_to_cfg,
        description="Receiver model/type (e.g. PolaRX5, NetR9)",
    ),
    FieldSpec(
        cfg_key="receiver_serial",
        label="Receiver Serial",
        receiver_extract=lambda identity: identity.get("serial_number"),
        tos_extract=tos_adapter.current_receiver_serial,
        normalize=_strip_placeholder,
        description="Receiver serial number",
    ),
    FieldSpec(
        cfg_key="receiver_firmware_version",
        label="Receiver Firmware",
        receiver_extract=lambda identity: identity.get("firmware_version"),
        tos_extract=tos_adapter.current_receiver_firmware,
        description="Active firmware version reported by the receiver",
    ),
    # Antenna fields — TOS is canonical; receiver values are operator-typed
    # so they're flagged on mismatch but never auto-fill cfg from receiver alone.
    FieldSpec(
        cfg_key="antenna_type",
        label="Antenna Type",
        receiver_extract=lambda identity: identity.get("antenna_type"),
        tos_extract=tos_adapter.current_antenna_model,
        receiver_authoritative=False,
        description="IGS-style antenna model code",
    ),
    FieldSpec(
        cfg_key="antenna_serial",
        label="Antenna Serial",
        receiver_extract=lambda identity: identity.get("antenna_serial"),
        tos_extract=tos_adapter.current_antenna_serial,
        normalize=_strip_placeholder,
        receiver_authoritative=False,
        description="Antenna serial number",
    ),
    FieldSpec(
        cfg_key="antenna_radome",
        label="Radome",
        receiver_extract=lambda identity: identity.get("antenna_radome"),
        tos_extract=tos_adapter.current_radome_model,
        receiver_authoritative=False,
        description="Radome model code (NONE if absent)",
    ),
    FieldSpec(
        cfg_key="antenna_height",
        label="Antenna Height",
        receiver_extract=lambda identity: (
            None
            if identity.get("antenna_height_delta") is None
            else f"{identity['antenna_height_delta']:.4f}"
        ),
        tos_extract=tos_adapter.current_antenna_height,
        equal=_approx_eq(4),
        receiver_authoritative=False,
        description="Antenna height above mark including monument offset (m)",
    ),
    # Coordinates — TOS is canonical (surveyed); receiver values come from a
    # real-time PVT solution (~1-2 m accuracy) and are used purely as a sanity
    # check that the receiver is at the expected mark. Equality tolerance is
    # rebuilt at CLI invocation from --position-tolerance-m.
    FieldSpec(
        cfg_key="latitude",
        label="Latitude",
        receiver_extract=lambda identity: (
            None
            if identity.get("latitude") is None
            else f"{float(identity['latitude']):.8f}"
        ),
        tos_extract=tos_adapter.station_latitude,
        equal=_abs_tol(_meters_to_lat_deg(DEFAULT_POSITION_TOLERANCE_M)),
        receiver_authoritative=False,
        description="Decimal-degree latitude",
    ),
    FieldSpec(
        cfg_key="longitude",
        label="Longitude",
        receiver_extract=lambda identity: (
            None
            if identity.get("longitude") is None
            else f"{float(identity['longitude']):.8f}"
        ),
        tos_extract=tos_adapter.station_longitude,
        equal=_abs_tol(_meters_to_lon_deg(DEFAULT_POSITION_TOLERANCE_M)),
        receiver_authoritative=False,
        description="Decimal-degree longitude",
    ),
    FieldSpec(
        cfg_key="height",
        label="Height",
        receiver_extract=lambda identity: (
            None
            if identity.get("height") is None
            else f"{float(identity['height']):.3f}"
        ),
        tos_extract=tos_adapter.station_height,
        equal=_abs_tol(DEFAULT_POSITION_TOLERANCE_M),
        receiver_authoritative=False,
        description="Ellipsoidal height (m)",
    ),
    FieldSpec(
        cfg_key="station_name",
        label="Station Name",
        tos_extract=tos_adapter.station_name,
        description="Long-form station name (TOS-only; receiver MarkerName carries the 4-char ID)",
    ),
]


def with_position_tolerance(tolerance_m: float) -> List[FieldSpec]:
    """Return a copy of FIELDS with position equality bound to ``tolerance_m``.

    The CLI calls this when the user passes ``--position-tolerance-m`` and
    threads the result into ``compare_station`` as ``fields=...`` is opaque to
    a tolerance knob — instead we rebuild the affected specs and let
    :func:`fields_by_key` resolve them.
    """
    overrides: Dict[str, Callable[[Optional[str], Optional[str]], bool]] = {
        "latitude": position_equality_for("latitude", tolerance_m),
        "longitude": position_equality_for("longitude", tolerance_m),
        "height": position_equality_for("height", tolerance_m),
    }
    out: List[FieldSpec] = []
    for spec in FIELDS:
        if spec.cfg_key in overrides:
            out.append(
                FieldSpec(
                    cfg_key=spec.cfg_key,
                    label=spec.label,
                    receiver_extract=spec.receiver_extract,
                    tos_extract=spec.tos_extract,
                    equal=overrides[spec.cfg_key],
                    normalize=spec.normalize,
                    cfg_format=spec.cfg_format,
                    description=spec.description,
                    receiver_authoritative=spec.receiver_authoritative,
                )
            )
        else:
            out.append(spec)
    return out


def fields_by_key() -> Dict[str, FieldSpec]:
    return {f.cfg_key: f for f in FIELDS}


def all_keys() -> List[str]:
    return [f.cfg_key for f in FIELDS]
