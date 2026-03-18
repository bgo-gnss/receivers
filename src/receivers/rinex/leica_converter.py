"""
Leica MDB (m00) to RINEX converter.

This module implements RINEX conversion for Leica raw files (.m00)
supporting two conversion backends:

1. mdb2rinex (preferred) - Leica's official converter
   - Outputs native RINEX 3 directly
   - Available from Leica myWorld portal
   - Recommended for production use

2. teqc + gfzrnx (fallback) - UNAVCO legacy tool
   - teqc converts m00 -> RINEX 2
   - gfzrnx upgrades RINEX 2 -> RINEX 3 (reformatting only)
   - Fallback when mdb2rinex is not available

Leica formats:
- m00: MDB format from Leica receivers (e.g., GR10, GR25, GR30)

Workflow with mdb2rinex:
    1. mdb2rinex converts m00 -> RINEX 3 observation file (native)
    2. MetadataProvider supplies TOS equipment metadata
    3. Header corrections applied using tostools
    4. File renamed to short/long naming convention

Workflow with teqc (fallback):
    1. teqc converts m00 -> RINEX 2 observation file
    2. GFZRNX converts to RINEX 3 (reformatting only)
    3. MetadataProvider supplies TOS equipment metadata
    4. Header corrections applied using tostools
    5. File renamed to short/long naming convention
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
    RawToRinexConverter,
    RinexVersion,
)


class LeicaConverter(RawToRinexConverter):
    """Converter for Leica m00 (MDB) files to RINEX format.

    Prefers mdb2rinex (native RINEX 3) when available, falls back to
    teqc + gfzrnx (RINEX 2 + reformatting) if not.

    Supports:
    - Leica GR10, GR25, GR30, GR50 .m00 files
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
        naming_convention: Optional[NamingConvention] = None,
        apply_header_corrections: bool = True,
        apply_hatanaka: Optional[bool] = None,
        compression_format=None,
        teqc_config: Optional[Path] = None,
        keep_intermediate: bool = False,
        loglevel: int = logging.INFO,
    ):
        """Initialize Leica converter.

        Args:
            station_id: Station identifier (e.g., 'SKFC')
            rinex_version: Target RINEX version (2 or 3)
            naming_convention: Filename convention (SHORT or LONG).
                              If None, defaults based on rinex_version.
            apply_header_corrections: Whether to apply TOS metadata corrections
            apply_hatanaka: Apply Hatanaka compression (None = read from config)
            compression_format: File compression format (None = read from config)
            teqc_config: Optional path to teqc configuration file (for teqc fallback)
            keep_intermediate: Keep intermediate files
            loglevel: Logging level
        """
        super().__init__(
            station_id=station_id,
            rinex_version=rinex_version,
            naming_convention=naming_convention,
            apply_header_corrections=apply_header_corrections,
            apply_hatanaka=apply_hatanaka,
            compression_format=compression_format,
            loglevel=loglevel,
        )
        self.teqc_config = teqc_config
        self.keep_intermediate = keep_intermediate
        self._temp_files: List[Path] = []
        self._use_mdb2rinex: Optional[bool] = None  # Determined at runtime

    @property
    def supported_extensions(self) -> List[str]:
        """Return supported file extensions."""
        return [".m00", ".M00", ".m00.gz", ".M00.gz"]

    @property
    def converter_name(self) -> str:
        """Return converter tool name."""
        if self._use_mdb2rinex:
            return "mdb2rinex"
        return "teqc"

    def _get_required_tools(self) -> List[str]:
        """Return list of required external tools.

        Checks for mdb2rinex first (preferred), falls back to teqc.
        """
        # Check if mdb2rinex is available (preferred)
        try:
            self.get_tool_path("mdb2rinex")
            self._use_mdb2rinex = True
            return ["mdb2rinex"]
        except ConversionError:
            pass

        # Fall back to teqc + gfzrnx
        self._use_mdb2rinex = False
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

        Uses mdb2rinex if available (native RINEX 3), otherwise falls back
        to teqc + gfzrnx (RINEX 2 + reformatting).

        Args:
            raw_file: Path to m00 file
            output_dir: Directory for output files
            observation_date: Date of observation

        Returns:
            Path to converted RINEX file
        """
        self._temp_files = []

        # Determine which backend to use
        if self._use_mdb2rinex is None:
            self._get_required_tools()  # Sets self._use_mdb2rinex

        try:
            # Step 1: Decompress if needed
            working_file = self._decompress_if_needed(raw_file)

            # Step 2: Convert using preferred backend
            if self._use_mdb2rinex:
                self.logger.info("Using mdb2rinex (native RINEX 3)")
                final_obs = self._run_mdb2rinex(working_file, output_dir, observation_date)
                # mdb2rinex also creates nav files - remove them (we only want obs)
                self._cleanup_nav_files(output_dir)
            else:
                self.logger.info("Using teqc + gfzrnx (RINEX 2 fallback)")
                # Run teqc to get RINEX 2
                rinex2_file = self._run_teqc(working_file, output_dir)

                # Convert to RINEX 3 if needed
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

    def _run_mdb2rinex(
        self, m00_file: Path, output_dir: Path, observation_date: datetime
    ) -> Path:
        """Run mdb2rinex to convert m00 to RINEX 3/4.

        mdb2rinex is Leica's official converter that outputs native RINEX 3/4.

        Command-line usage (from Mdb2Rinex_ReadMe.txt):
            mdb2rinex -f input.m00 -o output_dir [-r rinex3.04|rinex4.00]

        Args:
            m00_file: Input m00 file
            output_dir: Output directory
            observation_date: Date of observation (unused, mdb2rinex names files automatically)

        Returns:
            Path to converted RINEX observation file

        Raises:
            ConversionError: If mdb2rinex fails
        """
        mdb2rinex_path = self.get_tool_path("mdb2rinex")

        # Determine RINEX version for output
        # mdb2rinex supports rinex3.04 (default) and rinex4.00
        if self.rinex_version == RinexVersion.RINEX_3:
            version_arg = "rinex3.04"
        elif self.rinex_version == RinexVersion.RINEX_4:
            version_arg = "rinex4.00"
        else:
            # RINEX 2 not supported by mdb2rinex, use default (3.04)
            version_arg = "rinex3.04"
            self.logger.warning(
                "mdb2rinex doesn't support RINEX 2, using RINEX 3.04"
            )

        # Build mdb2rinex command
        # Usage: mdb2rinex -f filename -o output_directory [-r rinex_version]
        cmd = [
            str(mdb2rinex_path),
            "-f", str(m00_file),       # Input file
            "-o", str(output_dir),      # Output directory
            "-r", version_arg,          # RINEX version
        ]

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
                    f"mdb2rinex failed with code {result.returncode}: {result.stderr}"
                )

            # mdb2rinex creates output files with automatic naming based on content
            # Look for the observation file (.xxo or .rnx)
            obs_file = self._find_mdb2rinex_output(output_dir, m00_file)

            if obs_file:
                self.logger.info(f"Created RINEX: {obs_file}")
                return obs_file
            else:
                raise ConversionError(
                    f"mdb2rinex produced no observation file for {m00_file}"
                )

        except subprocess.TimeoutExpired:
            raise ConversionError(f"mdb2rinex timed out converting {m00_file}")
        except FileNotFoundError:
            raise ConversionError(
                "mdb2rinex not found. Download from Leica myWorld portal."
            )

    def _find_mdb2rinex_output(self, output_dir: Path, m00_file: Path) -> Optional[Path]:
        """Find the observation file created by mdb2rinex.

        mdb2rinex creates files with naming based on station ID and time from
        the MDB file content, not the input filename. The naming follows RINEX
        conventions: ssssdddf.yyo or ssssdddf.yyt

        Args:
            output_dir: Directory where mdb2rinex wrote output
            m00_file: Original input file (for station ID extraction)

        Returns:
            Path to observation file if found, None otherwise
        """
        # Extract station ID from input filename (first 4 chars)
        station_id = m00_file.stem[:4].upper()

        # Look for observation files (*.xxo patterns or *.rnx)
        patterns = [
            f"{station_id}*.??o",   # RINEX 2/3 obs files (e.g., SKFC0350.26o)
            f"{station_id}*.??O",   # Uppercase
            f"{station_id}*.rnx",   # RINEX 3 modern naming
            f"{station_id}*.RNX",
            "*.??o",                # Any obs file as fallback
            "*.rnx",
        ]

        for pattern in patterns:
            matches = list(output_dir.glob(pattern))
            if matches:
                # Return the most recently modified file
                matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return matches[0]

        return None

    def _cleanup_nav_files(self, output_dir: Path) -> None:
        """Remove navigation files created by mdb2rinex.

        mdb2rinex creates both observation and navigation files, but we only
        want the observation files. This removes GLONASS nav (.xxg) and
        GPS nav (.xxn) files.

        Args:
            output_dir: Directory containing mdb2rinex output
        """
        # Navigation file patterns (RINEX 2/3 naming)
        nav_patterns = [
            "*.??n",  # GPS nav (e.g., skfc0340.26n)
            "*.??g",  # GLONASS nav (e.g., skfc0340.26g)
            "*.??l",  # Galileo nav
            "*.??p",  # Mixed nav
            "*_MN.rnx",  # RINEX 3 mixed nav
            "*_GN.rnx",  # RINEX 3 GPS nav
            "*_RN.rnx",  # RINEX 3 GLONASS nav
            "*_EN.rnx",  # RINEX 3 Galileo nav
        ]

        for pattern in nav_patterns:
            for nav_file in output_dir.glob(pattern):
                try:
                    nav_file.unlink()
                    self.logger.debug(f"Removed nav file: {nav_file.name}")
                except Exception as e:
                    self.logger.warning(f"Could not remove {nav_file}: {e}")

    def _run_teqc(self, m00_file: Path, output_dir: Path) -> Path:
        """Run teqc to convert m00 to RINEX 2 (fallback).

        Args:
            m00_file: Input m00 file
            output_dir: Output directory

        Returns:
            Path to RINEX observation file

        Raises:
            ConversionError: If teqc fails
        """
        # Generate output filename (RINEX 2 style)
        temp_obs = output_dir / f"{m00_file.stem}.obs"

        # Build teqc command using configured path
        teqc_path = self.get_tool_path("teqc")
        cmd = [str(teqc_path)]

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

        Note: This is just reformatting, not native RINEX 3 conversion.
        For native RINEX 3, use mdb2rinex.

        Args:
            rinex2_file: RINEX 2 observation file
            output_dir: Output directory

        Returns:
            Path to RINEX 3 file

        Raises:
            ConversionError: If conversion fails
        """
        rinex3_file = output_dir / f"{rinex2_file.stem}.rnx"

        try:
            gfzrnx_path = self.get_tool_path("gfzrnx")
        except ConversionError:
            self.logger.warning("gfzrnx not configured, keeping RINEX 2 output")
            return rinex2_file

        cmd = [
            str(gfzrnx_path),
            "-finp", str(rinex2_file),
            "-fout", str(rinex3_file),
            "-vo", "3",  # Output RINEX version 3
            "-f",  # Force overwrite
            "-q",  # Quiet mode - suppress warnings about RINEX 2→3 reformatting
        ]

        self.logger.info(f"Converting to RINEX 3 (reformatting): {' '.join(cmd)}")

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
    Uses the same conversion backend as LeicaConverter (mdb2rinex or teqc).
    """

    @property
    def supported_extensions(self) -> List[str]:
        """G10 uses m00 format."""
        return [".m00", ".M00", ".m00.gz", ".M00.gz"]

    @property
    def converter_name(self) -> str:
        """Return converter name based on available backend."""
        if self._use_mdb2rinex:
            return "mdb2rinex"
        return "teqc"
