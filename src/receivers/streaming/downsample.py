"""Downsample 1 Hz hourly RINEX to a decimated daily RINEX file.

Port of the legacy ``conv1Hzrinto15s.sh``: for a station+day, take the (up to 24)
hourly 1 Hz RINEX files produced by BNC stream capture and build a single daily
RINEX file decimated to a coarser interval (default 15 s), in the fleet's
Hatanaka-compressed form.

Pipeline per day (mirrors the legacy ``uthjap`` / ``teqc`` / ``thjap`` chain with
the modern toolchain)::

    each hourly  <name>.YYd.Z  ──gzip -d──▶ <name>.YYd ──CRX2RNX──▶ <name>.YYo
    all hourly .YYo            ──teqc -O.int N -O.dec N──▶ daily .YYo  (concat+decimate)
    daily .YYo                 ──RNX2CRX──▶ daily .YYd ──compress──▶ daily .YYd.Z

External-command execution goes through a single injectable ``runner`` so the
orchestration (skip-if-exists, no-source handling, command sequencing, output
placement) is fully unit-testable without teqc / CRX2RNX / RNX2CRX present.

NOTE: exact tool flags and the final compression format (.Z) must be validated
against the real tools and the on-disk archive on rek-d01 before production use.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: A runner executes a command, optionally redirecting stdout to a file.
#: Returns the process return code.
Runner = Callable[[Sequence[str], Optional[Path]], int]

#: Minimum plausible size (bytes) for a valid decimated daily file.
DEFAULT_MIN_OUTPUT_SIZE = 1000


def _default_runner(cmd: Sequence[str], stdout_path: Optional[Path] = None) -> int:
    """Run ``cmd``; if ``stdout_path`` is given, capture stdout into that file."""
    if stdout_path is not None:
        with open(stdout_path, "wb") as fh:
            proc = subprocess.run(list(cmd), stdout=fh, timeout=600)
        return proc.returncode
    return subprocess.run(list(cmd), timeout=600).returncode


@dataclass
class DownsampleResult:
    """Outcome of a daily downsample."""

    station_id: str
    output_file: Path
    status: str  # created | skipped_exists | no_source | failed
    source_count: int = 0
    size_bytes: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in ("created", "skipped_exists")


class RinexDownsampler:
    """Build a decimated daily RINEX from hourly 1 Hz RINEX files."""

    def __init__(
        self,
        *,
        interval: int = 15,
        crx2rnx: str = "CRX2RNX",
        rnx2crx: str = "RNX2CRX",
        gfzrnx: str = "gfzrnx",
        compressor: Optional[Sequence[str]] = None,
        runner: Optional[Runner] = None,
        min_output_size: int = DEFAULT_MIN_OUTPUT_SIZE,
    ):
        self.interval = interval
        self.crx2rnx = crx2rnx
        self.rnx2crx = rnx2crx
        # gfzrnx, not teqc, does the concat+decimate: teqc 2019 hardcodes the
        # GLONASS slot max at 24 and aborts on modern slots (R25+) that appear in
        # BNC stream RINEX, failing the whole day. gfzrnx handles them.
        self.gfzrnx = gfzrnx
        # Default to gzip (handles/produces .Z-named gzip); overridable to `compress`.
        self.compressor = list(compressor) if compressor else ["gzip", "-c"]
        self._run: Runner = runner or _default_runner
        self.min_output_size = min_output_size

    def downsample_day(
        self,
        station_id: str,
        source_files: Sequence[Path],
        output_file: Path,
        workdir: Path,
        *,
        skip_if_exists: bool = True,
        min_existing_size: int = 300_000,
    ) -> DownsampleResult:
        """Create ``output_file`` (decimated daily RINEX) from hourly ``source_files``.

        Skips when the target already exists and is plausibly complete. Returns a
        ``no_source`` result (not an error) when no hourly inputs are present.
        """
        output_file = Path(output_file)
        if (
            skip_if_exists
            and output_file.exists()
            and output_file.stat().st_size > min_existing_size
        ):
            return DownsampleResult(
                station_id,
                output_file,
                "skipped_exists",
                size_bytes=output_file.stat().st_size,
            )

        existing = [Path(f) for f in source_files if Path(f).exists()]
        if not existing:
            logger.info("No 1Hz source files for %s, skipping downsample", station_id)
            return DownsampleResult(station_id, output_file, "no_source")

        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            obs_files = [self._to_obs(src, workdir) for src in existing]
            # Derive the daily obs name from the target so RNX2CRX/compress reproduce
            # exactly output_file.name (e.g. GONH1620.26D.Z -> obs GONH1620.26O).
            daily_obs = workdir / _obs_name_for_target(output_file.name)
            self._decimate(obs_files, daily_obs)
            produced = self._to_hatanaka_compressed(daily_obs, workdir)
            output_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(produced), str(output_file))
        except Exception as e:  # noqa: BLE001 - report any tool failure as a result
            logger.error("Downsample failed for %s: %s", station_id, e)
            return DownsampleResult(
                station_id, output_file, "failed", len(existing), error=str(e)
            )

        size = output_file.stat().st_size if output_file.exists() else 0
        if size < self.min_output_size:
            return DownsampleResult(
                station_id,
                output_file,
                "failed",
                len(existing),
                size_bytes=size,
                error=f"output too small ({size} b)",
            )
        logger.info(
            "Downsampled %s: %d hourly files -> %s (%d b)",
            station_id,
            len(existing),
            output_file.name,
            size,
        )
        return DownsampleResult(station_id, output_file, "created", len(existing), size)

    # -- pipeline steps -----------------------------------------------------

    def _to_obs(self, src: Path, workdir: Path) -> Path:
        """Decompress (if needed) then CRX2RNX a hourly Hatanaka file → .obs."""
        src = Path(src)
        if src.suffix in (".Z", ".gz"):
            decompressed = workdir / src.stem  # drop .Z/.gz
            if self._run([*self.compressor[:1], "-dc", str(src)], decompressed) != 0:
                raise RuntimeError(f"decompress failed: {src}")
            crx = decompressed
        else:
            crx = workdir / src.name
            shutil.copy(str(src), str(crx))
        # CRX2RNX preserves the case of the trailing char (.??D -> .??O,
        # .??d -> .??o). BNC writes uppercase-O RINEX2, so stream-ingested
        # hourly files are .??D.Z; hardcoding lowercase "o" here pointed teqc at
        # a non-existent path and broke every stream downsample.
        last = crx.suffix[-1]
        obs = crx.with_suffix(crx.suffix[:-1] + ("O" if last == "D" else "o"))
        if self._run([self.crx2rnx, "-f", str(crx)], None) != 0:
            raise RuntimeError(f"CRX2RNX failed: {crx}")
        return obs

    def _decimate(self, obs_files: List[Path], daily_obs: Path) -> None:
        """gfzrnx concat + decimate the hourly obs files into one daily obs.

        ``-vo 2`` keeps the output RINEX 2 (the legacy ``conv1Hzrinto15s`` /
        teqc product); ``-smp`` sets the sample interval; ``-f`` overwrites.
        gfzrnx writes ``-fout`` itself (no stdout redirect, unlike teqc).
        """
        cmd = [
            self.gfzrnx,
            "-finp",
            *[str(f) for f in obs_files],
            "-fout",
            str(daily_obs),
            "-smp",
            str(self.interval),
            "-vo",
            "2",
            "-f",
        ]
        if self._run(cmd, None) != 0:
            raise RuntimeError("gfzrnx decimation failed")

    def _to_hatanaka_compressed(self, daily_obs: Path, workdir: Path) -> Path:
        """RNX2CRX the daily obs then compress → .??d.Z (gzip)."""
        if self._run([self.rnx2crx, "-f", str(daily_obs)], None) != 0:
            raise RuntimeError(f"RNX2CRX failed: {daily_obs}")
        hatanaka = daily_obs.with_name(_swap_obs_to_hatanaka(daily_obs.name))
        compressed = workdir / (hatanaka.name + ".Z")
        if self._run([*self.compressor, str(hatanaka)], compressed) != 0:
            raise RuntimeError(f"compress failed: {hatanaka}")
        return compressed


def _obs_name_for_target(target_name: str) -> str:
    """Daily obs filename for a Hatanaka target (e.g. ``GONH1620.26D.Z`` -> ``GONH1620.26O``)."""
    name = target_name
    for suffix in (".Z", ".gz"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if name and name[-1] in "dD":
        return name[:-1] + ("O" if name[-1] == "D" else "o")
    return name


def _swap_obs_to_hatanaka(obs_name: str) -> str:
    """``...O`` -> ``...D`` (preserving case of the trailing char)."""
    if obs_name and obs_name[-1] in "oO":
        return obs_name[:-1] + ("D" if obs_name[-1] == "O" else "d")
    return obs_name
