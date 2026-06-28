"""EposDisseminate — the T1 tracer: convert one (station, date) and push it.

For one station and date: resolve the archived RINEX (prefer existing RINEX;
rinex-from-raw is a later ticket), convert it to a RINEX 3.04 long-name file
(:mod:`receivers.dissemination.convert`), and rsync it to the dissemination
target's dest (a staging path in T1). Dry-run does everything but the rsync write.

This proves the riskiest end-to-end path with the fewest moving parts: no DB, no
TOS include-filter, no QC gate (those are T2-T5).
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .config import DisseminationTarget
from .convert import (
    ConvertError,
    convert_raw_to_rinex3_long,
    convert_to_rinex3_long,
)
from .qc_gate import qc_check
from .tos_access import session_fingerprint

# Supplies the TOS session (one device_history entry) for (station, observation
# datetime), or None if TOS has no coverage. T1/T2 leave it None (gate skipped);
# T3's TOS access layer injects the live provider.
SessionProvider = Callable[[str, datetime], Optional[dict[str, Any]]]

logger = logging.getLogger("receivers.dissemination")

# Month dir component matches the archive layout (lowercase 3-letter English).
_MONTHS = [
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
]


@dataclass
class DisseminateResult:
    """Outcome of one EposDisseminate.run_one()."""

    station: str
    file_date: date
    ok: bool = False
    dry_run: bool = False
    source_path: Optional[str] = None
    long_name: Optional[str] = None
    cached: bool = False
    pushed: bool = False
    artifact_path: Optional[str] = None  # local converted R3 file (for indexing)
    qc_passed: Optional[bool] = None  # None = gate not run (no session provider)
    qc_message: str = ""
    dest: Optional[str] = None
    message: str = ""
    errors: list[str] = field(default_factory=list)


class EposDisseminate:
    """Run the dissemination pipeline for explicit (station, date) inputs."""

    def __init__(
        self,
        target: DisseminationTarget,
        *,
        dry_run: bool = False,
        dest_override: Optional[str] = None,
        session_provider: Optional[SessionProvider] = None,
        set_header: bool = True,
    ) -> None:
        self.target = target
        self.dry_run = dry_run
        self.dest_override = dest_override
        # When set, the header-QC gate runs before every push (T3 injects it).
        self.session_provider = session_provider
        # Rewrite the converted header from TOS before caching/QC (needs a session
        # provider to fingerprint the TOS metadata for the cache key). Tests pass
        # set_header=False to stay offline.
        self.set_header = set_header

    # ---- source resolution -------------------------------------------------

    def _station_session_dir(self, station: str, d: date, session: str) -> Path:
        return (
            Path(self.target.source_root)
            / f"{d.year:04d}"
            / _MONTHS[d.month - 1]
            / station.upper()
            / session
        )

    def _rinex_dir(self, station: str, d: date, session: str) -> Path:
        return self._station_session_dir(station, d, session) / "rinex"

    def find_source(self, station: str, d: date) -> Optional[Path]:
        """Locate the archived RINEX for (station, date), any compression/case.

        Prefers an existing RINEX file. (rinex-from-raw fallback is a later
        ticket — logged, not implemented, in T1.)
        """
        doy = d.timetuple().tm_yday
        yy = d.year % 100
        for session in self.target.sessions or ("15s_24hr",):
            rinex_dir = self._rinex_dir(station, d, session)
            if not rinex_dir.is_dir():
                continue
            # RINEX-2 short stem: SSSSDDD0  (daily session char '0').
            stem = f"{station.upper()}{doy:03d}0"
            for pattern in (
                f"{stem}.{yy:02d}[dD]*",  # Hatanaka (.YYd/.YYD[.gz/.Z])
                f"{stem}.{yy:02d}[oO]*",  # plain obs
                f"{station.upper()}*{doy:03d}*_??_MO.???*",  # already long-name R3
            ):
                hits = sorted(rinex_dir.glob(pattern))
                if hits:
                    return hits[0]
        return None

    def find_raw_source(self, station: str, d: date) -> Optional[Path]:
        """Locate the archived raw file for (station, date) — the rinex fallback.

        Trimble ``.T02``/``.T00`` (any compression). Septentrio ``.sbf`` is found
        too, but the converter for it is not wired yet (handled at convert time).
        """
        for session in self.target.sessions or ("15s_24hr",):
            raw_dir = self._station_session_dir(station, d, session) / "raw"
            if not raw_dir.is_dir():
                continue
            for pattern in ("*.T02*", "*.T00*", "*.t02*", "*.t00*", "*.sbf*"):
                hits = sorted(raw_dir.glob(pattern))
                if hits:
                    return hits[0]
        return None

    # ---- push --------------------------------------------------------------

    @property
    def _dest_base(self) -> str:
        dest = (
            self.dest_override if self.dest_override is not None else self.target.dest
        )
        if not self.target.host:
            return dest
        return f"{self.target.user}@{self.target.host}:{dest}"

    def _push(self, local_file: Path) -> bool:
        """rsync one converted file to the dest dir. Returns True if transferred."""
        dest = self._dest_base.rstrip("/") + "/"
        # Local dest: ensure the dir exists (remote dirs are the server's job).
        if not self.target.host and not self.dry_run:
            os.makedirs(self.dest_override or self.target.dest, exist_ok=True)
        cmd = ["rsync", "-a", "--itemize-changes"]
        if self.dry_run:
            cmd.append("--dry-run")
        cmd += [str(local_file), dest]
        logger.info(
            "rsync %s → %s%s",
            local_file.name,
            dest,
            " [dry-run]" if self.dry_run else "",
        )
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise ConvertError(f"rsync rc={proc.returncode}: {proc.stderr.strip()}")
        # itemize prints a line per changed file; empty ⇒ nothing transferred.
        return any(
            ln[:1] in "<>" for ln in proc.stdout.splitlines() if len(ln.split()) == 2
        )

    # ---- orchestration -----------------------------------------------------

    def run_one(self, station: str, d: date) -> DisseminateResult:
        result = DisseminateResult(
            station=station.upper(), file_date=d, dry_run=self.dry_run
        )

        if station.upper() in self.target.exclude_stations:
            result.ok = True
            result.message = "station excluded from this target"
            return result

        # Source precedence: prefer archived RINEX, else fall back to archived raw.
        obs_dt = datetime(d.year, d.month, d.day)
        source = self.find_source(station, d)
        raw_source = None if source is not None else self.find_raw_source(station, d)
        if source is None and raw_source is None:
            result.message = "no archived RINEX or raw found"
            result.errors.append(result.message)
            return result
        result.source_path = str(source or raw_source)

        # Fetch the TOS session ONCE (before convert): it drives both the
        # set-header step's cache fingerprint and the QC gate. set-header only runs
        # when we have a session (so the fingerprint reflects the metadata used).
        session = (
            self.session_provider(station.upper(), obs_dt)
            if self.session_provider is not None
            else None
        )
        do_set_header = self.set_header and session is not None
        fingerprint = session_fingerprint(session) if do_set_header else ""

        try:
            if source is not None:
                conv = convert_to_rinex3_long(
                    source,
                    station.upper(),
                    obs_dt,
                    country_code=self.target.country_code,
                    cache_dir=self.target.cache_path,
                    tos_fingerprint=fingerprint,
                    set_header=do_set_header,
                )
            else:
                assert raw_source is not None
                conv = convert_raw_to_rinex3_long(
                    raw_source,
                    station.upper(),
                    obs_dt,
                    country_code=self.target.country_code,
                    cache_dir=self.target.cache_path,
                    tos_fingerprint=fingerprint,
                    set_header=do_set_header,
                )
        except ConvertError as exc:
            result.message = f"convert failed: {exc}"
            result.errors.append(str(exc))
            return result

        result.long_name = conv.long_name
        result.cached = conv.cached
        result.artifact_path = str(conv.output_path)
        result.dest = self._dest_base

        # Header-QC gate: never push a file whose header disagrees with TOS on a
        # semantic field. Skipped (qc_passed stays None) when no session provider
        # is wired. Reuses the session fetched above (no second TOS round-trip).
        if self.session_provider is not None:
            verdict = qc_check(conv.output_path, session)
            result.qc_passed = verdict.passed
            result.qc_message = verdict.message
            if not verdict.passed:
                result.message = f"QC gate failed: {verdict.message}"
                result.errors.append(result.message)
                return result
        else:
            logger.debug("QC gate skipped for %s (no TOS session provider)", station)

        try:
            transferred = self._push(conv.output_path)
        except ConvertError as exc:
            result.message = f"push failed: {exc}"
            result.errors.append(str(exc))
            return result

        result.pushed = transferred and not self.dry_run
        result.ok = True
        verb = (
            "would push"
            if self.dry_run
            else ("pushed" if transferred else "up-to-date")
        )
        cache_note = " (cached)" if conv.cached else ""
        result.message = f"{verb} {conv.long_name}{cache_note}"
        return result
