"""
Abstract base class for raw-to-RINEX converters.

This module defines the interface that all raw format converters must implement,
along with common data structures for conversion results.
"""

import logging
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..config.receivers_config import get_receivers_config


class RinexVersion(Enum):
    """RINEX format versions."""
    RINEX_2 = 2
    RINEX_3 = 3
    RINEX_4 = 4


class OutputFormat(Enum):
    """Output file format options (legacy compatibility)."""
    MODERN = "modern"   # .rnx.gz (no Hatanaka)
    LEGACY = "legacy"   # .D.Z (Hatanaka compressed)


class CompressionFormat(Enum):
    """File compression format options."""
    GZ = "gz"     # gzip (.gz)
    Z = "Z"       # Unix compress (.Z) - requires ncompress


class NamingConvention(Enum):
    """RINEX filename naming conventions."""
    SHORT = "short"   # RINEX 2 style: SSSS0DDF.YYt
    LONG = "long"     # IGS/RINEX 3+ style: SSSS00CCC_R_YYYYDDDHHMM_...


class ConversionError(Exception):
    """Exception raised during RINEX conversion."""

    def __init__(self, message: str, raw_file: Optional[Path] = None, details: Optional[str] = None):
        self.message = message
        self.raw_file = raw_file
        self.details = details
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        msg = self.message
        if self.raw_file:
            msg += f" (file: {self.raw_file})"
        if self.details:
            msg += f"\nDetails: {self.details}"
        return msg


@dataclass
class ConversionResult:
    """Result of a single file conversion."""
    raw_file: Path
    rinex_file: Optional[Path] = None
    success: bool = False
    message: str = ""
    duration_seconds: float = 0.0
    header_corrections_applied: int = 0
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "raw_file": str(self.raw_file),
            "rinex_file": str(self.rinex_file) if self.rinex_file else None,
            "success": self.success,
            "message": self.message,
            "duration_seconds": self.duration_seconds,
            "header_corrections_applied": self.header_corrections_applied,
            "warnings": self.warnings,
        }


@dataclass
class BatchConversionResult:
    """Result of batch conversion."""
    station_id: str
    total_files: int = 0
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    results: List[ConversionResult] = field(default_factory=list)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    @property
    def total_duration_seconds(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return sum(r.duration_seconds for r in self.results)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "station_id": self.station_id,
            "total_files": self.total_files,
            "successful": self.successful,
            "failed": self.failed,
            "skipped": self.skipped,
            "total_duration_seconds": self.total_duration_seconds,
            "results": [r.to_dict() for r in self.results],
        }


