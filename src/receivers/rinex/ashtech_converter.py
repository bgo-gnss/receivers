"""Ashtech raw (.atc) to RINEX converter — the pre-2012 archive frontier.

The archive's ``.atc`` extension covers two Ashtech container formats (plus
mislabeled Septentrio SBF, which the content gate routes away from here):

* **µZ-12 "U-file"** — ``BHDR`` tag at offset 4 → ``teqc -ash u``
* **Z-XII3 "R-file"** — leading ``Z-12`` → ``teqc -ash r``

The decoder flag is chosen by CONTENT (magic bytes), never by name — the
2026-07-06 .atc findings: the wrong flag segfaults (rc 139) or silently
emits nothing, and whole batches sit misfiled a decade off (the base-class
identity gate catches those AFTER decode via first-obs date + position).

Output is ALWAYS native RINEX 2.11 — teqc cannot create real RINEX 3 from
Ashtech raw, and an R2→R3 pass through gfzrnx is explicitly an ambiguous
REFORMATTING (gfzrnx's own COMMENT-block warning: LLI bit semantics differ
between versions). Policy (bgo, 2026-07-06): R3 only when the converter
creates it straight from raw; otherwise ship the honest native version.
A RINEX-3 request is therefore clamped to RINEX 2 with a loud log line.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import List

from .converter_base import ConversionError
from .trimble_converter import TrimbleConverter

logger = logging.getLogger(__name__)


class AshtechConverter(TrimbleConverter):
    """Converter for Ashtech .atc raw files via teqc.

    Subclasses TrimbleConverter for the shared machinery (subprocess runner,
    temp-file cleanup, gfzrnx RINEX-3 step) — the pipeline differs only in
    the decode step: teqc reads the .atc directly, no runpkr00 extraction.

    Example:
        >>> converter = AshtechConverter("RHOF", rinex_version=RinexVersion.RINEX_3)
        >>> result = converter.convert_file("RHOF201004020000a.atc")
    """

    # Content gate: both Ashtech container flavours; anything else positively
    # identified (SBF, ...) is refused with the right chain named.
    accepted_raw_formats = frozenset({"ashtech_u", "ashtech_r"})

    @property
    def supported_extensions(self) -> List[str]:
        return [".atc", ".atc.gz"]

    @property
    def converter_name(self) -> str:
        return "teqc"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .converter_base import NamingConvention, RinexVersion

        if self.rinex_version.value >= 3:
            self.logger.info(
                "Ashtech raw cannot yield REAL RINEX 3 (teqc decodes to 2.11; "
                "R2→R3 is ambiguous reformatting per gfzrnx's own warning) — "
                "clamping output to native RINEX 2.11"
            )
            self.rinex_version = RinexVersion.RINEX_2
            self.naming_convention = NamingConvention.SHORT

    def _get_required_tools(self) -> List[str]:
        return ["teqc"]

    def _run_conversion(
        self,
        raw_file: Path,
        output_dir: Path,
        observation_date: datetime,
    ) -> Path:
        """teqc -ash {u|r} → native RINEX 2.11 obs (+nav sibling)."""
        try:
            working_file = self._decompress_if_needed(raw_file)
            rinex_file = self._run_teqc_ashtech(
                working_file, output_dir, observation_date
            )
            # The obs IS the product — keep it out of the temp cleanup.
            if rinex_file in self._temp_files:
                self._temp_files.remove(rinex_file)
            if not self.keep_intermediate:
                self._cleanup_temp_files()
            return rinex_file
        except Exception as e:
            self._cleanup_temp_files()
            if isinstance(e, ConversionError):
                raise
            raise ConversionError(str(e), raw_file)

    def _ashtech_flag(self, raw_file: Path) -> str:
        """'u' or 'r' from the file's magic bytes — never from the name."""
        from ..archive.raw_format import ASHTECH_R, ASHTECH_U, classify_raw

        fmt = classify_raw(raw_file)
        if fmt == ASHTECH_U:
            return "u"
        if fmt == ASHTECH_R:
            return "r"
        raise ConversionError(
            f"content is '{fmt}', not an Ashtech container — the wrong teqc "
            "flag segfaults or emits nothing; refusing to guess",
            raw_file,
        )

    def _run_teqc_ashtech(
        self, raw_file: Path, output_dir: Path, observation_date: datetime
    ) -> Path:
        """Decode the .atc to RINEX 2.11 obs (+ nav sibling) with teqc."""
        teqc = self.get_tool_path("teqc")
        flag = self._ashtech_flag(raw_file)

        day_of_year = observation_date.timetuple().tm_yday
        year_2digit = observation_date.year % 100
        obs_file = (
            output_dir / f"{self.station_id}{day_of_year:03d}0.{year_2digit:02d}o"
        )
        nav_file = (
            output_dir / f"{self.station_id}{day_of_year:03d}0.{year_2digit:02d}n"
        )

        # Raw carries no marker metadata (-Unknown-): stamp the station id so
        # downstream header QC has the right marker even before the TOS
        # correction pass (which remains authoritative).
        cmd = [
            str(teqc),
            "-ash",
            flag,
            "-O.mo",
            self.station_id,
            "+obs",
            str(obs_file),
            "+nav",
            str(nav_file),
            str(raw_file),
        ]
        self.logger.info(
            f"Running teqc -ash {flag} on {raw_file.name} (content-dispatched)"
        )
        self._run_subprocess(cmd, timeout=600, cwd=output_dir)

        if not obs_file.exists() or obs_file.stat().st_size < 1024:
            raise ConversionError(
                f"teqc -ash {flag} produced no usable obs for {raw_file.name}",
                raw_file,
            )
        self._temp_files.append(obs_file)
        if nav_file.exists():
            self._temp_files.append(nav_file)  # nav comes from IGS downstream
        return obs_file
