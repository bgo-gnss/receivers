"""Downsample 1 Hz hourly RINEX to a decimated daily RINEX file.

Port of the legacy ``conv1Hzrinto15s.sh``: for a station+day, take the (up to 24)
hourly 1 Hz RINEX files produced by BNC stream capture and build a single daily
RINEX file decimated to a coarser interval (default 15 s), in the fleet's
Hatanaka-compressed form.

Pipeline per day (mirrors the legacy ``uthjap`` / ``teqc`` / ``thjap`` chain with
the modern toolchain)::

    each hourly  <name>.YYd.Z  ──gzip -d──▶ <name>.YYd ──CRX2RNX──▶ <name>.YYo
    all hourly .YYo  ──gfzrnx -smp N -vo 3──▶ daily R3  ──gfzrnx -vo 2──▶ daily .YYo
    daily .YYo                 ──RNX2CRX──▶ daily .YYd ──compress──▶ daily .YYd.Z

The product is RINEX 2.11 (bgo, 2026-10-14). BNC writes RINEX 3.04 headers with
RINEX-2 style 2-char obs codes (``C1 P1 L1 S1 C2 P2 L2 S2``); in R3 that is
non-standard, and the streamed content is GPS+GLONASS C/L/S on L1/L2 only, so
R2 carries all of it in a valid file. The R2 step is separate because gfzrnx
crashes (``RINEX_DB.pm line 3663``) when splice, ``-smp`` and ``-vo 2`` are
combined in one call on these inputs; each step alone is fine.

A daily that already exists is rebuilt only when it was itself produced from the
stream (``PGM / RUN BY / DATE`` says BNC) AND an hourly source is newer than it.
The cycle builds today's daily while the day is still filling; the old size-only
skip (>300 kB) froze that morning snapshot for good — SEY9 day 182 of 2026 sat in
the archive covering 00:00-07:59 out of a 24-hour day. A daily from any other
program (the downloaded SBF via sbf2rin) is authoritative and is never replaced.

External-command execution goes through a single injectable ``runner`` so the
orchestration (skip-if-exists, no-source handling, command sequencing, output
placement) is fully unit-testable without teqc / CRX2RNX / RNX2CRX present.

NOTE: exact tool flags and the final compression format (.Z) must be validated
against the real tools and the on-disk archive on rek-d01 before production use.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: A runner executes a command, optionally redirecting stdout to a file.
#: Returns the process return code.
Runner = Callable[[Sequence[str], Optional[Path]], int]

#: Minimum plausible size (bytes) for a valid decimated daily file.
DEFAULT_MIN_OUTPUT_SIZE = 1000

#: Reads a compressed RINEX file's ``PGM / RUN BY / DATE`` value, or None when
#: the header cannot be read.
HeaderProbe = Callable[[Path], Optional[str]]


def read_rinex_pgm(path: Path) -> Optional[str]:
    """``PGM / RUN BY / DATE`` program field of a (.Z/.gz, Hatanaka) RINEX file.

    Only the header is needed, so the decompressed stream is cut after
    ``END OF HEADER``. Hatanaka keeps the RINEX header lines verbatim, so no
    CRX2RNX is needed. Returns None when the file cannot be read.
    """
    try:
        proc = subprocess.Popen(
            ["gzip", "-dc", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    pgm: Optional[str] = None
    try:
        assert proc.stdout is not None
        for i, raw in enumerate(proc.stdout):
            line = raw.decode("latin-1")
            if "PGM / RUN BY / DATE" in line:
                pgm = line[:20].strip()
                break
            if "END OF HEADER" in line or i > 200:
                break
    finally:
        proc.kill()
        proc.wait()
    return pgm


def is_stream_product(pgm: Optional[str]) -> bool:
    """True when a RINEX ``PGM`` value marks a BNC (stream-capture) product."""
    return pgm is not None and pgm.upper().startswith("BNC")


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
    # created | skipped_exists | skipped_authoritative | no_source | failed
    status: str
    source_count: int = 0
    size_bytes: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in ("created", "skipped_exists", "skipped_authoritative")


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
        rinex_version: int = 2,
        header_probe: Optional[HeaderProbe] = None,
    ):
        if rinex_version not in (2, 3):
            raise ValueError(f"rinex_version must be 2 or 3, not {rinex_version}")
        self.interval = interval
        self.rinex_version = rinex_version
        self._probe: HeaderProbe = header_probe or read_rinex_pgm
        self.crx2rnx = crx2rnx
        self.rnx2crx = rnx2crx
        # gfzrnx, not teqc, does the concat+decimate: teqc 2019 hardcodes the
        # GLONASS slot max at 24 and aborts on modern slots (R25+) that appear in
        # BNC stream RINEX, failing the whole day. gfzrnx handles them.
        self.gfzrnx = gfzrnx
        # .Z output MUST be real LZW compress(1) — the gzip-as-.Z era
        # (fixed fleet-wide 2026-07-06) also leaked from here: this default
        # was gzip, so every stream-capture product was a gzip impostor that
        # the archive-sync format guard now refuses. Decompression stays
        # gzip -dc (reads BOTH gzip and LZW), so legacy gzip-.Z inputs from
        # the transition keep working.
        self.compressor = list(compressor) if compressor else ["compress", "-c"]
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

        An existing target is kept when it is not a stream product (status
        ``skipped_authoritative``: a downloaded-SBF daily outranks any splice), or
        when it is a stream product at least as new as every hourly source and
        plausibly complete (``skipped_exists``). Otherwise it is rebuilt, so a
        daily built while its day was still filling is completed on a later cycle.
        ``skip_if_exists=False`` rebuilds regardless. Returns a ``no_source``
        result (not an error) when no hourly inputs are present.
        """
        output_file = Path(output_file)
        existing = [Path(f) for f in source_files if Path(f).exists()]
        if skip_if_exists and output_file.exists():
            size = output_file.stat().st_size
            pgm = self._probe(output_file)
            if not is_stream_product(pgm):
                # Unknown (unreadable header) fails closed too: never overwrite
                # what we cannot prove is ours.
                return DownsampleResult(
                    station_id,
                    output_file,
                    "skipped_authoritative",
                    size_bytes=size,
                )
            newest_source = max((f.stat().st_mtime for f in existing), default=0.0)
            if (
                size > min_existing_size
                and output_file.stat().st_mtime >= newest_source
            ):
                return DownsampleResult(
                    station_id, output_file, "skipped_exists", size_bytes=size
                )
            logger.info(
                "Rebuilding stream daily %s: hourly sources are newer", output_file.name
            )

        if not existing:
            logger.info("No 1Hz source files for %s, skipping downsample", station_id)
            return DownsampleResult(station_id, output_file, "no_source")

        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        # Per-call scratch, removed on exit: 24 decompressed 1 Hz hours are
        # ~150 MB per station-day, and a backfill runs hundreds of days.
        scratch = Path(tempfile.mkdtemp(prefix=f"{station_id}_", dir=workdir))
        try:
            obs_files = [self._to_obs(src, scratch) for src in existing]
            # Derive the daily obs name from the target so RNX2CRX/compress reproduce
            # exactly output_file.name (e.g. GONH1620.26D.Z -> obs GONH1620.26O).
            daily_obs = scratch / _obs_name_for_target(output_file.name)
            self._decimate(obs_files, daily_obs)
            produced = self._to_hatanaka_compressed(daily_obs, scratch)
            output_file.parent.mkdir(parents=True, exist_ok=True)
            # Land beside the target, then rename: a rebuild replaces a live
            # archive file, and a reader must never see it half-written.
            landing = output_file.with_name(f".{output_file.name}.tmp")
            shutil.move(str(produced), str(landing))
            os.replace(landing, output_file)
        except Exception as e:  # noqa: BLE001 - report any tool failure as a result
            logger.error("Downsample failed for %s: %s", station_id, e)
            return DownsampleResult(
                station_id, output_file, "failed", len(existing), error=str(e)
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

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
            if self._run(["gzip", "-dc", str(src)], decompressed) != 0:
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

        Step 1 splices and samples to ``-smp`` into RINEX 3 (``-f`` overwrites;
        gfzrnx writes ``-fout`` itself, no stdout redirect). For a RINEX 2
        product, step 2 converts that file with ``-vo 2``. The two steps cannot be
        merged: gfzrnx dies in ``RINEX_DB.pm`` on splice + ``-smp`` + ``-vo 2``.
        """
        spliced = (
            daily_obs
            if self.rinex_version == 3
            else daily_obs.with_name(daily_obs.name + ".r3")
        )
        cmd = [
            self.gfzrnx,
            "-finp",
            *[str(f) for f in obs_files],
            "-fout",
            str(spliced),
            "-smp",
            str(self.interval),
            "-vo",
            "3",
            "-f",
        ]
        if self._run(cmd, None) != 0:
            raise RuntimeError("gfzrnx decimation failed")
        if self.rinex_version == 3:
            return
        cmd = [self.gfzrnx, "-finp", str(spliced), "-fout", str(daily_obs)]
        cmd += ["-vo", "2", "-f"]
        if self._run(cmd, None) != 0:
            raise RuntimeError("gfzrnx RINEX 2 conversion failed")
        spliced.unlink(missing_ok=True)

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
