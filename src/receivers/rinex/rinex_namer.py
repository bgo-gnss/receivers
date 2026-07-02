"""
RINEX filename generation utilities.

This module provides filename generation for both short (RINEX 2) and
long (IGS/RINEX 3+) naming conventions.

Short format (RINEX 2):
    SSSS0DDF.YYt
    - SSSS: Station marker (4 chars)
    - 0: Day of year (3 digits, 001-366)
    - DD: File sequence (0 for first, a for second, etc.)
    - F: Session indicator
    - YY: 2-digit year
    - t: File type (o=obs, n=nav, etc.)

Long format (IGS/RINEX 3+):
    SSSS00CCC_R_YYYYDDDHHMM_PPU_FFS_TT.rnx
    - SSSS: Station marker (4 chars, lowercase for RINEX 3+)
    - 00: Monument marker (2 digits)
    - CCC: Country code (3 chars, ISL for Iceland)
    - _R_: Data source (R=receiver, S=stream, U=unknown)
    - YYYY: 4-digit year
    - DDD: Day of year (3 digits)
    - HH: Start hour (00-23)
    - MM: Start minute (00-59)
    - PP: File period (01D, 01H, 15M, etc.)
    - U: Period unit (D=day, H=hour, M=minute, S=second)
    - FF: Data frequency (15S, 01S, 30S, etc.)
    - S: Frequency unit
    - TT: File type (MO=Mixed Obs, GO=GPS Obs, etc.)
    - .rnx: Extension
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from .converter_base import NamingConvention, RinexVersion


@dataclass
class FileNameComponents:
    """Components for RINEX filename generation."""

    station: str  # 4-char marker
    year: int  # Full year
    day_of_year: int  # 1-366
    hour: int = 0  # 0-23
    minute: int = 0  # 0-59
    file_sequence: int = 0  # 0 for first file
    monument_number: str = "00"
    country_code: str = "ISL"
    data_source: str = "R"  # R=receiver, S=stream, U=unknown
    file_period: str = "01D"  # 01D, 01H, 15M
    data_frequency: str = "15S"  # 15S, 01S, 30S
    file_type: str = "MO"  # MO=Mixed Obs, GO=GPS Obs, etc.


class RinexNamer:
    """Generate RINEX filenames according to naming conventions.

    Supports both short (RINEX 2) and long (IGS/RINEX 3+) naming formats.

    Example usage:
        >>> namer = RinexNamer("ELDC", RinexVersion.RINEX_3)
        >>> name = namer.generate_filename(
        ...     datetime(2026, 1, 15, 0, 0),
        ...     convention=NamingConvention.LONG,
        ...     file_type="MO"
        ... )
        >>> print(name)
        ELDC00ISL_R_20260150000_01D_15S_MO.rnx
    """

    # Hour letter mapping for RINEX 2 (0=a, 1=b, ..., 23=x)
    HOUR_LETTERS = "abcdefghijklmnopqrstuvwx"

    # File type codes
    FILE_TYPE_EXTENSIONS = {
        "o": "observation",
        "n": "navigation",
        "m": "meteorological",
        "g": "glonass_navigation",
        "l": "galileo_navigation",
        "h": "sbas_payload",
    }

    # Country codes
    COUNTRY_CODES = {
        "IS": "ISL",
        "NO": "NOR",
        "SE": "SWE",
        "DK": "DNK",
        "FI": "FIN",
        "GL": "GRL",
    }

    def __init__(
        self,
        station_id: str,
        rinex_version: RinexVersion = RinexVersion.RINEX_3,
        country_code: str = "ISL",
        uppercase_station: bool = True,
        loglevel: int = logging.INFO,
        monument_number: str = "00",
    ):
        """Initialize RINEX namer.

        Args:
            station_id: 4-character station marker
            rinex_version: Target RINEX version
            country_code: 3-character country code (default: ISL for Iceland)
            uppercase_station: Use uppercase station ID in long filenames (default: True)
                               Note: IGS convention is lowercase, but we default to uppercase
            loglevel: Logging level
            monument_number: 2-digit monument number for the 9-char long-name ID
                             (default "00"; will become a per-station TOS attribute)
        """
        self.station_id = station_id.upper()[:4].ljust(4)  # Ensure 4 chars
        self.rinex_version = rinex_version
        self.country_code = country_code.upper()[:3]
        self.uppercase_station = uppercase_station
        self.monument_number = str(monument_number)[:2].rjust(2, "0")
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.logger.setLevel(loglevel)

    def generate_filename(
        self,
        observation_time: datetime,
        convention: Optional[NamingConvention] = None,
        file_sequence: int = 0,
        data_source: str = "R",
        file_period: Optional[str] = None,
        data_frequency: Optional[str] = None,
        file_type: str = "MO",
        include_extension: bool = True,
    ) -> str:
        """Generate RINEX filename.

        Args:
            observation_time: Start time of observation
            convention: Naming convention to use. If None, defaults based on
                        RINEX version (SHORT for v2, LONG for v3+)
            file_sequence: File sequence number (0 for first)
            data_source: Data source (R=receiver, S=stream, U=unknown)
            file_period: File period (01D, 01H, 15M) - auto-detected if None
            data_frequency: Data frequency (15S, 01S, 30S) - auto-detected if None
            file_type: File type code (MO, GO, RO, etc.)
            include_extension: Whether to include file extension

        Returns:
            Generated filename
        """
        # Default convention based on RINEX version
        if convention is None:
            if self.rinex_version == RinexVersion.RINEX_2:
                convention = NamingConvention.SHORT
            else:
                convention = NamingConvention.LONG

        if convention == NamingConvention.SHORT:
            return self._generate_short_name(
                observation_time,
                file_sequence,
                file_type,
                include_extension,
            )
        else:
            return self._generate_long_name(
                observation_time,
                file_sequence,
                data_source,
                file_period,
                data_frequency,
                file_type,
                include_extension,
            )

    def _generate_short_name(
        self,
        observation_time: datetime,
        file_sequence: int,
        file_type: str,
        include_extension: bool,
    ) -> str:
        """Generate short (RINEX 2) format filename.

        Format: SSSS0DDF.YYt
        Example: ELDC0150.26o

        Uses gtimes.timefunc.rinex2_filename internally.
        """
        from gtimes import timefunc

        # Determine session character
        if file_sequence == 0:
            session = "0"  # Daily file
        elif file_sequence < 10:
            session = str(file_sequence)
        else:
            session = chr(ord("a") + file_sequence - 10)

        return timefunc.rinex2_filename(
            self.station_id,
            observation_time,
            file_type=file_type,
            session=session,
        )

    def _generate_long_name(
        self,
        observation_time: datetime,
        file_sequence: int,  # noqa: ARG002 - kept for API compatibility
        data_source: str,
        file_period: Optional[str],
        data_frequency: Optional[str],
        file_type: str,
        include_extension: bool,
    ) -> str:
        """Generate long (IGS/RINEX 3+) format filename.

        Format: SSSS00CCC_R_YYYYDDDHHMM_PPU_FFS_TT.rnx
        Example: ELDC00ISL_R_20260150000_01D_15S_MO.rnx

        Uses gtimes.timefunc.rinex3_filename internally.
        """
        from gtimes import timefunc

        # Auto-detect period if not specified
        if file_period is None:
            if observation_time.hour == 0 and observation_time.minute == 0:
                file_period = "01D"  # Daily file
            else:
                file_period = "01H"  # Hourly file

        # Auto-detect frequency if not specified
        if data_frequency is None:
            data_frequency = "15S"  # Default 15-second

        filename = timefunc.rinex3_filename(
            self.station_id,
            observation_time,
            country_code=self.country_code,
            data_source=data_source,
            file_period=file_period,
            data_frequency=data_frequency,
            file_type=file_type,
            monument_number=self.monument_number,
        )

        # gtimes produces lowercase station ID per IGS convention
        # Uppercase the station part (first 4 chars) if requested
        if self.uppercase_station:
            filename = filename[:4].upper() + filename[4:]

        # gtimes always includes .rnx extension, strip if not wanted
        if not include_extension and filename.endswith(".rnx"):
            filename = filename[:-4]

        return filename

    @staticmethod
    def parse_filename(filename: str) -> Optional[FileNameComponents]:
        """Parse a RINEX filename into components.

        Delegates to gtimes.timefunc.parse_rinex3_filename /
        parse_rinex2_filename for the canonical parser. Returns a
        FileNameComponents dataclass (receivers-side shape).

        Args:
            filename: RINEX filename to parse (directory part ignored).

        Returns:
            FileNameComponents if successfully parsed, None otherwise.
        """
        from gtimes.timefunc import parse_rinex2_filename, parse_rinex3_filename

        name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

        parsed = parse_rinex3_filename(name)
        if parsed is not None:
            return FileNameComponents(
                station=parsed["station"].upper(),
                monument_number=parsed["monument_number"],
                country_code=parsed["country_code"].upper(),
                data_source=parsed["data_source"],
                year=parsed["year"],
                day_of_year=parsed["doy"],
                hour=parsed["hour"],
                minute=parsed["minute"],
                file_period=parsed["file_period"],
                data_frequency=parsed["data_frequency"],
                file_type=parsed["file_type"],
            )

        parsed = parse_rinex2_filename(name)
        if parsed is not None:
            session = parsed["session"]
            if session.isdigit():
                file_sequence = int(session)
            else:
                file_sequence = ord(session.lower()) - ord("a") + 10
            type_mapping = {"o": "MO", "n": "MN", "g": "GN", "l": "EN", "m": "MM"}
            return FileNameComponents(
                station=parsed["station"].upper(),
                year=parsed["year"],
                day_of_year=parsed["doy"],
                file_sequence=file_sequence,
                file_type=type_mapping.get(parsed["file_type"], "MO"),
            )

        return None

    @staticmethod
    def parse_date_hour(
        filename: str, station_id: Optional[str] = None
    ) -> Tuple[Optional[date], Optional[int]]:
        """Parse a RINEX filename into (file_date, file_hour).

        Convenience for callers that only need the datetime anchor. Daily
        files (RINEX 2 session '0' or RINEX 3 period '01D' at hour 0) yield
        hour=None; hourly files (RINEX 2 session a-x or RINEX 3 with a
        non-zero hour) yield hour=0..23.

        Delegates to gtimes.timefunc parsers for the actual regex matching.

        Args:
            filename: RINEX filename to parse (directory part ignored).
            station_id: If given, require parsed station to match
                (case-insensitive). Mismatch returns (None, None).

        Returns:
            (file_date, file_hour) — either may be None if unparseable.
        """
        from gtimes.timefunc import parse_rinex2_filename, parse_rinex3_filename

        name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        for _ext in (".Z", ".gz", ".bz2"):
            if name.endswith(_ext):
                name = name[: -len(_ext)]
                break

        parsed = parse_rinex3_filename(name)
        if parsed is not None:
            if (
                station_id is not None
                and parsed["station"].upper() != station_id.upper()
            ):
                return None, None
            file_date = date(parsed["year"], 1, 1) + timedelta(days=parsed["doy"] - 1)
            if (
                parsed["file_period"] == "01D"
                and parsed["hour"] == 0
                and parsed["minute"] == 0
            ):
                return file_date, None
            return file_date, parsed["hour"]

        parsed = parse_rinex2_filename(name)
        if parsed is not None:
            if (
                station_id is not None
                and parsed["station"].upper() != station_id.upper()
            ):
                return None, None
            file_date = date(parsed["year"], 1, 1) + timedelta(days=parsed["doy"] - 1)
            session = parsed["session"]
            if session == "0":
                return file_date, None
            if "a" <= session <= "x":
                return file_date, ord(session) - ord("a")
            return file_date, None

        return None, None

    @staticmethod
    def get_session_file_period(session_type: str) -> str:
        """Get file period code for a session type.

        Args:
            session_type: Session type (e.g., '15s_24hr', '1Hz_1hr')

        Returns:
            File period code (e.g., '01D', '01H')
        """
        session_lower = session_type.lower()

        if "24hr" in session_lower or "daily" in session_lower:
            return "01D"
        elif "1hr" in session_lower or "hourly" in session_lower:
            return "01H"
        elif "15m" in session_lower:
            return "15M"
        else:
            return "01D"  # Default to daily

    @staticmethod
    def get_session_data_frequency(session_type: str) -> str:
        """Get data frequency code for a session type.

        Args:
            session_type: Session type (e.g., '15s_24hr', '1Hz_1hr')

        Returns:
            Data frequency code (e.g., '15S', '01S')
        """
        session_lower = session_type.lower()

        if "1hz" in session_lower or "1s" in session_lower:
            return "01S"
        elif "15s" in session_lower:
            return "15S"
        elif "30s" in session_lower:
            return "30S"
        elif "5s" in session_lower:
            return "05S"
        elif "20hz" in session_lower:
            return "00U"  # 50ms
        elif "50hz" in session_lower:
            return "00U"  # 20ms
        else:
            return "15S"  # Default
