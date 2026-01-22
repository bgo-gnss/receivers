"""
Metadata provider for RINEX header corrections.

This module provides equipment metadata for RINEX files, supporting:
1. Historical metadata lookup from TOS database (for old data)
2. Current station configuration (for recent data)

The TOS database contains time-segmented equipment sessions, allowing
accurate metadata for any observation date.

RINEX Header Field Formatting:
    RINEX uses fixed-width Fortran format. Field specifications are based on
    RINEX 3.x standard and tostools/rinex/reader.py:

    Field Name              Format          Width   Description
    -----------------------------------------------------------------
    MARKER NAME             A60             60      Station identifier
    MARKER NUMBER           A20             20      IERS DOMES number
    OBSERVER / AGENCY       A20,A40         60      Observer + Agency
    REC # / TYPE / VERS     A20,A20,A20     60      Serial + Type + Version
    ANT # / TYPE            A20,A20         40      Serial + Type (with radome)
    APPROX POSITION XYZ     3F14.4          42      ECEF X, Y, Z
    ANTENNA: DELTA H/E/N    3F14.4          42      Height, East, North offsets
    INTERVAL                F10.3           10      Sampling interval
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# Default threshold for using config vs TOS
DEFAULT_RECENT_DAYS = 30

# RINEX header field specifications: (Fortran format, total width)
# Based on RINEX 3.x spec and tostools/rinex/reader.py
RINEX_FIELD_SPECS: Dict[str, Tuple[str, int]] = {
    "MARKER NAME": ("A60", 60),
    "MARKER NUMBER": ("A20", 20),
    "OBSERVER / AGENCY": ("A20,A40", 60),  # observer(20) + agency(40)
    "REC # / TYPE / VERS": ("A20,A20,A20", 60),  # serial(20) + type(20) + vers(20)
    "ANT # / TYPE": ("A20,A20", 40),  # serial(20) + type(20)
    "APPROX POSITION XYZ": ("3F14.4", 42),
    "ANTENNA: DELTA H/E/N": ("3F14.4", 42),
    "INTERVAL": ("F10.3", 10),
}


def format_rinex_field(field_name: str, value: Any) -> Optional[str]:
    """Format a value for a specific RINEX header field.

    Uses fixed-width Fortran formatting per RINEX 3.x specification.

    Args:
        field_name: RINEX field name (e.g., "MARKER NAME", "ANT # / TYPE")
        value: Value to format. Can be:
               - str: Single value (will be parsed if multi-part field)
               - tuple/list: Multi-part value (e.g., (observer, agency))
               - float/int: Numeric value
               - None: Returns None (skip this field)

    Returns:
        Formatted string with correct RINEX column widths, or None if empty/None

    Examples:
        >>> format_rinex_field("MARKER NAME", "ELDC")
        'ELDC                                                        '  # 60 chars

        >>> format_rinex_field("OBSERVER / AGENCY", ("BGO", "IMO"))
        'BGO                 IMO                                     '  # 60 chars

        >>> format_rinex_field("ANT # / TYPE", ("CR620012345", "ASH701945C_M    SCIS"))
        'CR620012345         ASH701945C_M    SCIS'  # 40 chars
    """
    if value is None:
        return None

    if field_name == "MARKER NAME":
        v = str(value).strip()
        if not v:
            return None
        return v.upper().ljust(60)[:60]

    elif field_name == "MARKER NUMBER":
        v = str(value).strip()
        if not v:
            return None
        return v.ljust(20)[:20]

    elif field_name == "OBSERVER / AGENCY":
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            obs, agency = str(value[0]).strip(), str(value[1]).strip()
        else:
            parts = str(value).split(None, 1)
            obs = parts[0] if parts else ""
            agency = parts[1] if len(parts) > 1 else ""
        if not obs and not agency:
            return None
        return f"{obs.ljust(20)[:20]}{agency.ljust(40)[:40]}"

    elif field_name == "REC # / TYPE / VERS":
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            serial, model, version = (
                str(value[0]).strip(),
                str(value[1]).strip(),
                str(value[2]).strip(),
            )
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            serial, model, version = str(value[0]).strip(), str(value[1]).strip(), ""
        else:
            parts = str(value).split(None, 2)
            serial = parts[0] if len(parts) > 0 else ""
            model = parts[1] if len(parts) > 1 else ""
            version = parts[2] if len(parts) > 2 else ""
        if not serial and not model:
            return None
        return f"{serial.ljust(20)[:20]}{model.ljust(20)[:20]}{version.ljust(20)[:20]}"

    elif field_name == "ANT # / TYPE":
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            serial, ant_type = str(value[0]).strip(), str(value[1]).strip()
        else:
            parts = str(value).split(None, 1)
            serial = parts[0] if parts else ""
            ant_type = parts[1] if len(parts) > 1 else ""
        if not serial:
            return None
        return f"{serial.ljust(20)[:20]}{ant_type.ljust(20)[:20]}"

    elif field_name == "ANTENNA: DELTA H/E/N":
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            h, e, n = float(value[0]), float(value[1]), float(value[2])
        elif isinstance(value, (int, float)):
            # Single height value with 0.0 for E/N
            h, e, n = float(value), 0.0, 0.0
        else:
            try:
                parts = str(value).split()
                h = float(parts[0]) if len(parts) > 0 else 0.0
                e = float(parts[1]) if len(parts) > 1 else 0.0
                n = float(parts[2]) if len(parts) > 2 else 0.0
            except (ValueError, IndexError):
                return None
        return f"{h:14.4f}{e:14.4f}{n:14.4f}"

    elif field_name == "APPROX POSITION XYZ":
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            x, y, z = float(value[0]), float(value[1]), float(value[2])
        else:
            return None
        return f"{x:14.4f}{y:14.4f}{z:14.4f}"

    elif field_name == "INTERVAL":
        try:
            return f"{float(value):10.3f}"
        except (ValueError, TypeError):
            return None

    else:
        # Unknown field - return as string if not empty
        v = str(value).strip()
        return v if v else None


def format_antenna_type_with_radome(antenna_model: str, radome: str = "NONE") -> str:
    """Format antenna type with radome for ANT # / TYPE field.

    IGS convention: 15 char antenna model + space + 4 char radome = 20 chars

    Args:
        antenna_model: Antenna model (e.g., "ASH701945C_M", "SEPPOLANT_X_MF")
        radome: Radome code (e.g., "SCIS", "NONE")

    Returns:
        20-character formatted string: "ANT_MODEL       DOME"
    """
    model = antenna_model.ljust(15)[:15]
    dome = (radome or "NONE").ljust(4)[:4]
    return f"{model} {dome}"


@dataclass
class EquipmentMetadata:
    """Equipment metadata for a specific time period.

    Contains all information needed to populate RINEX header fields
    for equipment at a station during a specific time period.
    """

    # Time period
    time_from: Optional[datetime] = None
    time_to: Optional[datetime] = None

    # Receiver information
    receiver_model: str = ""
    receiver_serial: str = ""
    receiver_firmware: str = ""

    # Antenna information
    antenna_model: str = ""
    antenna_serial: str = ""
    antenna_height: float = 0.0

    # Radome information
    radome_model: str = ""

    # Monument information
    monument_height: float = 0.0

    # Station information
    marker_name: str = ""
    marker_number: str = ""
    iers_domes: str = ""

    # Observer/Agency
    observer: str = "GNSS OPERATOR"
    agency: str = "IMO"

    # Additional fields
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_rinex_corrections(
        self,
        overrides: Optional[Dict[str, Any]] = None,
        include_receiver: bool = False,
    ) -> Dict[str, str]:
        """Convert metadata to RINEX header correction dictionary.

        Generates properly formatted RINEX header corrections using fixed-width
        Fortran format per RINEX 3.x specification.

        Args:
            overrides: Optional dictionary of additional/override corrections.
                       Keys are RINEX field names, values can be:
                       - str: Single value
                       - tuple/list: Multi-part value (e.g., (observer, agency))
                       - None: Explicitly skip this field (remove from output)
                       Any field in overrides replaces the auto-generated value.

            include_receiver: If True, include REC # / TYPE / VERS from metadata.
                              Default False because converter tools (sbf2rin)
                              usually extract correct values from raw data files.

        Returns:
            Dictionary mapping RINEX field names to formatted values.
            Only fields with non-empty values are included.

        Examples:
            # Basic usage - auto-generates from metadata
            >>> corrections = metadata.to_rinex_corrections()

            # Include receiver info (if needed for tools that don't embed it)
            >>> corrections = metadata.to_rinex_corrections(include_receiver=True)

            # Override specific fields
            >>> corrections = metadata.to_rinex_corrections(
            ...     overrides={
            ...         "REC # / TYPE / VERS": ("1234567", "SEPT POLARX5", "5.6.0"),
            ...         "MARKER NAME": None,  # Skip this field
            ...     }
            ... )
        """
        # Build base values from metadata (unformatted)
        base: Dict[str, Any] = {}

        if self.marker_name:
            base["MARKER NAME"] = self.marker_name

        if self.marker_number:
            base["MARKER NUMBER"] = self.marker_number

        if self.observer or self.agency:
            base["OBSERVER / AGENCY"] = (self.observer or "", self.agency or "")

        # Receiver: only if explicitly requested
        if include_receiver and (self.receiver_serial or self.receiver_model):
            base["REC # / TYPE / VERS"] = (
                self.receiver_serial or "",
                self.receiver_model or "",
                self.receiver_firmware or "",
            )

        # Antenna: only if we have serial (to fix "Unknown" from converters)
        if self.antenna_serial:
            if self.antenna_model:
                ant_type = format_antenna_type_with_radome(
                    self.antenna_model, self.radome_model
                )
            else:
                ant_type = ""
            base["ANT # / TYPE"] = (self.antenna_serial, ant_type)

        # Antenna height (always include)
        total_height = self.antenna_height + self.monument_height
        base["ANTENNA: DELTA H/E/N"] = (total_height, 0.0, 0.0)

        # Apply overrides
        if overrides:
            for key, value in overrides.items():
                if value is None:
                    # None means explicitly skip this field
                    base.pop(key, None)
                else:
                    base[key] = value

        # Format all fields using the general formatter
        corrections = {}
        for field_name, value in base.items():
            formatted = format_rinex_field(field_name, value)
            if formatted is not None:
                corrections[field_name] = formatted

        return corrections

    @classmethod
    def from_tos_session(cls, session: Dict[str, Any]) -> "EquipmentMetadata":
        """Create EquipmentMetadata from TOS device_history session.

        The TOS API returns device_history as a list of sessions, each with:
        - time_from, time_to: Session time period
        - gnss_receiver: Receiver details
        - antenna: Antenna details
        - radome: Radome details
        - monument: Monument details

        Args:
            session: TOS device_history session dictionary

        Returns:
            EquipmentMetadata instance
        """
        metadata = cls()

        # Parse time period
        if "time_from" in session:
            if isinstance(session["time_from"], datetime):
                metadata.time_from = session["time_from"]
            elif isinstance(session["time_from"], str):
                try:
                    metadata.time_from = datetime.fromisoformat(
                        session["time_from"].replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

        if "time_to" in session:
            if isinstance(session["time_to"], datetime):
                metadata.time_to = session["time_to"]
            elif isinstance(session["time_to"], str) and session["time_to"]:
                try:
                    metadata.time_to = datetime.fromisoformat(
                        session["time_to"].replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

        # Parse receiver
        receiver = session.get("gnss_receiver", {})
        if receiver:
            metadata.receiver_model = receiver.get("model", "")
            metadata.receiver_serial = receiver.get("serial_number", "")
            metadata.receiver_firmware = receiver.get("firmware_version", "")

        # Parse antenna
        antenna = session.get("antenna", {})
        if antenna:
            metadata.antenna_model = antenna.get("model", "")
            metadata.antenna_serial = antenna.get("serial_number", "")
            metadata.antenna_height = float(antenna.get("antenna_height", 0) or 0)

        # Parse radome
        radome = session.get("radome", {})
        if radome:
            metadata.radome_model = radome.get("model", "NONE")

        # Parse monument
        monument = session.get("monument", {})
        if monument:
            metadata.monument_height = float(monument.get("monument_height", 0) or 0)

        return metadata

    @classmethod
    def from_station_config(cls, station_config: Dict[str, Any]) -> "EquipmentMetadata":
        """Create EquipmentMetadata from gps_parser station configuration.

        The station configuration now includes RINEX metadata from teqc configs:
        - station_config['rinex'] - RINEX header info (observer, agency, marker)
        - station_config['antenna'] - Antenna info (type, serial, radome, height)
        - station_config['receiver'] - Receiver info (type)

        Args:
            station_config: Station configuration from gps_parser/config_utils

        Returns:
            EquipmentMetadata instance
        """
        metadata = cls()

        # Parse validity period from config
        rinex_config = station_config.get("rinex", {})
        valid_from = rinex_config.get("config_valid_from", "")
        if valid_from:
            try:
                metadata.time_from = datetime.strptime(valid_from, "%Y-%m-%d")
            except ValueError:
                metadata.time_from = datetime.now()
        else:
            metadata.time_from = datetime.now()
        metadata.time_to = None  # Current config, no end date

        # RINEX header metadata (from teqc configs)
        metadata.marker_name = rinex_config.get(
            "marker_name", station_config.get("station_id", "")
        )
        metadata.marker_number = rinex_config.get(
            "marker_number", station_config.get("station_id", "")
        )
        metadata.observer = rinex_config.get("observer", "GNSS OPERATOR")
        metadata.agency = rinex_config.get("agency", "IMO")

        # Receiver info
        receiver = station_config.get("receiver", {})
        if receiver:
            metadata.receiver_model = receiver.get("type", "")
            metadata.receiver_serial = receiver.get("serial", "")
            metadata.receiver_firmware = receiver.get("firmware", "")

        # Antenna info (from teqc configs)
        antenna = station_config.get("antenna", {})
        if antenna:
            metadata.antenna_model = antenna.get("type", "")
            metadata.antenna_serial = antenna.get("serial", "")
            metadata.antenna_height = float(antenna.get("height", 0) or 0)
            metadata.radome_model = antenna.get("radome", "NONE")

        return metadata


class MetadataProvider:
    """Provider for RINEX header metadata.

    Handles lookup of equipment metadata for any observation date:
    - Recent dates: Use current station configuration (faster)
    - Historical dates: Query TOS database device_history

    The TOS database contains complete equipment history with time periods,
    allowing accurate metadata for old data processing.
    """

    def __init__(
        self,
        recent_days_threshold: int = DEFAULT_RECENT_DAYS,
        use_tos_for_historical: bool = True,
        loglevel: int = logging.INFO,
    ):
        """Initialize metadata provider.

        Args:
            recent_days_threshold: Days back to consider "recent" (use config)
            use_tos_for_historical: Use TOS database for old data
            loglevel: Logging level
        """
        self.recent_days_threshold = recent_days_threshold
        self.use_tos_for_historical = use_tos_for_historical
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.logger.setLevel(loglevel)

        # Cache for TOS data
        self._tos_cache: Dict[str, List[Dict]] = {}
        self._config_cache: Dict[str, EquipmentMetadata] = {}

    def get_equipment_at_date(
        self,
        station_id: str,
        observation_date: datetime,
        force_tos: bool = False,
        force_config: bool = False,
    ) -> Optional[EquipmentMetadata]:
        """Get equipment metadata for a specific observation date.

        Logic:
        1. Recent dates (within threshold): Use config (station.cfg has current data)
        2. Historical dates: Use TOS if enabled, otherwise config
        3. Fall back to config if TOS lookup fails

        Args:
            station_id: Station identifier (e.g., 'ELDC')
            observation_date: Date of observation
            force_tos: Always use TOS database (even for recent dates)
            force_config: Always use current configuration (even for old dates)

        Returns:
            EquipmentMetadata for the observation date, or None if not found
        """
        station_id = station_id.upper()

        # Determine whether date is recent
        days_ago = (datetime.now() - observation_date).days
        is_recent = days_ago <= self.recent_days_threshold

        # Priority logic:
        # 1. force_config: always use config
        # 2. force_tos: always use TOS (with config fallback)
        # 3. Recent dates: use config (station.cfg has current valid data)
        # 4. Historical dates + use_tos_for_historical: use TOS (with config fallback)
        # 5. Otherwise: use config

        if force_config:
            self.logger.debug(f"Using config for {station_id} (force_config=True)")
            return self._get_from_config(station_id)

        if force_tos:
            self.logger.debug(f"Using TOS for {station_id} (force_tos=True)")
            metadata = self._get_from_tos(station_id, observation_date)
            if metadata is None:
                self.logger.debug(
                    f"TOS lookup failed, falling back to config for {station_id}"
                )
                return self._get_from_config(station_id)
            return metadata

        if is_recent:
            # Recent dates: use config (station.cfg has current valid data)
            self.logger.debug(
                f"Using config for {station_id} ({days_ago} days ago is within "
                f"{self.recent_days_threshold} day threshold)"
            )
            return self._get_from_config(station_id)

        if self.use_tos_for_historical:
            # Historical dates: try TOS first
            self.logger.debug(
                f"Using TOS for {station_id} ({days_ago} days ago is historical)"
            )
            metadata = self._get_from_tos(station_id, observation_date)
            if metadata is None:
                self.logger.debug(
                    f"TOS lookup failed, falling back to config for {station_id}"
                )
                return self._get_from_config(station_id)
            return metadata

        # Historical dates, TOS disabled: use config
        self.logger.debug(
            f"Using config for {station_id} (TOS disabled for historical)"
        )
        return self._get_from_config(station_id)

    def _get_from_config(self, station_id: str) -> Optional[EquipmentMetadata]:
        """Get metadata from current station configuration.

        Args:
            station_id: Station identifier

        Returns:
            EquipmentMetadata or None
        """
        if station_id in self._config_cache:
            return self._config_cache[station_id]

        try:
            # Import here to avoid circular imports
            from ..config_utils import get_station_config

            config = get_station_config(station_id)
            if config is None:
                self.logger.warning(f"Station {station_id} not found in configuration")
                return None

            metadata = EquipmentMetadata.from_station_config(config)
            metadata.marker_name = station_id  # Ensure marker name is set
            self._config_cache[station_id] = metadata
            return metadata

        except Exception as e:
            self.logger.error(f"Failed to get config for {station_id}: {e}")
            return None

    def _get_from_tos(
        self,
        station_id: str,
        observation_date: datetime,
    ) -> Optional[EquipmentMetadata]:
        """Get metadata from TOS database for a specific date.

        Args:
            station_id: Station identifier
            observation_date: Date to look up equipment

        Returns:
            EquipmentMetadata or None
        """
        # Get device history (cached)
        device_history = self._get_device_history(station_id)

        if not device_history:
            return None

        # Find session that covers the observation date
        for session in device_history:
            metadata = EquipmentMetadata.from_tos_session(session)

            # Check if observation date falls within this session
            if metadata.time_from and metadata.time_from > observation_date:
                continue  # Session starts after observation

            if metadata.time_to and metadata.time_to < observation_date:
                continue  # Session ended before observation

            # Found matching session
            metadata.marker_name = station_id
            self.logger.debug(
                f"Found TOS metadata for {station_id} at {observation_date.date()}: "
                f"{metadata.receiver_model}, {metadata.antenna_model}"
            )
            return metadata

        self.logger.warning(
            f"No TOS session found for {station_id} at {observation_date.date()}"
        )
        return None

    def _get_device_history(self, station_id: str) -> List[Dict]:
        """Get device history from TOS database (cached).

        Args:
            station_id: Station identifier

        Returns:
            List of device history sessions
        """
        if station_id in self._tos_cache:
            return self._tos_cache[station_id]

        try:
            from tostools.api.tos_client import TOSClient

            client = TOSClient()
            station_data = client.get_complete_station_metadata(station_id)

            if station_data and "device_history" in station_data:
                device_history = station_data["device_history"]
                self._tos_cache[station_id] = device_history
                self.logger.info(
                    f"Loaded {len(device_history)} TOS sessions for {station_id}"
                )
                return device_history
            else:
                self.logger.warning(f"No device history found in TOS for {station_id}")
                return []

        except ImportError:
            self.logger.warning("tostools not available, cannot query TOS database")
            return []

        except Exception as e:
            self.logger.error(f"TOS query failed for {station_id}: {e}")
            return []

    def clear_cache(self) -> None:
        """Clear all cached metadata."""
        self._tos_cache.clear()
        self._config_cache.clear()

    def preload_station(self, station_id: str) -> bool:
        """Preload TOS data for a station into cache.

        Args:
            station_id: Station identifier

        Returns:
            True if data was loaded successfully
        """
        history = self._get_device_history(station_id.upper())
        return len(history) > 0
