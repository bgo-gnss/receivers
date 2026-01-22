"""
Trimble T02/T00 to RINEX converter.

This module implements RINEX conversion for Trimble raw files (.T02, .T00)
using runpkr00 for extraction and GFZRNX for format conversion.

Trimble formats:
- T02: NetR9 raw format (newer)
- T00: NetRS raw format (older)

Workflow:
    1. runpkr00 extracts T02/T00 -> TGD (RINEX-like intermediate)
    2. GFZRNX converts TGD -> RINEX 3 (if needed)
    3. MetadataProvider supplies TOS equipment metadata
    4. Header corrections applied using tostools
    5. File renamed to short/long naming convention

Note: Since teqc is deprecated and cannot produce RINEX 3, we use GFZRNX
from GFZ Potsdam as a replacement for format conversion.
"""

import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .converter_base import (
    ConversionError,
    OutputFormat,
    RawToRinexConverter,
    RinexVersion,
)


class TrimbleConverter(RawToRinexConverter):
    """Converter for Trimble T02/T00 files to RINEX format.

    Uses runpkr00 for initial extraction and GFZRNX for RINEX 3 conversion.

    Supports:
    - NetR9 .T02 files
    - NetRS .T00 files
    - RINEX versions 2.x and 3.x output

    Example:
        >>> converter = TrimbleConverter("MANA", rinex_version=RinexVersion.RINEX_3)
        >>> result = converter.convert_file("MANA202601010000a.T02")
        >>> print(result.rinex_file)
        MANA00ISL_R_20260010000_01D_15S_MO.rnx.gz
    """

    def __init__(
        self,
        station_id: str,
        rinex_version: RinexVersion = RinexVersion.RINEX_3,
        output_format: OutputFormat = OutputFormat.MODERN,
        apply_header_corrections: bool = True,
        keep_intermediate: bool = False,
        loglevel: int = logging.INFO,
    ):
        """Initialize Trimble converter.

        Args:
            station_id: Station identifier (e.g., 'MANA')
            rinex_version: Target RINEX version (2 or 3)
            output_format: Output format (modern or legacy)
            apply_header_corrections: Whether to apply TOS metadata corrections
            keep_intermediate: Keep intermediate .tgd files
            loglevel: Logging level
        """
        super().__init__(
            station_id=station_id,
            rinex_version=rinex_version,
            output_format=output_format,
            apply_header_corrections=apply_header_corrections,
            loglevel=loglevel,
        )
        self.keep_intermediate = keep_intermediate
        self._temp_files: List[Path] = []

    @property
    def supported_extensions(self) -> List[str]:
        """Return supported file extensions."""
        return [".t02", ".T02", ".t00", ".T00"]

    @property
    def converter_name(self) -> str:
        """Return converter tool name."""
        return "runpkr00"

    def _get_required_tools(self) -> List[str]:
        """Return list of required external tools."""
        tools = ["runpkr00"]
        if self.rinex_version.value >= 3:
            tools.append("gfzrnx")
        return tools

    def _run_conversion(
        self,
        raw_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """Run T02/T00 to RINEX conversion.

        Workflow:
        1. runpkr00 extracts T02/T00 -> .tgd (RINEX 2-like)
        2. If RINEX 3 requested, GFZRNX converts tgd -> .rnx

        Args:
            raw_file: Path to T02/T00 file
            output_dir: Output directory for RINEX file
            observation_date: Date of observation

        Returns:
            Path to converted RINEX file

        Raises:
            ConversionError: If conversion fails
        """
        try:
            # Step 1: Extract with runpkr00
            tgd_file = self._run_runpkr00(raw_file, output_dir)

            # Step 2: Convert to final RINEX version
            if self.rinex_version.value >= 3:
                # Use GFZRNX for RINEX 3 conversion
                rinex_file = self._run_gfzrnx(tgd_file, output_dir, observation_date)
            else:
                # RINEX 2: rename tgd to proper extension
                rinex_file = self._rename_tgd_to_rinex2(tgd_file, observation_date)

            # Clean up intermediate files
            if not self.keep_intermediate:
                self._cleanup_temp_files()

            return rinex_file

        except Exception as e:
            # Clean up on error too
            self._cleanup_temp_files()
            if isinstance(e, ConversionError):
                raise
            raise ConversionError(str(e), raw_file)

    def _run_runpkr00(self, raw_file: Path, output_dir: Path) -> Path:
        """Run runpkr00 to extract T02/T00 to TGD format.

        Args:
            raw_file: Input T02/T00 file
            output_dir: Output directory

        Returns:
            Path to extracted .tgd file

        Raises:
            ConversionError: If extraction fails
        """
        runpkr00 = self.get_tool_path("runpkr00")

        # Determine output filename (runpkr00 generates .tgd)
        tgd_file = output_dir / (raw_file.stem + ".tgd")

        # Build command
        # runpkr00 -g -d -s <input> <output_dir>
        # -g: Generate GPS observation file
        # -d: Generate RINEX 2 format
        # -s: Silent mode
        cmd = [
            str(runpkr00),
            "-g",     # GPS obs file
            "-d",     # RINEX 2 format
            str(raw_file),
            "-o",
            str(output_dir),
        ]

        self.logger.info(f"Running runpkr00 for {raw_file.name}")
        self._run_subprocess(cmd, timeout=300, cwd=output_dir)

        # Find output file (runpkr00 naming can vary)
        if tgd_file.exists():
            self._temp_files.append(tgd_file)
            return tgd_file

        # Try to find any .tgd file
        tgd_files = list(output_dir.glob("*.tgd"))
        if tgd_files:
            tgd_file = tgd_files[0]
            self._temp_files.append(tgd_file)
            return tgd_file

        # Also check for .obs files (alternative output)
        obs_files = list(output_dir.glob(f"{raw_file.stem}*.obs"))
        if obs_files:
            return obs_files[0]

        raise ConversionError(
            "runpkr00 did not produce expected .tgd output",
            raw_file,
        )

    def _run_gfzrnx(
        self,
        tgd_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """Run GFZRNX to convert to RINEX 3 format.

        Args:
            tgd_file: Input TGD/RINEX 2 file
            output_dir: Output directory
            observation_date: Date of observation

        Returns:
            Path to RINEX 3 file

        Raises:
            ConversionError: If conversion fails
        """
        gfzrnx = self.get_tool_path("gfzrnx")

        # Build output filename
        day_of_year = observation_date.timetuple().tm_yday
        year = observation_date.year

        # GFZRNX output naming
        output_name = f"{self.station_id.lower()}00isl_R_{year:04d}{day_of_year:03d}0000_01D_15S_MO.rnx"
        rinex_file = output_dir / output_name

        # Build command
        # gfzrnx -finp <input> -fout <output> [-kv] [-vo 3]
        cmd = [
            str(gfzrnx),
            "-finp", str(tgd_file),
            "-fout", str(rinex_file),
            "-vo", "3",        # Output RINEX version 3
            "-kv",             # Keep version/format info
        ]

        self.logger.info(f"Running GFZRNX for RINEX 3 conversion")
        self._run_subprocess(cmd, timeout=300)

        if rinex_file.exists():
            return rinex_file

        # Check for alternative output patterns
        patterns = [
            f"{self.station_id}*.rnx",
            f"{self.station_id.lower()}*.rnx",
            "*.rnx",
        ]

        for pattern in patterns:
            matches = list(output_dir.glob(pattern))
            if matches:
                return max(matches, key=lambda p: p.stat().st_mtime)

        raise ConversionError(
            "GFZRNX did not produce expected RINEX 3 output",
            tgd_file,
        )

    def _rename_tgd_to_rinex2(
        self,
        tgd_file: Path,
        observation_date: datetime,
    ) -> Path:
        """Rename TGD file to proper RINEX 2 naming.

        Args:
            tgd_file: Input TGD file
            observation_date: Date of observation

        Returns:
            Path to renamed file
        """
        day_of_year = observation_date.timetuple().tm_yday
        year_2digit = observation_date.year % 100

        # RINEX 2 naming: SSSS0DDF.YYo
        rinex_name = f"{self.station_id}{day_of_year:03d}0.{year_2digit:02d}o"
        rinex_file = tgd_file.parent / rinex_name

        if tgd_file != rinex_file:
            shutil.copy(tgd_file, rinex_file)

        return rinex_file

    def _cleanup_temp_files(self) -> None:
        """Clean up intermediate files."""
        for temp_file in self._temp_files:
            try:
                if temp_file.exists():
                    temp_file.unlink()
                    self.logger.debug(f"Cleaned up {temp_file.name}")
            except Exception as e:
                self.logger.warning(f"Could not clean up {temp_file}: {e}")

        self._temp_files.clear()


class NetR9Converter(TrimbleConverter):
    """Specialized converter for NetR9 T02 files.

    Inherits from TrimbleConverter with NetR9-specific defaults.
    """

    @property
    def supported_extensions(self) -> List[str]:
        """Return supported file extensions for NetR9."""
        return [".t02", ".T02"]


class NetRSConverter(TrimbleConverter):
    """Specialized converter for NetRS T00 files.

    Inherits from TrimbleConverter with NetRS-specific defaults.
    """

    @property
    def supported_extensions(self) -> List[str]:
        """Return supported file extensions for NetRS."""
        return [".t00", ".T00"]
