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

    MODERN = "modern"  # .rnx.gz (no Hatanaka)
    LEGACY = "legacy"  # .D.Z (Hatanaka compressed)


class CompressionFormat(Enum):
    """File compression format options."""

    GZ = "gz"  # gzip (.gz)
    Z = "Z"  # Unix compress (.Z) - requires ncompress


class NamingConvention(Enum):
    """RINEX filename naming conventions."""

    SHORT = "short"  # RINEX 2 style: SSSS0DDF.YYt
    LONG = "long"  # IGS/RINEX 3+ style: SSSS00CCC_R_YYYYDDDHHMM_...


def _is_network_error(exc: BaseException) -> bool:
    """True if ``exc`` looks like a transport/connection failure (DNS, refused,
    reset, timeout) rather than a data error. Checks the exception-class name so
    we don't need a hard dependency on ``requests`` here — covers
    requests.ConnectionError / ConnectTimeout / ReadTimeout / MaxRetryError /
    NameResolutionError and urllib3's equivalents."""
    chain = [e for e in (exc, exc.__cause__, exc.__context__) if e]
    # Any transport-layer OSError (socket.gaierror for DNS, ECONNREFUSED, …)
    if any(isinstance(e, OSError) for e in chain):
        return True
    names = {type(e).__name__ for e in chain}
    markers = (
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "MaxRetryError",
        "NameResolutionError",
        "NewConnectionError",
        "gaierror",
    )
    return any(any(m in n for m in markers) for n in names)


class NetworkUnavailableError(Exception):
    """TOS/network unreachable during header correction.

    Distinct from a per-file conversion failure: the metadata source is down,
    not the data. Re-rinex must NOT stage a header-less file, so this propagates
    out of ``convert_file`` to abort the run cleanly; fixing connectivity and
    re-running the same command resumes (already-staged files are skipped).
    """


