"""Helpers to extract current values from a TOS station record.

A TOS station record (as returned by
``TOSClient.get_complete_station_metadata``) carries a ``device_history``
list with one entry per session. The *current* session is the one whose
``time_to`` is ``None``. These helpers find that session and pull values
out of the nested device dicts.

Each ``current_*`` helper returns ``None`` when the field is missing, so
callers can use the absence of a value as a signal that TOS has nothing
to say about that field.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def current_session(station: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the current open session, or ``None`` if there isn't one."""
    history = station.get("device_history") or []
    for session in reversed(history):
        if session.get("time_to") is None:
            return session
    return None


def _device_field(station: Dict[str, Any], device: str, field: str) -> Optional[str]:
    session = current_session(station)
    if not session:
        return None
    dev = session.get(device)
    if not isinstance(dev, dict):
        return None
    val = dev.get(field)
    if val is None or val == "":
        return None
    return str(val)


def current_receiver_model(station: Dict[str, Any]) -> Optional[str]:
    return _device_field(station, "gnss_receiver", "model")


def current_receiver_serial(station: Dict[str, Any]) -> Optional[str]:
    return _device_field(station, "gnss_receiver", "serial_number")


def current_receiver_firmware(station: Dict[str, Any]) -> Optional[str]:
    return _device_field(station, "gnss_receiver", "firmware_version")


def current_antenna_model(station: Dict[str, Any]) -> Optional[str]:
    return _device_field(station, "antenna", "model")


def current_antenna_serial(station: Dict[str, Any]) -> Optional[str]:
    return _device_field(station, "antenna", "serial_number")


def current_radome_model(station: Dict[str, Any]) -> Optional[str]:
    """Return current radome model. ``NONE`` if no radome session is active."""
    val = _device_field(station, "radome", "model")
    if val is None:
        # Per existing convention in the codebase, absence of a radome session
        # implies NONE rather than "unknown".
        return "NONE"
    return val


def _antenna_composite(
    station: Dict[str, Any],
    antenna_key: str,
    monument_key: str,
) -> Optional[str]:
    """Return antenna_key + monument_key as a 4-decimal string, or None."""
    session = current_session(station)
    if not session:
        return None
    antenna = session.get("antenna") or {}
    av = antenna.get(antenna_key)
    if av is None:
        return None
    monument = session.get("monument") or {}
    mv = monument.get(monument_key) or 0.0
    try:
        composite = float(av) + float(mv)
    except (TypeError, ValueError):
        return None
    return f"{composite:.4f}"


def current_antenna_height(station: Dict[str, Any]) -> Optional[str]:
    """Composite antenna height: antenna.antenna_height + monument.monument_height."""
    return _antenna_composite(station, "antenna_height", "monument_height")


def current_antenna_east(station: Dict[str, Any]) -> Optional[str]:
    """Composite East offset: antenna.antenna_offset_east + monument.monument_offset_east."""
    return _antenna_composite(station, "antenna_offset_east", "monument_offset_east")


def current_antenna_north(station: Dict[str, Any]) -> Optional[str]:
    """Composite North offset: antenna.antenna_offset_north + monument.monument_offset_north."""
    return _antenna_composite(station, "antenna_offset_north", "monument_offset_north")


def station_latitude(station: Dict[str, Any]) -> Optional[str]:
    val = station.get("lat")
    if val in (None, 0, 0.0, "", "0", "0.0"):
        return None
    try:
        return f"{float(val):.6f}"
    except (TypeError, ValueError):
        return None


def station_longitude(station: Dict[str, Any]) -> Optional[str]:
    val = station.get("lon")
    if val in (None, 0, 0.0, "", "0", "0.0"):
        return None
    try:
        return f"{float(val):.6f}"
    except (TypeError, ValueError):
        return None


def station_height(station: Dict[str, Any]) -> Optional[str]:
    val = station.get("altitude")
    if val in (None, "", 0, 0.0, "0", "0.0"):
        return None
    try:
        return f"{float(val):.2f}"
    except (TypeError, ValueError):
        return None


def station_name(station: Dict[str, Any]) -> Optional[str]:
    val = station.get("name")
    if val in (None, ""):
        return None
    return str(val)
