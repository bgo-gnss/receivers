"""
Metadata provider for RINEX header corrections.

This module provides equipment metadata for RINEX files, supporting:
1. Historical metadata lookup from TOS database (for old data)
2. Current station configuration (for recent data)

The TOS database contains time-segmented equipment sessions, allowing
accurate metadata for any observation date.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# Default threshold for using config vs TOS
DEFAULT_RECENT_DAYS = 30


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

    def to_rinex_corrections(self) -> Dict[str, str]:
        """Convert metadata to RINEX header correction dictionary.

        Returns:
            Dictionary mapping RINEX field names to corrected values
        """
        corrections = {}

        # Marker information
        if self.marker_name:
            corrections["MARKER NAME"] = self.marker_name.upper()
        if self.marker_number:
            corrections["MARKER NUMBER"] = self.marker_number

        # Observer/Agency
        corrections["OBSERVER / AGENCY"] = f"{self.observer} {self.agency}"

        # Receiver
        if self.receiver_model:
            rec_line = f"{self.receiver_serial} {self.receiver_model}"
            if self.receiver_firmware:
                rec_line += f" {self.receiver_firmware}"
            corrections["REC # / TYPE / VERS"] = rec_line

        # Antenna
        if self.antenna_model:
            # IGS convention: ANTENNA_MODEL + RADOME (20 chars each)
            ant_model = self.antenna_model
            if self.radome_model and self.radome_model != "NONE":
                ant_model = f"{self.antenna_model} {self.radome_model}"
            corrections["ANT # / TYPE"] = f"{self.antenna_serial} {ant_model}"

        # Antenna height offsets (H/E/N)
        total_height = self.antenna_height + self.monument_height
        corrections["ANTENNA: DELTA H/E/N"] = f"{total_height:.4f} 0.0000 0.0000"

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
        metadata.marker_name = rinex_config.get("marker_name", station_config.get("station_id", ""))
        metadata.marker_number = rinex_config.get("marker_number", station_config.get("station_id", ""))
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

        Args:
            station_id: Station identifier (e.g., 'ELDC')
            observation_date: Date of observation
            force_tos: Always use TOS database
            force_config: Always use current configuration

        Returns:
            EquipmentMetadata for the observation date, or None if not found
        """
        station_id = station_id.upper()

        # Determine whether to use config or TOS
        days_ago = (datetime.now() - observation_date).days
        is_recent = days_ago <= self.recent_days_threshold

        if force_config or (is_recent and not force_tos and not self.use_tos_for_historical):
            # Use current configuration
            return self._get_from_config(station_id)
        else:
            # Use TOS database for historical lookup
            metadata = self._get_from_tos(station_id, observation_date)
            if metadata is None and is_recent:
                # Fallback to config if TOS lookup fails for recent data
                self.logger.debug(f"TOS lookup failed, falling back to config for {station_id}")
                return self._get_from_config(station_id)
            return metadata

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
