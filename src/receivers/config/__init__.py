"""Configuration management for receivers package.

This module provides configuration loaders for:
- receivers.cfg: General receiver settings, archive paths, session types
- icinga.cfg: Icinga monitoring thresholds and connection settings
"""

from .receivers_config import ReceiversConfig, get_receivers_config
from .icinga_config import (
    IcingaConfig,
    IcingaThresholds,
    IcingaConnection,
    get_icinga_config,
)

__all__ = [
    # Receivers configuration
    "ReceiversConfig",
    "get_receivers_config",
    # Icinga configuration
    "IcingaConfig",
    "IcingaThresholds",
    "IcingaConnection",
    "get_icinga_config",
]
