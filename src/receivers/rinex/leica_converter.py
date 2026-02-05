"""
Leica MDB (m00) to RINEX converter.

This module implements RINEX conversion for Leica raw files (.m00)
using teqc for conversion and optional GFZRNX for format upgrades.

Leica formats:
- m00: MDB format from Leica receivers (e.g., G10)

Workflow:
    1. teqc converts m00 -> RINEX 2 observation file
    2. GFZRNX converts to RINEX 3 (if needed)
    3. MetadataProvider supplies TOS equipment metadata
    4. Header corrections applied using tostools
    5. File renamed to short/long naming convention

Note: teqc is no longer maintained by UNAVCO but remains the standard
tool for Leica MDB conversion. Alternative: mdb2rinex from Leica.
"""

import gzip
import logging
import shutil
import subprocess
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


class LeicaConverter(RawToRinexConverter):
    """Converter for Leica m00 (MDB) files to RINEX format.

    Uses teqc for initial conversion and optionally GFZRNX for RINEX 3.

    Supports:
    - Leica G10 .m00 files
    - Compressed input (.m00.gz)
    - RINEX versions 2.x and 3.x output

    Example:
        >>> converter = LeicaConverter("SKFC", rinex_version=RinexVersion.RINEX_3)
        >>> result = converter.convert_file("SKFC202601010000a.m00.gz")
        >>> print(result.rinex_file)
        SKFC00ISL_R_20260010000_01D_15S_MO.rnx.gz
    """

    def __init__(
        self,
        station_id: str,
        rinex_version: RinexVersion = RinexVersion.RINEX_3,
        output_format: OutputFormat = OutputFormat.MODERN,
        naming_convention: Optional[NamingConvention] = None,
        apply_header_corrections: bool = True,
        teqc_config: Optional[Path] = None,
        keep_intermediate: bool = False,
        loglevel: int = logging.INFO,
    ):
        """Initialize Leica converter.

        Args:
            station_id: Station identifier (e.g., 'SKFC')
            rinex_version: Target RINEX version (2 or 3)
            output_format: Output format (modern or legacy)
            naming_convention: Filename convention (SHORT or LONG).
                              If None, defaults based on rinex_version.
            apply_header_corrections: Whether to apply TOS metadata corrections
            teqc_config: Optional path to teqc configuration file
            keep_intermediate: Keep intermediate files
            loglevel: Logging level
        """
        super().__init__(
            station_id=station_id,
            rinex_version=rinex_version,
            output_format=output_format,
            naming_convention=naming_convention,
            apply_header_corrections=apply_header_corrections,
            loglevel=loglevel,
        )
        self.teqc_config = teqc_config
        self.keep_intermediate = keep_intermediate
        self._temp_files: List[Path] = []

    @property
    def supported_extensions(self) -> List[str]:
        """Return supported file extensions."""
        return [".m00", ".M00", ".m00.gz", ".M00.gz"]

    @property
    def converter_name(self) -> str:
        """Return converter tool name."""
        return "teqc"

    def _get_required_tools(self) -> List[str]:
        """Return list of required external tools."""
        tools = ["teqc"]
        if self.rinex_version == RinexVersion.RINEX_3:
            tools.append("gfzrnx")
        return tools

    def _run_conversion(
        self,
        raw_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """Run m00 to RINEX conversion.

        Workflow:
        1. Decompress .m00.gz if needed
        2. teqc converts m00 -> RINEX 2
        3. GFZRNX upgrades to RINEX 3 (if needed)

        Args:
            raw_file: Path to m00 file
            output_dir: Directory for output files
            observation_date: Date of observation

        Returns:
            Path to converted RINEX file
        """
        self._temp_files = []

        try:
            # Step 1: Decompress if needed
            working_file = self._decompress_if_needed(raw_file)

            # Step 2: Run teqc to get RINEX 2
            rinex2_file = self._run_teqc(working_file, output_dir)

            # Step 3: Convert to RINEX 3 if needed
            if self.rinex_version == RinexVersion.RINEX_3:
                final_obs = self._convert_to_rinex3(rinex2_file, output_dir)
            else:
                final_obs = rinex2_file

            return final_obs

        finally:
            # Clean up temp files
            if not self.keep_intermediate:
                for temp_file in self._temp_files:
                    if temp_file.exists() and temp_file.is_file():
                        temp_file.unlink()
                        self.logger.debug(f"Removed temp file: {temp_file}")
                    elif temp_file.exists() and temp_file.is_dir():
                        shutil.rmtree(temp_file)
                        self.logger.debug(f"Removed temp dir: {temp_file}")

    def _decompress_if_needed(self, raw_file: Path) -> Path:
        """Decompress .m00.gz file if needed.

        Args:
            raw_file: Input file (possibly compressed)

        Returns:
            Path to uncompressed file
        """
        if raw_file.suffix.lower() == '.gz':
            # Create temp file for decompressed data
            temp_dir = Path(tempfile.mkdtemp(prefix="leica_"))
            decompressed = temp_dir / raw_file.stem

            self.logger.debug(f"Decompressing {raw_file} to {decompressed}")

            with gzip.open(raw_file, 'rb') as f_in:
                with open(decompressed, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)

            self._temp_files.append(decompressed)
            self._temp_files.append(temp_dir)
            return decompressed

        return raw_file

    def _run_teqc(self, m00_file: Path, output_dir: Path) -> Path:
        """Run teqc to convert m00 to RINEX.

        Args:
            m00_file: Input m00 file
            output_dir: Output directory

        Returns:
            Path to RINEX observation file

        Raises:
            ConversionError: If teqc fails
        """
        # Generate output filename (RINEX 2 style: SSSSdddh.YYo)
        # We'll use a temp name and rename later
        temp_obs = output_dir / f"{m00_file.stem}.obs"

        # Build teqc command
        cmd = ["teqc"]

        # Add config file if specified
        if self.teqc_config and self.teqc_config.exists():
            cmd.extend(["-config", str(self.teqc_config)])

        # Add error output
        cmd.extend(["+err", "/dev/null"])

        # Input file
        cmd.append(str(m00_file))

        self.logger.info(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
            )

            if result.returncode != 0:
                raise ConversionError(
                    f"teqc failed with code {result.returncode}: {result.stderr}"
                )

            # teqc outputs to stdout, write to file
            with open(temp_obs, 'w') as f:
                f.write(result.stdout)

            if not temp_obs.exists() or temp_obs.stat().st_size == 0:
                raise ConversionError(
                    f"teqc produced no output for {m00_file}"
                )

            self.logger.info(f"Created RINEX 2: {temp_obs}")
            self._temp_files.append(temp_obs)
            return temp_obs

        except subprocess.TimeoutExpired:
            raise ConversionError(f"teqc timed out converting {m00_file}")
        except FileNotFoundError:
            raise ConversionError(
                "teqc not found. Install from UNAVCO or use mdb2rinex."
            )

    def _convert_to_rinex3(self, rinex2_file: Path, output_dir: Path) -> Path:
        """Convert RINEX 2 to RINEX 3 using GFZRNX.

        Args:
            rinex2_file: RINEX 2 observation file
            output_dir: Output directory

        Returns:
            Path to RINEX 3 file

        Raises:
            ConversionError: If conversion fails
        """
        rinex3_file = output_dir / f"{rinex2_file.stem}.rnx"

        cmd = [
            "gfzrnx",
            "-finp", str(rinex2_file),
            "-fout", str(rinex3_file),
            "-vo", "3",  # Output RINEX version 3
            "-f",  # Force overwrite
        ]

        self.logger.info(f"Converting to RINEX 3: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                self.logger.warning(
                    f"GFZRNX failed, keeping RINEX 2: {result.stderr}"
                )
                return rinex2_file

            if rinex3_file.exists():
                self.logger.info(f"Created RINEX 3: {rinex3_file}")
                return rinex3_file
            else:
                self.logger.warning("GFZRNX produced no output, keeping RINEX 2")
                return rinex2_file

        except FileNotFoundError:
            self.logger.warning("gfzrnx not found, keeping RINEX 2 output")
            return rinex2_file
        except subprocess.TimeoutExpired:
            self.logger.warning("gfzrnx timed out, keeping RINEX 2 output")
            return rinex2_file


class G10Converter(LeicaConverter):
    """Specialized converter for Leica G10 receivers.

    G10 receivers produce .m00 files with specific characteristics.
    """

    @property
    def supported_extensions(self) -> List[str]:
        """G10 uses m00 format."""
        return [".m00", ".M00", ".m00.gz", ".M00.gz"]