class ConversionError(Exception):
    """Exception raised during RINEX conversion."""

    def __init__(
        self,
        message: str,
        raw_file: Optional[Path] = None,
        details: Optional[str] = None,
    ):
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
        session_type: Optional[str] = None,
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
        # session_type drives hourly vs daily filename session letter; None = daily
        self.session_type = session_type
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
                    self.apply_hatanaka = config_hatanaka.lower() in (
                        "true",
                        "yes",
                        "1",
                    )
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

        # Naming convention: explicit > config > version-based default
        if naming_convention is not None:
            self.naming_convention = naming_convention
        else:
            rinex_cfg = self.config.get_rinex_config()
            config_naming = str(rinex_cfg.get("default_naming", "")).lower()
            if config_naming == "short":
                self.naming_convention = NamingConvention.SHORT
            elif config_naming == "long":
                self.naming_convention = NamingConvention.LONG
            elif rinex_version == RinexVersion.RINEX_2:
                self.naming_convention = NamingConvention.SHORT
            else:
                self.naming_convention = NamingConvention.LONG

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
                raise ConversionError("Raw file not found", raw_path)

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

            self.logger.info(
                f"Converting {raw_path.name} for {observation_date.date()}"
            )

            # Run conversion
            rinex_file = self._run_conversion(raw_path, output_path, observation_date)

            # Apply header corrections if enabled
            corrections_applied = 0
            if self.apply_header_corrections:
                corrections_applied = self._apply_header_corrections(
                    rinex_file, observation_date
                )

            # Canonicalize header order via gfzrnx (piece 3: matches the EPOS
            # path's header order so operational and EPOS files are directly
            # comparable). Keeps the current filename; gfzrnx rewrites in place.
            rinex_file = self._canonicalize_rinex(rinex_file, observation_date)

            # Rename to final naming convention
            final_file = self._rename_to_convention(rinex_file, observation_date)

            # Apply naming-gated header records (piece 4): long names get a
            # 9-char MARKER NAME (RHOF00ISL); short names keep the 4-char marker
            # the converter emitted — safe for GAMIT processing until confirmed.
            final_file = self._apply_naming_headers(final_file, observation_date)

            # Apply compression if needed
            final_file = self._apply_compression(final_file)

            result.rinex_file = final_file
            result.success = True
            result.message = "Conversion successful"
            result.header_corrections_applied = corrections_applied

        except NetworkUnavailableError:
            # Metadata source down, not a per-file failure — propagate so the run
            # aborts cleanly and a re-run resumes (the final compressed file is
            # never written: corrections precede compression, so no header-less
            # product is staged).
            raise

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
        return any(
            name_lower.endswith(ext.lower()) for ext in self.supported_extensions
        )

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
        for ext in [".sbf", ".t02", ".t00"]:
            if name.lower().endswith(ext):
                name = name[: -len(ext)]

        # Try pattern: STATION + YYYYMMDD + HHMM + session_letter
        # Station is 4 alphanumeric chars, date is 8 chars, time is 4 chars
        pattern = r"([A-Za-z0-9]{4})(\d{8})(\d{4})"
        match = re.match(pattern, name, re.IGNORECASE)

        if match:
            date_str = match.group(2)
            time_str = match.group(3)
            try:
                return datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M")
            except ValueError:
                pass

        # Try simpler pattern: just YYYYMMDD somewhere in filename
        pattern = r"(\d{8})"
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

            # Always inject the resolved OBSERVER / AGENCY. config_utils resolves
            # it from station_operator → agencies.yaml onto station_config['rinex'];
            # but correct_rinex_from_tos uses the TOS path for historical dates,
            # which does NOT emit observer/agency — so without this the converter's
            # own default ("GNSS Observer / Trimble") would survive on re-rinexed
            # historical files. Overrides that with GNSSatIMO / the agency name.
            extra = None
            if station_config:
                _rc = station_config.get("rinex", {})
                _obs = str(_rc.get("observer") or "").strip()
                _agc = str(_rc.get("agency") or "").strip()
                if _obs or _agc:
                    extra = {"OBSERVER / AGENCY": [_obs, _agc]}

            # Apply corrections using tostools
            # This function handles the config vs TOS decision internally.
            # tostools.search_station does sys.exit(1) on requests.ConnectionError
            # (→ SystemExit, a BaseException that skips `except Exception`); a
            # timeout/other transport error surfaces as a requests/OSError. Convert
            # all of these to NetworkUnavailableError so the run aborts cleanly
            # instead of grinding out header-less files (or dying on a raw
            # SystemExit). Genuine data no-ops still fall through to return 0.
            try:
                result = correct_rinex_from_tos(
                    rinex_file=rinex_file,
                    station_id=self.station_id,
                    observation_date=observation_date,
                    output_file=rinex_file,  # Overwrite in place
                    station_config=station_config,
                    loglevel=self.logger.level,
                    extra_corrections=extra,
                )
            except SystemExit as e:
                raise NetworkUnavailableError(
                    f"TOS unreachable while correcting {self.station_id} "
                    f"{observation_date.date()} (network/DNS down)"
                ) from e
            except OSError as e:
                # socket.gaierror (DNS) and other transport errors subclass OSError
                raise NetworkUnavailableError(
                    f"TOS unreachable while correcting {self.station_id} "
                    f"{observation_date.date()}: {e}"
                ) from e
            except Exception as e:  # noqa: BLE001 — inspect for transport errors
                if _is_network_error(e):
                    raise NetworkUnavailableError(
                        f"TOS unreachable while correcting {self.station_id} "
                        f"{observation_date.date()}: {e}"
                    ) from e
                raise

            if result is None:
                self.logger.warning(
                    f"Header correction failed for {self.station_id} at {observation_date.date()}"
                )
                return 0

            # Success - return approximate count (tostools doesn't return exact count)
            self.logger.info(f"Applied header corrections for {self.station_id}")
            return 1  # Indicates success

        except NetworkUnavailableError:
            raise
        except Exception as e:
            self.logger.warning(f"Header correction failed: {e}")
            return 0

    def _data_frequency(self) -> str:
        """gtimes lfrequency for this conversion ('1H' hourly, '1D' daily).

        Daily is the safe default — legacy callers without a session_type
        still produce the 24h naming convention they used before.
        """
        if self.session_type and "1hr" in self.session_type.lower():
            return "1H"
        return "1D"

    def _build_short_filename(self, observation_date: datetime, file_type: str) -> str:
        """Build a RINEX 2 short filename via gtimes' frequency-aware template.

        The ``#Rin2`` token in gtimes.datepathlist resolves to ``DDD<letter>.YY``
        where ``<letter>`` is ``0`` for daily (``lfrequency='1D'``) and the
        hour letter ``a``–``x`` for hourly (``lfrequency='1H'``). Going through
        the template (rather than calling ``rinex2_filename`` with an explicit
        session letter) keeps the converter aligned with how ``FormatResolver``
        and the rest of the receivers code build paths.
        """
        import gtimes.timefunc as gt

        template = f"{self.station_id}#Rin2{file_type}"
        names = gt.datepathlist(
            template, self._data_frequency(), datelist=[observation_date]
        )
        return names[0]

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
            # RINEX 2 short format: SSSS<DOY><session>.YY<type>
            # session letter comes from gtimes #Rin2 template via _data_frequency().
            new_name = self._build_short_filename(observation_date, "o")
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
        if rinex_file.suffix == ".gz":
            # Already compressed
            return rinex_file

        # Determine compression extension
        if self.compression_format == CompressionFormat.Z:
            ext = ".Z"
        else:
            ext = ".gz"

        compressed_path = rinex_file.parent / (rinex_file.name + ext)
        with open(rinex_file, "rb") as f_in:
            with gzip.open(compressed_path, "wb") as f_out:
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
        if not (len(suffix) == 4 and suffix[1:3].isdigit() and suffix[3] == "o"):
            self.logger.debug(f"Skipping Hatanaka: not an observation file ({suffix})")
            return rinex_file

        try:
            rnx2crx = self.get_tool_path("rnx2crx")
        except ConversionError:
            self.logger.warning("rnx2crx not found, skipping Hatanaka compression")
            return rinex_file

        # rnx2crx writes lowercase .YYd (its own convention). IMO's long-term
        # archive on rawdata — and the okada getimorinex.py / GAMIT pipeline that
        # reads it — expects UPPERCASE .YYD, so rename rnx2crx's output to match.
        # Lowercase .d.Z broke the okada fetch at the old-rek -> rek_new cutover
        # (DOY 172, 2026): getimorinex.py requests .D.Z, found no file, and left a
        # 0-byte download. Keep the whole archive on the historical .D.Z convention.
        rnx2crx_out = rinex_file.with_suffix(suffix[:-1] + "d")  # what rnx2crx writes
        # uppercase .YYD = IMO archive convention
        hatanaka_file = rinex_file.with_suffix(suffix[:-1] + "D")

        try:
            # Run rnx2crx: reads input file, creates lowercase .YYd alongside it.
            cmd = [str(rnx2crx), str(rinex_file)]
            self._run_subprocess(cmd, timeout=60)

            # rnx2crx creates output file alongside input with changed extension
            if rnx2crx_out.exists() and rnx2crx_out.stat().st_size > 0:
                rnx2crx_out.rename(hatanaka_file)  # .YYd -> .YYD (uppercase)
                rinex_file.unlink()  # Remove original .YYo file
                self.logger.debug(
                    f"Hatanaka compressed: {rinex_file.name} -> {hatanaka_file.name}"
                )
                return hatanaka_file
            else:
                self.logger.warning(
                    f"rnx2crx did not create expected output: {rnx2crx_out}"
                )
                return rinex_file

        except Exception as e:
            self.logger.warning(f"Hatanaka compression failed: {e}")
            return rinex_file

    def _canonicalize_rinex(self, rinex_file: Path, observation_date: datetime) -> Path:
        """Run gfzrnx to canonicalize RINEX 3 header order (piece 3).

        Mirrors the EPOS dissemination path's ``gfzrnx -vo {version}`` pass so
        operational-archive files and EPOS files share identical header ordering.
        Runs in place: the canonicalized output replaces the input file.

        On failure or if gfzrnx is unavailable, the original file is returned
        unchanged — canonicalization is best-effort.
        """
        rinex_file = Path(rinex_file)

        # Only RINEX 3 benefits from canonicalization (R2 has a different
        # standard and gfzrnx up-conversion would change the marker convention).
        if self.rinex_version not in (RinexVersion.RINEX_3, RinexVersion.RINEX_4):
            return rinex_file

        try:
            gfzrnx = self.get_tool_path("gfzrnx")
        except Exception:  # noqa: BLE001
            self.logger.debug("gfzrnx not available — skipping canonicalization")
            return rinex_file

        out = rinex_file.with_name(rinex_file.stem + "_gfzrnx" + rinex_file.suffix)
        version = self.rinex_version.value
        try:
            cmd = [
                str(gfzrnx),
                "-finp",
                str(rinex_file),
                "-fout",
                str(out),
                "-vo",
                str(version),
            ]
            self._run_subprocess(cmd, timeout=120)
        except Exception:  # noqa: BLE001
            self.logger.debug("gfzrnx run failed — keeping original header order")
            if out.exists():
                out.unlink()
            return rinex_file

        if not out.exists() or out.stat().st_size == 0:
            self.logger.debug("gfzrnx produced no output — keeping original")
            return rinex_file

        out.rename(rinex_file)
        self.logger.debug("gfzrnx canonicalized header for %s", rinex_file.name)
        return rinex_file

    def _apply_naming_headers(
        self, rinex_file: Path, observation_date: datetime
    ) -> Path:
        """Set naming-gated header records (piece 4).

        Long names → 9-char MARKER NAME (RHOF00ISL) + generic OBSERVER/AGENCY.
        Short names → keep the 4-char marker the converter emitted (safe for
        GAMIT until confirmed).

        Uses :func:`_set_header_records` (same logic the EPOS dissemination path
        uses), imported lazily to avoid a startup-time circular dependency on
        the dissemination subpackage.
        """
        if self.naming_convention != NamingConvention.LONG:
            return rinex_file

        try:
            from ..dissemination.convert import epos_marker_name, _set_header_records

            nine_char = epos_marker_name(
                self.station_id, self.rinex_version.value, "ISL"
            )
            _set_header_records(rinex_file, {"MARKER NAME": nine_char})
            self.logger.debug(
                "set 9-char MARKER NAME for %s: %s",
                rinex_file.name,
                nine_char,
            )
        except Exception:  # noqa: BLE001
            self.logger.debug(
                "could not apply 9-char marker for %s (dissemination not available)",
                rinex_file.name,
            )
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
            for section in ["rinex_tools", "rinex"]:
                try:
                    tool_path = self.config.config.get(section, config_key)
                    if tool_path and Path(tool_path).exists():
                        self._tool_paths[tool_name] = Path(tool_path)
                        return self._tool_paths[tool_name]
                except Exception:
                    pass
        except Exception:
            pass

        # Try system PATH (also uppercase variant for tools like RNX2CRX)
        import shutil

        system_path = shutil.which(tool_name) or shutil.which(tool_name.upper())
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
