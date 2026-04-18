"""Receiver fingerprint matching and mismatch detection.

Compares configured receiver type (from stations.cfg) against identity
data reported by the actual device during health checks. Flags mismatches
that may indicate a receiver was replaced without updating configuration.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("receivers.health.fingerprint")

# Known receiver fingerprints: patterns that identify each receiver type
RECEIVER_FINGERPRINTS: Dict[str, Dict[str, Any]] = {
    "PolaRX5": {
        "model_patterns": ["PolaRx5", "PolaRX5", "POLARX5"],
        "banner_patterns": ["Septentrio"],
        "protocol": "sbf",
    },
    "NetR9": {
        "model_patterns": ["NetR9", "NETR9"],
        "banner_patterns": [],
        "protocol": "trimble_http",
    },
    "NetRS": {
        "model_patterns": ["NetRS", "NETRS"],
        "banner_patterns": [],
        "protocol": "trimble_http",
    },
    "G10": {
        "model_patterns": ["G10", "GR10", "GR25", "Leica"],
        "banner_patterns": [],
        "protocol": "leica_http",
    },
}


def check_identity_mismatch(
    configured_type: str,
    identity_data: Dict[str, Any],
    ftp_banner: Optional[str] = None,
) -> Optional[str]:
    """Compare configured receiver type against detected identity.

    Args:
        configured_type: Receiver type from stations.cfg (e.g., "PolaRX5")
        identity_data: Identity dict with receiver_model, firmware_version,
                      serial_number as returned by health extractors
        ftp_banner: Optional FTP banner text for additional fingerprinting

    Returns:
        None if no mismatch detected, or a description string if mismatch found.
    """
    fingerprint = RECEIVER_FINGERPRINTS.get(configured_type)
    if not fingerprint:
        return None  # Unknown configured type, can't check

    detected_model = identity_data.get("receiver_model", "")
    if not detected_model:
        return None  # No model detected, nothing to compare

    # Check if detected model matches any known pattern for configured type
    model_patterns = fingerprint.get("model_patterns", [])
    model_match = any(
        pattern.lower() in detected_model.lower() for pattern in model_patterns
    )

    if model_match:
        return None  # Model matches configured type

    # Check FTP banner if available
    if ftp_banner:
        banner_patterns = fingerprint.get("banner_patterns", [])
        banner_match = any(
            pattern.lower() in ftp_banner.lower() for pattern in banner_patterns
        )
        if not banner_match and banner_patterns:
            # Banner doesn't match either — stronger mismatch signal
            return (
                f"Receiver mismatch: configured as {configured_type} but "
                f"detected model '{detected_model}' "
                f"(banner: '{ftp_banner[:80]}')"
            )

    # Model didn't match any pattern
    return (
        f"Receiver mismatch: configured as {configured_type} but "
        f"detected model '{detected_model}'"
    )


def identify_receiver_type(
    identity_data: Dict[str, Any],
    ftp_banner: Optional[str] = None,
) -> Optional[str]:
    """Attempt to identify receiver type from identity data.

    Args:
        identity_data: Identity dict from health extractor
        ftp_banner: Optional FTP banner text

    Returns:
        Best-guess receiver type string, or None if unidentifiable.
    """
    detected_model = identity_data.get("receiver_model", "")

    for rx_type, fingerprint in RECEIVER_FINGERPRINTS.items():
        model_patterns = fingerprint.get("model_patterns", [])
        if any(pattern.lower() in detected_model.lower() for pattern in model_patterns):
            return rx_type

    # Try banner matching as fallback
    if ftp_banner:
        for rx_type, fingerprint in RECEIVER_FINGERPRINTS.items():
            banner_patterns = fingerprint.get("banner_patterns", [])
            if any(
                pattern.lower() in ftp_banner.lower() for pattern in banner_patterns
            ):
                return rx_type

    return None
