"""
RINEX conversion module for GPS receiver data.

This module provides converters for converting raw GPS receiver data (SBF, T02, T00)
to RINEX format with proper header metadata from TOS database.

Main Components:
    - RawToRinexConverter: Abstract base class for all converters
    - SBFConverter: Septentrio SBF -> RINEX (using sbf2rin)
    - TrimbleConverter: Trimble T02/T00 -> RINEX (using runpkr00 + GFZRNX)
    - MetadataProvider: Historical metadata lookup from TOS database
    - RinexNamer: Short/long RINEX filename conventions
"""

from .converter_base import (
    RawToRinexConverter,
    ConversionResult,
    ConversionError,
    BatchConversionResult,
    RinexVersion,
    OutputFormat,
)
from .metadata_provider import MetadataProvider, EquipmentMetadata
from .rinex_namer import RinexNamer, NamingConvention
from .sbf_converter import SBFConverter
from .trimble_converter import TrimbleConverter, NetR9Converter, NetRSConverter

__all__ = [
    # Base classes
    "RawToRinexConverter",
    "ConversionResult",
    "ConversionError",
    "BatchConversionResult",
    "RinexVersion",
    "OutputFormat",
    # Metadata
    "MetadataProvider",
    "EquipmentMetadata",
    # Naming
    "RinexNamer",
    "NamingConvention",
    # Converters
    "SBFConverter",
    "TrimbleConverter",
    "NetR9Converter",
    "NetRSConverter",
]