class RawToRinexConverter(ABC):
    """Abstract base class for raw GPS data to RINEX converters.

    Subclasses implement conversion for specific raw formats:
    - SBFConverter: Septentrio Binary Format (.sbf)
    - TrimbleConverter: Trimble formats (.T02, .T00)
    - LeicaConverter: Leica MDB format (.m00)

    Each converter handles:
    1. Raw format conversion using appropriate external tools
    2. Header correction using TOS database metadata
    3. Output naming according to short/long conventions
    """

    def __init__(
        self,
        station_id: str,
        rinex_version: RinexVersion = RinexVersion.RINEX_3,
        output_format: Optional[OutputFormat] = None,
        naming_convention: Optional[NamingConvention] = None,
        apply_header_corrections: bool = True,
        apply_hatanaka: Optional[bool] = None,
        compression_format: Optional[CompressionFormat] = None,
        loglevel: int = logging.INFO,
    ):
        """Initialize converter.

        Args:
            station_id: Station identifier (e.g., 'ELDC')
            rinex_version: Target RINEX version (2, 3, or 4)
            output_format: Legacy parameter for backwards compatibility.
                          Use apply_hatanaka and compression_format instead.
            naming_convention: Filename convention (SHORT or LONG).
                              If None, defaults based on rinex_version:
                              RINEX_2 -> SHORT, RINEX_3/4 -> LONG
            apply_header_corrections: Whether to apply TOS metadata corrections
            apply_hatanaka: Apply Hatanaka compression (.YYd vs .YYo).
                           If None, reads from config default_hatanaka.
            compression_format: File compression (GZ or Z).
                               If None, reads from config default_compression.
            loglevel: Logging level
        """
        self.station_id = station_id.upper()
        self.rinex_version = rinex_version
        self.apply_header_corrections = apply_header_corrections
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.logger.setLevel(loglevel)

        # Load configuration
        self.config = get_receivers_config()
        self._tool_paths: Dict[str, Path] = {}

        # Handle legacy output_format parameter
        if output_format is not None:
            if output_format == OutputFormat.LEGACY:
                self.apply_hatanaka = True
                self.compression_format = CompressionFormat.Z
            else:
                self.apply_hatanaka = False
                self.compression_format = CompressionFormat.GZ
        else:
            # Read from config or use defaults
            rinex_config = self.config.get_rinex_config()

            if apply_hatanaka is not None:
                self.apply_hatanaka = apply_hatanaka
            else:
                config_hatanaka = rinex_config.get("default_hatanaka", True)
                if isinstance(config_hatanaka, str):
                    self.apply_hatanaka = config_hatanaka.lower() in ("true", "yes", "1")
                else:
                    self.apply_hatanaka = bool(config_hatanaka)

            if compression_format is not None:
                self.compression_format = compression_format
            else:
                config_compression = str(rinex_config.get("default_compression", "gz"))
                if config_compression.upper() == "Z":
                    self.compression_format = CompressionFormat.Z
                else:
                    self.compression_format = CompressionFormat.GZ

        # Keep output_format for backwards compatibility
        if self.apply_hatanaka:
            self.output_format = OutputFormat.LEGACY
        else:
            self.output_format = OutputFormat.MODERN

        # Default naming convention based on RINEX version
        if naming_convention is None:
            if rinex_version == RinexVersion.RINEX_2:
                self.naming_convention = NamingConvention.SHORT
            else:
                self.naming_convention = NamingConvention.LONG
        else:
            self.naming_convention = naming_convention

    @property
    @abstractmethod
    def supported_extensions(self) -> List[str]:
        """Return list of supported raw file extensions (e.g., ['.sbf', '.sbf.gz'])."""
        pass

    @property
    @abstractmethod
    def converter_name(self) -> str:
        """Return name of the external converter tool (e.g., 'sbf2rin')."""
        pass

    @abstractmethod
    def _run_conversion(
        self,
        raw_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """Run the actual conversion process.

        Args:
            raw_file: Path to raw input file
            output_dir: Directory for output RINEX file
            observation_date: Date of observation for naming

        Returns:
            Path to converted RINEX file (before header correction/renaming)

        Raises:
            ConversionError: If conversion fails
        """
        pass

    def convert_file(
        self,
        raw_file: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        observation_date: Optional[datetime] = None,
        force: bool = False,
    ) -> ConversionResult:
        """Convert a single raw file to RINEX format.

        Args:
            raw_file: Path to raw input file
            output_dir: Output directory (default: same as input)
            observation_date: Date of observation (extracted from filename if not provided)
            force: Overwrite existing output files

        Returns:
            ConversionResult with conversion details
        """
        import time
        start_time = time.time()
        raw_path = Path(raw_file)
        result = ConversionResult(raw_file=raw_path)

        try:
            # Validate input file
            if not raw_path.exists():
                raise ConversionError(f"Raw file not found", raw_path)

            if not self._has_supported_extension(raw_path):
                raise ConversionError(
                    f"Unsupported file extension. Expected one of: {self.supported_extensions}",
                    raw_path,
                )

            # Set output directory
            if output_dir is None:
                output_dir = raw_path.parent
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)

            # Extract observation date from filename if not provided
            if observation_date is None:
                observation_date = self._extract_date_from_filename(raw_path)

            self.logger.info(f"Converting {raw_path.name} for {observation_date.date()}")

            # Run conversion
            rinex_file = self._run_conversion(raw_path, output_path, observation_date)

            # Apply header corrections if enabled
            corrections_applied = 0
            if self.apply_header_corrections:
                corrections_applied = self._apply_header_corrections(
                    rinex_file, observation_date
                )

            # Rename to final naming convention
            final_file = self._rename_to_convention(rinex_file, observation_date)

            # Apply compression if needed
            final_file = self._apply_compression(final_file)

            result.rinex_file = final_file
            result.success = True
            result.message = "Conversion successful"
            result.header_corrections_applied = corrections_applied

        except ConversionError as e:
            result.success = False
            result.message = str(e)
            self.logger.error(f"Conversion failed: {e}")

        except Exception as e:
            result.success = False
            result.message = f"Unexpected error: {e}"
            self.logger.exception(f"Unexpected error converting {raw_path}")

        finally:
            result.duration_seconds = time.time() - start_time

        return result

    def convert_batch(
        self,
        raw_files: List[Union[str, Path]],
        output_dir: Optional[Union[str, Path]] = None,
        force: bool = False,
    ) -> BatchConversionResult:
        """Convert multiple raw files to RINEX format.

        Args:
            raw_files: List of raw file paths
            output_dir: Output directory (default: same as each input)
            force: Overwrite existing output files

        Returns:
            BatchConversionResult with all conversion details
        """
        batch_result = BatchConversionResult(
            station_id=self.station_id,
            total_files=len(raw_files),
            start_time=datetime.now(),
        )

        for raw_file in raw_files:
            result = self.convert_file(raw_file, output_dir, force=force)
            batch_result.results.append(result)

            if result.success:
                batch_result.successful += 1
            else:
                batch_result.failed += 1

        batch_result.end_time = datetime.now()
        return batch_result

    def _has_supported_extension(self, file_path: Path) -> bool:
        """Check if file has a supported extension."""
        name_lower = file_path.name.lower()
        return any(name_lower.endswith(ext.lower()) for ext in self.supported_extensions)

    def _extract_date_from_filename(self, file_path: Path) -> datetime:
        """Extract observation date from raw filename.

        Expected patterns:
        - Septentrio: STATIONYYYYMMDDHHMM{session}.sbf[.gz]
        - Trimble: STATIONYYYYMMDDHHMM{session}.T02/.T00

        Args:
            file_path: Path to raw file

        Returns:
            Extracted datetime

        Raises:
            ConversionError: If date cannot be extracted
        """
        import re

        name = file_path.stem
        # Remove .sbf, .T02, etc. extensions that might be left after .stem
        for ext in ['.sbf', '.t02', '.t00']:
            if name.lower().endswith(ext):
                name = name[:-len(ext)]

        # Try pattern: STATION + YYYYMMDD + HHMM + session_letter
        # Station is 4 alphanumeric chars, date is 8 chars, time is 4 chars
        pattern = r'([A-Za-z0-9]{4})(\d{8})(\d{4})'
        match = re.match(pattern, name, re.IGNORECASE)

        if match:
            date_str = match.group(2)
            time_str = match.group(3)
            try:
                return datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M")
            except ValueError:
                pass

        # Try simpler pattern: just YYYYMMDD somewhere in filename
        pattern = r'(\d{8})'
        match = re.search(pattern, name)
        if match:
            date_str = match.group(1)
            try:
                return datetime.strptime(date_str, "%Y%m%d")
            except ValueError:
                pass

        raise ConversionError(
            f"Could not extract date from filename: {file_path.name}",
            file_path,
        )

    def _apply_header_corrections(
        self,
        rinex_file: Path,
        observation_date: datetime,
    ) -> int:
        """Apply TOS metadata corrections to RINEX header.

        Uses tostools.rinex.correct_rinex_from_tos() which:
        - For recent dates (>= config_valid_from): Uses station.cfg (no TOS query)
        - For historical dates (< config_valid_from): Queries TOS database

        Args:
            rinex_file: Path to RINEX file
            observation_date: Date of observation (for historical metadata lookup)

        Returns:
            Number of corrections applied (estimated from operation success)
        """
        try:
            from tostools.rinex import correct_rinex_from_tos
        except ImportError:
            self.logger.warning("tostools not available, skipping header corrections")
            return 0

        try:
            # Get station configuration (used for recent dates)
            station_config = None
            try:
                from ..config_utils import get_station_config
                station_config = get_station_config(self.station_id)
            except Exception as e:
                self.logger.debug(f"Could not load station config: {e}")

            # Apply corrections using tostools
            # This function handles the config vs TOS decision internally
            result = correct_rinex_from_tos(
                rinex_file=rinex_file,
                station_id=self.station_id,
                observation_date=observation_date,
                output_file=rinex_file,  # Overwrite in place
                station_config=station_config,
                loglevel=self.logger.level,
            )

            if result is None:
                self.logger.warning(
                    f"Header correction failed for {self.station_id} at {observation_date.date()}"
                )
                return 0

            # Success - return approximate count (tostools doesn't return exact count)
            self.logger.info(f"Applied header corrections for {self.station_id}")
            return 1  # Indicates success

        except Exception as e:
            self.logger.warning(f"Header correction failed: {e}")
            return 0

    def _rename_to_convention(
        self,
        rinex_file: Path,
        observation_date: datetime,
    ) -> Path:
        """Rename RINEX file according to naming convention.

        Args:
            rinex_file: Path to current RINEX file
            observation_date: Date of observation

        Returns:
            Path to renamed file
        """
        from gtimes import timefunc

        # Generate filename based on naming convention
        if self.naming_convention == NamingConvention.SHORT:
            # RINEX 2 short format: SSSS0DDF.YYo
            new_name = timefunc.rinex2_filename(
                self.station_id,
                observation_date,
                file_type="o",  # observation
            )
        else:
            # RINEX 3 long format: SSSS00CCC_R_YYYYDDD...
            new_name = timefunc.rinex3_filename(
                self.station_id,
                observation_date,
                country_code="ISL",
                data_source="R",  # Receiver
                file_period="01D",  # Will be updated based on session
                data_frequency="15S",  # Will be updated based on session
                file_type="MO",  # Mixed Observation
                uppercase=True,  # Use uppercase station ID
            )

        # Rename file
        new_path = rinex_file.parent / new_name

        if new_path != rinex_file:
            rinex_file.rename(new_path)
            self.logger.debug(f"Renamed {rinex_file.name} -> {new_name}")

        return new_path

    def _apply_compression(self, rinex_file: Path) -> Path:
        """Apply compression to RINEX file.

        Uses apply_hatanaka and compression_format attributes:
        - apply_hatanaka: True -> run rnx2crx (.YYo -> .YYd)
        - compression_format: GZ -> .gz, Z -> .Z

        Args:
            rinex_file: Path to RINEX file

        Returns:
            Path to compressed file
        """
        import gzip
        import shutil

        # Step 1: Apply Hatanaka compression if enabled
        if self.apply_hatanaka:
            rinex_file = self._apply_hatanaka_compression(rinex_file)

        # Step 2: Apply file compression
        if rinex_file.suffix == '.gz':
            # Already compressed
            return rinex_file

        # Determine compression extension
        if self.compression_format == CompressionFormat.Z:
            ext = '.Z'
        else:
            ext = '.gz'

        compressed_path = rinex_file.parent / (rinex_file.name + ext)
        with open(rinex_file, 'rb') as f_in:
            with gzip.open(compressed_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        rinex_file.unlink()
        return compressed_path

    def _apply_hatanaka_compression(self, rinex_file: Path) -> Path:
        """Apply Hatanaka compression to RINEX observation file.

        Uses rnx2crx to convert .YYo -> .YYd (compact RINEX format).
        Only applies to observation files (.o extension).

        Args:
            rinex_file: Path to RINEX observation file

        Returns:
            Path to Hatanaka compressed file, or original if not applicable
        """
        # Check if this is an observation file (.YYo format)
        suffix = rinex_file.suffix.lower()
        if not (len(suffix) == 4 and suffix[1:3].isdigit() and suffix[3] == 'o'):
            self.logger.debug(f"Skipping Hatanaka: not an observation file ({suffix})")
            return rinex_file

        try:
            rnx2crx = self.get_tool_path("rnx2crx")
        except ConversionError:
            self.logger.warning("rnx2crx not found, skipping Hatanaka compression")
            return rinex_file

        # Generate output filename (.YYo -> .YYd)
        hatanaka_suffix = suffix[:-1] + 'd'  # e.g., .26o -> .26d
        hatanaka_file = rinex_file.with_suffix(hatanaka_suffix)

        try:
            # Run rnx2crx: reads input file, creates output file in same directory
            # Output filename is automatically .YYd (e.g., .26o -> .26d)
            cmd = [str(rnx2crx), str(rinex_file)]
            self._run_subprocess(cmd, timeout=60)

            # rnx2crx creates output file alongside input with changed extension
            if hatanaka_file.exists() and hatanaka_file.stat().st_size > 0:
                rinex_file.unlink()  # Remove original .26o file
                self.logger.debug(f"Hatanaka compressed: {rinex_file.name} -> {hatanaka_file.name}")
                return hatanaka_file
            else:
                self.logger.warning(f"rnx2crx did not create expected output: {hatanaka_file}")
                return rinex_file

        except Exception as e:
            self.logger.warning(f"Hatanaka compression failed: {e}")
            return rinex_file

    def _run_subprocess(
        self,
        cmd: List[str],
        timeout: int = 300,
        cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess:
        """Run external command with error handling.

        Args:
            cmd: Command and arguments
            timeout: Timeout in seconds
            cwd: Working directory

        Returns:
            Completed process result

        Raises:
            ConversionError: If command fails
        """
        self.logger.debug(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )

            if result.returncode != 0:
                raise ConversionError(
                    f"{cmd[0]} failed with exit code {result.returncode}",
                    details=result.stderr or result.stdout,
                )

            return result

        except subprocess.TimeoutExpired:
            raise ConversionError(f"{cmd[0]} timed out after {timeout} seconds")

        except FileNotFoundError:
            raise ConversionError(
                f"{cmd[0]} not found. Please install or configure path.",
                details=f"Command: {' '.join(cmd)}",
            )

    def get_tool_path(self, tool_name: str) -> Path:
        """Get path to external conversion tool.

        Args:
            tool_name: Name of the tool (e.g., 'sbf2rin')

        Returns:
            Path to tool executable

        Raises:
            ConversionError: If tool not found
        """
        if tool_name in self._tool_paths:
            return self._tool_paths[tool_name]

        # Try configuration first
        try:
            config_key = f"{tool_name}_path"
            # Try [rinex_tools] section, then [rinex] section
            for section in ['rinex_tools', 'rinex']:
                try:
                    tool_path = self.config.config.get(section, config_key)
                    if tool_path and Path(tool_path).exists():
                        self._tool_paths[tool_name] = Path(tool_path)
                        return self._tool_paths[tool_name]
                except Exception:
                    pass
        except Exception:
            pass

        # Try system PATH
        import shutil
        system_path = shutil.which(tool_name)
        if system_path:
            self._tool_paths[tool_name] = Path(system_path)
            return self._tool_paths[tool_name]

        raise ConversionError(
            f"Conversion tool '{tool_name}' not found",
            details="Install the tool or configure its path in receivers.cfg [rinex_tools] section",
        )

    def validate_tools(self) -> Dict[str, bool]:
        """Validate that required conversion tools are available.

        Returns:
            Dictionary mapping tool names to availability status
        """
        tools = self._get_required_tools()
        results = {}

        for tool in tools:
            try:
                self.get_tool_path(tool)
                results[tool] = True
            except ConversionError:
                results[tool] = False

        return results

    @abstractmethod
    def _get_required_tools(self) -> List[str]:
        """Return list of required external tools for this converter."""
        pass
