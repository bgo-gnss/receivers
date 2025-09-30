"""Receiver factory for creating receiver instances based on type."""

import logging
from typing import Dict, Any, Optional, Type

from .receiver import BaseReceiver
from ..base.exceptions import ConfigurationError


class ReceiverFactory:
    """Factory for creating receiver instances based on configuration.

    This factory automatically discovers available receiver types and
    creates instances based on the receiver_type specified in station
    configuration.
    """

    def __init__(self):
        """Initialize factory with available receiver types."""
        self.logger = logging.getLogger(__name__)
        self._receiver_types: Dict[str, Type[BaseReceiver]] = {}
        self._discover_receiver_types()

    def _discover_receiver_types(self) -> None:
        """Dynamically discover available receiver types."""
        try:
            # Import known receiver types
            from ..septentrio.polarx5 import PolaRX5
            self._receiver_types["PolaRX5"] = PolaRX5

            # Try to import additional receiver types
            try:
                from ..trimble.netrs import NetRS
                self._receiver_types["NetRS"] = NetRS
            except ImportError:
                self.logger.debug("NetRS receiver type not available")

            try:
                from ..trimble.netr9 import NetR9
                self._receiver_types["NetR9"] = NetR9
            except ImportError:
                self.logger.debug("NetR9 receiver type not available")

            try:
                from ..leica.g10 import LeicaG10
                self._receiver_types["G10"] = LeicaG10
            except ImportError:
                self.logger.debug("G10 receiver type not available")

            self.logger.debug(f"Discovered receiver types: {list(self._receiver_types.keys())}")

        except Exception as e:
            self.logger.warning(f"Failed to discover receiver types: {e}")

    def get_available_types(self) -> Dict[str, Type[BaseReceiver]]:
        """Get all available receiver types.

        Returns:
            Dictionary mapping receiver type names to classes
        """
        return self._receiver_types.copy()

    def is_supported(self, receiver_type: str) -> bool:
        """Check if a receiver type is supported.

        Args:
            receiver_type: Type of receiver to check

        Returns:
            True if receiver type is supported, False otherwise
        """
        return receiver_type in self._receiver_types

    def _adapt_configuration(self, station_config: Dict[str, Any], receiver_type: str) -> Dict[str, Any]:
        """Adapt station configuration to expected format for receiver type.

        Args:
            station_config: Original station configuration
            receiver_type: Type of receiver being created

        Returns:
            Adapted configuration dictionary
        """
        # If config already has separate router/receiver sections, return as-is
        if "router" in station_config and "receiver" in station_config:
            return station_config

        # Adapt legacy format where everything is under 'station' key
        if "station" in station_config:
            station_data = station_config["station"]
            adapted_config = {
                "station": station_data,  # Keep original station data
                "router": {
                    "ip": station_data.get("router_ip", "")
                },
                "receiver": {
                    "type": station_data.get("receiver_type", receiver_type),
                    "httpport": int(station_data.get("receiver_httpport", 8060)),
                    "ftpport": int(station_data.get("receiver_ftpport", 21)),
                    "controlport": int(station_data.get("receiver_controlport", 28784))
                }
            }
            return adapted_config

        # Return original if no adaptation needed
        return station_config

    def create_receiver(
        self,
        station_id: str,
        station_config: Dict[str, Any]
    ) -> BaseReceiver:
        """Create receiver instance based on configuration.

        Args:
            station_id: Station identifier
            station_config: Complete station configuration

        Returns:
            Receiver instance

        Raises:
            ConfigurationError: If receiver type is unsupported or config invalid
        """
        # Try different configuration formats
        receiver_type = None

        # Format 1: receiver.type (new format)
        if "receiver" in station_config and "type" in station_config["receiver"]:
            receiver_type = station_config["receiver"]["type"]

        # Format 2: station.receiver_type (legacy format)
        elif "station" in station_config and "receiver_type" in station_config["station"]:
            receiver_type = station_config["station"]["receiver_type"]

        if not receiver_type:
            raise ConfigurationError(
                f"Missing receiver type in configuration for station {station_id}. "
                f"Expected 'receiver.type' or 'station.receiver_type'",
                station_id=station_id,
                config_field="receiver.type"
            )

        if not self.is_supported(receiver_type):
            available_types = ", ".join(self._receiver_types.keys())
            raise ConfigurationError(
                f"Unsupported receiver type '{receiver_type}' for station {station_id}. "
                f"Available types: {available_types}",
                station_id=station_id,
                config_field="receiver.type",
                actual_value=receiver_type,
                suggested_fix=f"Use one of: {available_types}"
            )

        ReceiverClass = self._receiver_types[receiver_type]

        try:
            # Adapt configuration format for receivers that expect separate router/receiver sections
            adapted_config = self._adapt_configuration(station_config, receiver_type)
            receiver = ReceiverClass(station_id, adapted_config)
            self.logger.debug(f"Created {receiver_type} receiver for station {station_id}")
            return receiver

        except Exception as e:
            raise ConfigurationError(
                f"Failed to create {receiver_type} receiver for station {station_id}: {e}",
                station_id=station_id,
                config_field="receiver",
                suggested_fix="Check station configuration completeness"
            ) from e

    def create_receiver_from_type(
        self,
        receiver_type: str,
        station_id: str,
        station_config: Dict[str, Any]
    ) -> BaseReceiver:
        """Create receiver instance from explicit type.

        Args:
            receiver_type: Explicit receiver type to create
            station_id: Station identifier
            station_config: Station configuration

        Returns:
            Receiver instance
        """
        # Override the receiver type in config
        config_copy = station_config.copy()
        if "receiver" not in config_copy:
            config_copy["receiver"] = {}
        config_copy["receiver"]["type"] = receiver_type

        return self.create_receiver(station_id, config_copy)


# Global factory instance for efficient reuse
_global_factory: Optional[ReceiverFactory] = None


def get_receiver_factory() -> ReceiverFactory:
    """Get global receiver factory instance.

    Returns:
        Shared ReceiverFactory instance
    """
    global _global_factory
    if _global_factory is None:
        _global_factory = ReceiverFactory()
    return _global_factory


def create_receiver(station_id: str, station_config: Dict[str, Any]) -> BaseReceiver:
    """Convenience function to create receiver using global factory.

    Args:
        station_id: Station identifier
        station_config: Station configuration

    Returns:
        Receiver instance
    """
    factory = get_receiver_factory()
    return factory.create_receiver(station_id, station_config)