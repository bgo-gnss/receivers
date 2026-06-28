"""Convert an archived RINEX file to RINEX 3.04 with a long IGS name.

The riskiest link in the dissemination chain — it is what the legacy library
*cannot* do. The chain, for a 15s_24hr RINEX-2 Hatanaka source
(``SSSSDDD0.YYd.gz`` / ``.YYD.Z``):

    1. decompress  (.gz/.Z → CRINEX)            via ``gzip -dc``
    2. un-Hatanaka (CRINEX → RINEX-2 obs)       via ``CRX2RNX``
    3. R2 → R3 + long name                      via ``gfzrnx -vo 3``
    (4. set header from TOS — DEFERRED to T2/T3; ``set_header`` is a stub here)

The converted output is cached keyed on
``sha256(source content_sha256 + ":" + tos_fingerprint)`` — NOT the source hash
alone, so a later TOS header correction (which changes ``tos_fingerprint``)
invalidates exactly the affected files and forces a re-render. T1 passes an empty
fingerprint; the reactive ticket (T6) supplies the real one.

Tools resolve from ``$GPS_TOOLS_BIN`` → ``PATH`` (incl. uppercase) → the sibling
``gps-tools/bin/`` checkout, so it works both deployed (symlinked into
/usr/local/bin) and on a dev laptop.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..rinex.converter_base import NamingConvention, RinexVersion
from ..rinex.rinex_namer import RinexNamer
from ..utils.content_hash import content_sha256

logger = logging.getLogger(__name__)

# Repo-relative fallback: convert.py is receivers/src/receivers/dissemination/ →
# the gpslibrary_new root is parents[4], and gps-tools/bin is its sibling.
_GPS_TOOLS_BIN_FALLBACK = Path(__file__).resolve().parents[4] / "gps-tools" / "bin"


class ConvertError(RuntimeError):
    """A convert step (decompress / CRX2RNX / gfzrnx) failed."""


def resolve_tool(name: str) -> str:
    """Locate an external tool: ``$GPS_TOOLS_BIN`` → PATH → sibling gps-tools/bin.

    Also tries the uppercase variant (CRX2RNX/RNX2CRX ship uppercase).
    """
    env_bin = os.getenv("GPS_TOOLS_BIN")
    candidates = []
    for base in (env_bin, str(_GPS_TOOLS_BIN_FALLBACK)):
        if not base:
            continue
        candidates += [Path(base) / name, Path(base) / name.upper()]
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    found = shutil.which(name) or shutil.which(name.upper())
    if found:
        return found
    raise ConvertError(
        f"tool {name!r} not found (set GPS_TOOLS_BIN, add to PATH, or check "
        f"{_GPS_TOOLS_BIN_FALLBACK})"
    )


def _run(cmd: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    logger.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ConvertError(
            f"{cmd[0]} rc={proc.returncode}: {(proc.stderr or proc.stdout).strip()}"
        )
    return proc


@dataclass
class ConvertResult:
    """Outcome of one convert call."""

    output_path: Path
    long_name: str
    cached: bool  # True if served from cache (no work done)
    source_path: Path


def _strip_compression(name: str) -> tuple[str, bool]:
    """Return (name-without-compression-suffix, was_compressed)."""
    for suffix in (".gz", ".Z", ".bz2"):
        if name.endswith(suffix):
            return name[: -len(suffix)], True
    return name, False


def _crinex_to_obs_name(crinex_name: str) -> str:
    """Expected CRX2RNX output name for a CRINEX input.

    ``*.YYd`` → ``*.YYo``, ``*.YYD`` → ``*.YYO``, ``*.crx`` → ``*.rnx``.
    """
    if crinex_name.endswith(".crx"):
        return crinex_name[:-4] + ".rnx"
    if crinex_name.endswith(".CRX"):
        return crinex_name[:-4] + ".RNX"
    last = crinex_name[-1]
    if last == "d":
        return crinex_name[:-1] + "o"
    if last == "D":
        return crinex_name[:-1] + "O"
    raise ConvertError(f"cannot derive obs name from CRINEX {crinex_name!r}")


def _is_hatanaka(name: str) -> bool:
    """True if a decompressed RINEX name is Hatanaka-compressed (CRINEX)."""
    return bool(name) and (name.endswith((".crx", ".CRX")) or name[-1] in "dD")


def cache_key(source_path: Path, tos_fingerprint: str = "") -> str:
    """``sha256(source content_sha256 + ':' + tos_fingerprint)``.

    The TOS fingerprint is part of the key so a header correction (which leaves
    the source bytes unchanged) still invalidates the cached converted product.
    """
    src_hash = content_sha256(source_path)
    return hashlib.sha256(f"{src_hash}:{tos_fingerprint}".encode()).hexdigest()


def long_rinex3_name(
    station: str,
    observation_dt: datetime,
    *,
    country_code: str = "ISL",
    data_frequency: str = "15S",
    file_period: str = "01D",
) -> str:
    """The RINEX 3 long IGS filename for this station/epoch (e.g. ``...MO.rnx``)."""
    namer = RinexNamer(station, RinexVersion.RINEX_3, country_code=country_code)
    return namer.generate_filename(
        observation_dt,
        convention=NamingConvention.LONG,
        data_source="R",
        file_period=file_period,
        data_frequency=data_frequency,
        file_type="MO",
    )


def _run_rinex_chain(source_path: Path, out_path: Path) -> None:
    """Decompress → un-Hatanaka → gfzrnx R3, writing ``out_path`` atomically.

    ``source_path`` is any RINEX (plain/gz/Z, Hatanaka or not, R2 or R3). The
    output filename is ``out_path.name`` (the long IGS name).
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="epos_convert_") as tmp:
        tmpdir = Path(tmp)

        # 1. decompress (.gz/.Z → CRINEX or obs). gzip(1) handles both.
        stripped, was_compressed = _strip_compression(source_path.name)
        if was_compressed:
            decompressed = tmpdir / stripped
            with open(decompressed, "wb") as fh:
                proc = subprocess.run(
                    ["gzip", "-dc", str(source_path)], stdout=fh, stderr=subprocess.PIPE
                )
            if proc.returncode != 0:
                raise ConvertError(
                    f"decompress {source_path.name}: {proc.stderr.decode().strip()}"
                )
        else:
            decompressed = tmpdir / source_path.name
            shutil.copy2(source_path, decompressed)

        # 2. un-Hatanaka (CRINEX → RINEX obs) if needed.
        if _is_hatanaka(decompressed.name):
            crx2rnx = resolve_tool("CRX2RNX")
            _run([crx2rnx, "-f", str(decompressed)])
            obs_path = tmpdir / _crinex_to_obs_name(decompressed.name)
            if not obs_path.is_file():
                raise ConvertError(f"CRX2RNX produced no {obs_path.name}")
        else:
            obs_path = decompressed

        # 3. → R3 + long name.
        gfzrnx = resolve_tool("gfzrnx")
        tmp_out = tmpdir / out_path.name
        _run([gfzrnx, "-finp", str(obs_path), "-fout", str(tmp_out), "-vo", "3", "-f"])
        if not tmp_out.is_file():
            raise ConvertError(f"gfzrnx produced no {out_path.name}")

        shutil.move(str(tmp_out), str(out_path))


