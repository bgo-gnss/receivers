"""
RINEX conversion module for GPS receiver data.

This module provides converters for converting raw GPS receiver data (SBF, T02, T00, m00)
to RINEX format with proper header metadata from TOS database.

Main Components:
    - RawToRinexConverter: Abstract base class for all converters
    - SBFConverter: Septentrio SBF -> RINEX (using sbf2rin)
    - TrimbleConverter: Trimble T02/T00 -> RINEX (using runpkr00 + GFZRNX)
    - LeicaConverter: Leica m00 -> RINEX (using teqc + GFZRNX)
    - MetadataProvider: Historical metadata lookup from TOS database
    - RinexNamer: Short/long RINEX filename conventions
"""

from .converter_base import (
    BatchConversionResult,
    ConversionError,
    ConversionResult,
    OutputFormat,
    RawToRinexConverter,
    RinexVersion,
)
from .metadata_provider import (
    RINEX_FIELD_SPECS,
    EquipmentMetadata,
    MetadataProvider,
    format_antenna_type_with_radome,
    format_rinex_field,
)
from .rinex_namer import NamingConvention, RinexNamer
from .leica_converter import G10Converter, LeicaConverter
from .sbf_converter import SBFConverter
from .trimble_converter import NetR9Converter, NetRSConverter, TrimbleConverter

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
    # RINEX formatting utilities
    "format_rinex_field",
    "format_antenna_type_with_radome",
    "RINEX_FIELD_SPECS",
    # Naming
    "RinexNamer",
    "NamingConvention",
    # Converters
    "SBFConverter",
    "TrimbleConverter",
    "NetR9Converter",
    "NetRSConverter",
    "LeicaConverter",
    "G10Converter",
]
