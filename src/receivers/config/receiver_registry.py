"""Centralized receiver capability registry.

Single source of truth for receiver-type knowledge: file extensions,
converter classes, supported sessions. Replaces scattered if/elif chains
across the codebase.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiverCapability:
    """Capabilities and metadata for a receiver type."""

    raw_extension: str
    """Primary raw file extension (e.g., '.sbf.gz', '.T02')."""

    raw_extensions: tuple[str, ...]
    """All valid raw extensions for glob matching."""

    rinex_converter: str | None
    """Dotted path to converter class relative to receivers package.
    Example: 'rinex.sbf_converter.SBFConverter'"""

    sessions: frozenset[str]
    """Supported session types (e.g., {'15s_24hr', '1Hz_1hr'})."""


REGISTRY: dict[str, ReceiverCapability] = {
    "polarx5": ReceiverCapability(
        raw_extension=".sbf.gz",
        raw_extensions=(".sbf", ".sbf.gz"),
        rinex_converter="rinex.sbf_converter.SBFConverter",
        sessions=frozenset({"15s_24hr", "1Hz_1hr", "status_1hr"}),
    ),
    "netr9": ReceiverCapability(
        raw_extension=".T02",
        raw_extensions=(".T02", ".T02.gz", ".t02"),
        rinex_converter="rinex.trimble_converter.NetR9Converter",
        sessions=frozenset({"15s_24hr", "1Hz_1hr"}),
    ),
    "netr5": ReceiverCapability(
        raw_extension=".T02",
        raw_extensions=(".T02", ".T02.gz", ".t02"),
        rinex_converter="rinex.trimble_converter.NetR9Converter",
        sessions=frozenset({"15s_24hr", "1Hz_1hr"}),
    ),
    "netrs": ReceiverCapability(
        raw_extension=".T00",
        raw_extensions=(".T00", ".T00.gz", ".t00"),
        rinex_converter="rinex.trimble_converter.NetRSConverter",
        sessions=frozenset({"15s_24hr", "1Hz_1hr"}),
    ),
    "g10": ReceiverCapability(
        raw_extension=".m00.gz",
        raw_extensions=(".m00", ".M00", ".m00.gz"),
        rinex_converter="rinex.leica_converter.G10Converter",
        sessions=frozenset({"15s_24hr", "1Hz_1hr"}),
    ),
}


def get_capability(receiver_type: str) -> ReceiverCapability | None:
    """Look up capability for a receiver type (case-insensitive).

    Also handles substring variants like 'PolaRX5' or 'Septentrio PolaRx5'.
    """
    if not receiver_type:
        return None
    key = receiver_type.strip().lower()
    # Direct match
    if key in REGISTRY:
        return REGISTRY[key]
    # Substring match for common variants (e.g., 'polarx5e', 'septentrio')
    if "polarx" in key or "septentrio" in key:
        return REGISTRY["polarx5"]
    if "netr9" in key:
        return REGISTRY["netr9"]
    if "netr5" in key:
        return REGISTRY["netr5"]
    if "netrs" in key:
        return REGISTRY["netrs"]
    if "g10" in key or "gr10" in key or "leica" in key:
        return REGISTRY["g10"]
    return None


def get_raw_extension(receiver_type: str) -> str:
    """Get primary raw file extension for a receiver type.

    Returns '.sbf.gz' as default if receiver type is unknown.
    """
    cap = get_capability(receiver_type)
    if cap is not None:
        return cap.raw_extension
    return ".sbf.gz"


def has_rinex_converter(receiver_type: str) -> bool:
    """Check if a receiver type has a RINEX converter available."""
    cap = get_capability(receiver_type)
    return cap is not None and cap.rinex_converter is not None


def get_converter_class(receiver_type: str) -> type | None:
    """Dynamically import and return the converter class for a receiver type.

    For Trimble receivers (NetR9/NetRS), checks receivers.cfg for
    ``use_native_trimble = true`` and returns TrimbleNativeConverter
    when Docker is available.  Falls back to the standard converter
    (runpkr00-based) otherwise.

    Returns None if receiver type is unknown or import fails.
    """
    cap = get_capability(receiver_type)
    if cap is None or cap.rinex_converter is None:
        return None

    # Prefer native Trimble converter when configured and available
    if _is_trimble_type(receiver_type) and _should_use_native_trimble():
        native_cls = _try_import_native_trimble()
        if native_cls is not None:
            return native_cls

    module_path, class_name = cap.rinex_converter.rsplit(".", 1)
    full_module = f"receivers.{module_path}"

    try:
        module = importlib.import_module(full_module)
        return getattr(module, class_name)
    except (ImportError, AttributeError) as e:
        logger.debug(f"Could not import converter {cap.rinex_converter}: {e}")
        return None


def _is_trimble_type(receiver_type: str) -> bool:
    """Check if a receiver type is a Trimble variant."""
    key = receiver_type.strip().lower()
    return "netr9" in key or "netr5" in key or "netrs" in key


def _should_use_native_trimble() -> bool:
    """Check if receivers.cfg has use_native_trimble = true."""
    try:
        from .receivers_config import get_receivers_config
        config = get_receivers_config()
        rinex_cfg = config.get_rinex_config()
        return rinex_cfg.get("use_native_trimble", False)
    except Exception:
        return False


def _try_import_native_trimble() -> type | None:
    """Import TrimbleNativeConverter and check Docker availability."""
    try:
        from ..rinex.trimble_native_converter import TrimbleNativeConverter
        if TrimbleNativeConverter.is_available():
            return TrimbleNativeConverter
        logger.debug("Native Trimble converter configured but Docker not available")
        return None
    except ImportError:
        return None


def get_convertible_receiver_types() -> list[str]:
    """Return list of receiver type keys that have RINEX converters."""
    return [key for key, cap in REGISTRY.items() if cap.rinex_converter is not None]
