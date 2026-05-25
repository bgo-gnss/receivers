"""
Septentrio SBF to RINEX converter.

This module implements RINEX conversion for Septentrio Binary Format (SBF) files
using the sbf2rin tool from RxTools.

SBF (Septentrio Binary Format) is the native format for Septentrio receivers
including PolaRX5, used throughout the Icelandic GNSS network.

Tool: sbf2rin (from Septentrio RxTools)
    - Converts SBF to RINEX 2.x, 3.x, or 4.x
    - Handles compressed input (.sbf.gz)
    - Configurable observation types

Example workflow:
    1. sbf2rin converts SBF -> RINEX
    2. MetadataProvider supplies TOS equipment metadata
    3. Header corrections applied using tostools
    4. File renamed to short/long naming convention
"""

import gzip
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .converter_base import (
    ConversionError,
    NamingConvention,
    OutputFormat,
    RawToRinexConverter,
    RinexVersion,
)


class SBFConverter(RawToRinexConverter):
    """Converter for Septentrio SBF files to RINEX format.

    Uses sbf2rin from Septentrio RxTools for conversion. Supports:
    - Compressed (.sbf.gz) and uncompressed (.sbf) input
    - RINEX versions 2.x, 3.x, and 4.x output
    - Configurable observation types

    Example:
        >>> converter = SBFConverter("ELDC")
        >>> result = converter.convert_file("ELDC202601010000a.sbf.gz")
        >>> print(result.rinex_file)
        ELDC0010.26o
    """

    def __init__(
        self,
        station_id: str,
        rinex_version: RinexVersion = RinexVersion.RINEX_3,
        output_format: Optional[OutputFormat] = None,
        naming_convention: Optional["NamingConvention"] = None,
        apply_header_corrections: bool = True,
        apply_hatanaka: Optional[bool] = None,
        compression_format=None,
        observation_types: Optional[List[str]] = None,
        loglevel: int = logging.INFO,
        session_type: Optional[str] = None,
    ):
        """Initialize SBF converter.

        Args:
            station_id: Station identifier (e.g., 'ELDC')
            rinex_version: Target RINEX version (2, 3, or 4)
            output_format: Legacy parameter (use apply_hatanaka/compression_format instead)
            naming_convention: Filename convention (SHORT or LONG).
                              If None, reads from config default_naming,
                              then falls back based on rinex_version.
            apply_header_corrections: Whether to apply TOS metadata corrections
            apply_hatanaka: Apply Hatanaka compression (None = read from config)
            compression_format: File compression format (None = read from config)
            observation_types: List of observation types to include (default: all)
            loglevel: Logging level
        """
        super().__init__(
            station_id=station_id,
            rinex_version=rinex_version,
            output_format=output_format,
            naming_convention=naming_convention,
            apply_header_corrections=apply_header_corrections,
            apply_hatanaka=apply_hatanaka,
            compression_format=compression_format,
            loglevel=loglevel,
            session_type=session_type,
        )
        self.observation_types = observation_types

    @property
    def supported_extensions(self) -> List[str]:
        """Return supported file extensions."""
        return [".sbf", ".sbf.gz", ".sbf_"]

    @property
    def converter_name(self) -> str:
        """Return converter tool name."""
        return "sbf2rin"

    def _get_required_tools(self) -> List[str]:
        """Return list of required external tools."""
        return ["sbf2rin"]

    def _run_conversion(
        self,
        raw_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """Run SBF to RINEX conversion using sbf2rin.

        Uses the simple workflow from mall_septentrio.sh:
            sbf2rin -v -f input.sbf -o output.rnxT

        Args:
            raw_file: Path to SBF file
            output_dir: Output directory for RINEX file
            observation_date: Date of observation

        Returns:
            Path to converted RINEX file (temporary, before header corrections)

        Raises:
            ConversionError: If conversion fails
        """
        # Get sbf2rin path
        sbf2rin = self.get_tool_path("sbf2rin")

        # Handle compressed files - decompress to temp if needed
        if raw_file.suffix == ".gz":
            working_file = self._decompress_to_temp(raw_file)
            temp_created = True
        else:
            working_file = raw_file
            temp_created = False

        try:
            # Generate output filename via gtimes' frequency-aware #Rin2 template
            # (see RawToRinexConverter._build_short_filename). Append 'T' suffix
            # to mark this as the pre-header-correction temp file.
            base_name = self._build_short_filename(observation_date, "o")
            temp_rinex = output_dir / f"{base_name}T"

            # Build sbf2rin command with explicit output file
            cmd = self._build_sbf2rin_command(sbf2rin, working_file, temp_rinex)

            # Run conversion
            self.logger.info(
                f"Running sbf2rin for {raw_file.name} -> {temp_rinex.name}"
            )
            self._run_subprocess(cmd, timeout=300, cwd=output_dir)

            if not temp_rinex.exists():
                # Try to find any output file sbf2rin may have created
                rinex_file = self._find_output_file(output_dir, observation_date)
                if rinex_file and rinex_file.exists():
                    return rinex_file
                raise ConversionError(
                    "sbf2rin did not produce expected output file",
                    raw_file,
                )

            return temp_rinex

        finally:
            # Clean up temp file
            if temp_created and working_file.exists():
                working_file.unlink()

    def _build_sbf2rin_command(
        self,
        sbf2rin: Path,
        input_file: Path,
        output_file: Path,
    ) -> List[str]:
        """Build sbf2rin command line.

        Uses the simple workflow from mall_septentrio.sh:
            sbf2rin -v -f input.sbf -o output.rnx

        Args:
            sbf2rin: Path to sbf2rin executable
            input_file: Input SBF file
            output_file: Output RINEX file path

        Returns:
            Command and arguments list
        """
        cmd = [str(sbf2rin)]

        # Verbose output for logging
        cmd.append("-v")

        # Input file
        cmd.extend(["-f", str(input_file.absolute())])

        # Output file (sbf2rin uses -o for output filename)
        cmd.extend(["-o", str(output_file)])

        # RINEX version (optional, sbf2rin defaults to RINEX 3.04)
        if self.rinex_version == RinexVersion.RINEX_2:
            cmd.append("-R211")
        elif self.rinex_version == RinexVersion.RINEX_4:
            cmd.append("-R4")
        # Note: RINEX 3 is the default, no flag needed

        # Observation types (if specified) - sbf2rin uses -I for include
        if self.observation_types:
            obs_str = "+".join(self.observation_types)
            cmd.extend(["-I", obs_str])

        return cmd

    def _decompress_to_temp(self, compressed_file: Path) -> Path:
        """Decompress .gz file to temporary location.

        Args:
            compressed_file: Path to .gz file

        Returns:
            Path to decompressed file

        Raises:
            ConversionError: If decompression fails
        """
        try:
            # Create temp file with .sbf extension
            temp_dir = Path(tempfile.gettempdir())
            temp_file = temp_dir / compressed_file.stem

            self.logger.debug(f"Decompressing {compressed_file.name} to {temp_file}")

            with gzip.open(compressed_file, "rb") as f_in:
                with open(temp_file, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)

            return temp_file

        except Exception as e:
            raise ConversionError(
                f"Failed to decompress {compressed_file.name}",
                compressed_file,
                str(e),
            )

    def _find_output_file(
        self,
        output_dir: Path,
        observation_date: datetime,
    ) -> Optional[Path]:
        """Find the RINEX file generated by sbf2rin.

        sbf2rin generates files with naming based on RINEX version:
        - RINEX 2: SSSS0DDF.YYo
        - RINEX 3+: SSSS00CCC_R_YYYYDDDHHMM_...

        Args:
            output_dir: Directory to search
            observation_date: Expected observation date

        Returns:
            Path to RINEX file if found, None otherwise
        """
        # Get expected patterns based on RINEX version
        day_of_year = observation_date.timetuple().tm_yday
        year = observation_date.year
        year_2digit = year % 100

        # Look for common patterns
        patterns = []

        if self.rinex_version == RinexVersion.RINEX_2:
            # Short name pattern: SSSS0DDF.YYo
            patterns.append(f"{self.station_id}{day_of_year:03d}*.{year_2digit:02d}o")
            patterns.append(
                f"{self.station_id.lower()}{day_of_year:03d}*.{year_2digit:02d}o"
            )
        else:
            # Long name pattern: SSSS00CCC_R_YYYYDDD...
            patterns.append(f"{self.station_id}*{year:04d}{day_of_year:03d}*.rnx")
            patterns.append(
                f"{self.station_id.lower()}*{year:04d}{day_of_year:03d}*.rnx"
            )

        # Also try generic patterns
        patterns.extend(
            [
                f"{self.station_id}*.o",
                f"{self.station_id}*.rnx",
                f"{self.station_id.lower()}*.o",
                f"{self.station_id.lower()}*.rnx",
            ]
        )

        # Search for matching files
        for pattern in patterns:
            matches = list(output_dir.glob(pattern))
            if matches:
                # Return most recently modified
                return max(matches, key=lambda p: p.stat().st_mtime)

        # Fallback: find any new RINEX-like file
        all_files = []
        for ext in ["*.o", "*.O", "*.rnx", "*.RNX"]:
            all_files.extend(output_dir.glob(ext))

        if all_files:
            # Return most recently modified
            return max(all_files, key=lambda p: p.stat().st_mtime)

        return None


class SBFToRinexConverter(SBFConverter):
    """Alias for backward compatibility."""

    pass