def set_header_from_tos(
    rinex_file: Path, station: str, observation_dt: datetime
) -> bool:
    """Rewrite ``rinex_file``'s header from TOS metadata (in place). Best-effort.

    Delegates to ``tostools.rinex.correct_rinex_from_tos`` with ``station_config``
    unset, so TOS is the authority for every epoch (EPOS wants the canonical TOS
    metadata, and historical re-pushes must reflect TOS as of that date). Returns
    True if a correction was applied; False (logged) on any failure — the QC gate
    is the safety net that blocks a still-wrong header from being pushed.
    """
    try:
        from tostools.rinex import correct_rinex_from_tos

        result = correct_rinex_from_tos(
            rinex_file=rinex_file,
            station_id=station.upper(),
            observation_date=observation_dt,
            output_file=rinex_file,
            station_config=None,  # force TOS (canonical for EPOS), not station.cfg
            loglevel=logging.WARNING,
        )
        return result is not None
    except Exception as exc:  # noqa: BLE001 - never fail the convert on a TOS glitch
        logger.warning("set-header-from-TOS failed for %s: %s", station, exc)
        return False


def convert_to_rinex3_long(
    source_path: Path,
    station: str,
    observation_dt: datetime,
    *,
    country_code: str = "ISL",
    data_frequency: str = "15S",
    file_period: str = "01D",
    cache_dir: Path,
    tos_fingerprint: str = "",
    set_header: bool = False,
) -> ConvertResult:
    """Convert ``source_path`` (archived RINEX) to RINEX 3.04 with a long IGS name.

    Returns a :class:`ConvertResult`; the output lives under ``cache_dir`` keyed
    on :func:`cache_key` so an unchanged (content, TOS-metadata) pair converts
    once. With ``set_header`` the cached product's header is rewritten from TOS
    (and ``tos_fingerprint`` must reflect that metadata so the cache invalidates
    on a later correction).
    """
    source_path = Path(source_path)
    long_name = long_rinex3_name(
        station,
        observation_dt,
        country_code=country_code,
        data_frequency=data_frequency,
        file_period=file_period,
    )

    key = cache_key(source_path, tos_fingerprint)
    out_dir = cache_dir.expanduser() / key
    out_path = out_dir / long_name
    if out_path.is_file():
        logger.info("convert cache hit %s → %s", source_path.name, long_name)
        return ConvertResult(out_path, long_name, cached=True, source_path=source_path)

    out_dir.mkdir(parents=True, exist_ok=True)
    _run_rinex_chain(source_path, out_path)
    if set_header:
        set_header_from_tos(out_path, station, observation_dt)
    logger.info("converted %s → %s", source_path.name, long_name)
    return ConvertResult(out_path, long_name, cached=False, source_path=source_path)


# Raw extensions handled by the raw→rinex fallback (Trimble only in this build).
_TRIMBLE_RAW = (".t02", ".t00")


def convert_raw_to_rinex3_long(
    raw_path: Path,
    station: str,
    observation_dt: datetime,
    *,
    country_code: str = "ISL",
    data_frequency: str = "15S",
    file_period: str = "01D",
    cache_dir: Path,
    tos_fingerprint: str = "",
    set_header: bool = False,
) -> ConvertResult:
    """Convert an archived **raw** file to RINEX 3.04 with a long IGS name.

    The fallback when no archived RINEX exists: decode the raw with the existing
    receivers converter (Trimble ``.T02``/``.T00`` → RINEX 2 via runpkr00+teqc),
    then run the shared chain to the long-name R3 product. Cache key is on the
    raw source. Septentrio ``.sbf`` is not wired here yet (current production is
    Trimble for the raw-only stations).
    """
    raw_path = Path(raw_path)
    name_l = raw_path.name.lower()
    if not any(ext in name_l for ext in _TRIMBLE_RAW):
        raise ConvertError(
            f"raw→rinex supports only Trimble T02/T00 in this build; got {raw_path.name}"
        )

    long_name = long_rinex3_name(
        station,
        observation_dt,
        country_code=country_code,
        data_frequency=data_frequency,
        file_period=file_period,
    )
    key = cache_key(raw_path, tos_fingerprint)
    out_dir = cache_dir.expanduser() / key
    out_path = out_dir / long_name
    if out_path.is_file():
        logger.info("convert cache hit (raw) %s → %s", raw_path.name, long_name)
        return ConvertResult(out_path, long_name, cached=True, source_path=raw_path)

    out_dir.mkdir(parents=True, exist_ok=True)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="epos_raw_") as tmp:
        tmpdir = Path(tmp)
        decoded = _decode_trimble_raw(raw_path, station, observation_dt, tmpdir)
        # Normalise whatever the decoder produced (native R3 .gz, or legacy R2 .crx)
        # through the shared chain to our canonical plain-.rnx long name.
        _run_rinex_chain(decoded, out_path)
    if set_header:
        set_header_from_tos(out_path, station, observation_dt)

    logger.info("converted (raw) %s → %s", raw_path.name, long_name)
    return ConvertResult(out_path, long_name, cached=False, source_path=raw_path)


def _decode_trimble_raw(
    raw_path: Path, station: str, observation_dt: datetime, out_dir: Path
) -> Path:
    """Decode a Trimble .T02/.T00 to a RINEX file in ``out_dir`` and return it.

    Prefers the production native converter (official Trimble Convert-to-RINEX via
    the ``trm2rinex`` Docker image — the path the live download pipeline uses);
    falls back to the legacy runpkr00+teqc converter when Docker is unavailable.
    No header corrections (the QC gate verifies; set-header is a later ticket).
    """
    from ..rinex.trimble_native_converter import TrimbleNativeConverter

    if TrimbleNativeConverter.is_available():
        conv: Any = TrimbleNativeConverter(
            station,
            rinex_version=RinexVersion.RINEX_3,
            apply_header_corrections=False,
            loglevel=logging.WARNING,
        )
    else:
        # Legacy fallback. NOTE: runpkr00+teqc does NOT handle some T02/T00 files
        # (returns rc=30 / no output) — the native Docker converter is the
        # production path. This branch exists only for Docker-less environments.
        from ..rinex.trimble_converter import TrimbleConverter

        logger.warning(
            "trm2rinex Docker image unavailable — falling back to legacy "
            "runpkr00+teqc, which fails on some Trimble files"
        )
        conv = TrimbleConverter(
            station,
            rinex_version=RinexVersion.RINEX_2,
            apply_header_corrections=False,
            loglevel=logging.WARNING,
        )

    result = conv.convert_file(
        raw_path, output_dir=out_dir, observation_date=observation_dt
    )
    if not result.success or not result.rinex_file:
        raise ConvertError(
            f"Trimble decode failed for {raw_path.name}: {result.message}"
        )
    return Path(result.rinex_file)
