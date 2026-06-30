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
from typing import Any, Optional

from ..rinex.converter_base import NamingConvention, RinexVersion
from ..rinex.rinex_namer import RinexNamer
from ..utils.content_hash import content_sha256

logger = logging.getLogger(__name__)

# Repo-relative fallback: convert.py is receivers/src/receivers/dissemination/ →
# the gpslibrary_new root is parents[4], and gps-tools/bin is its sibling.
_GPS_TOOLS_BIN_FALLBACK = Path(__file__).resolve().parents[4] / "gps-tools" / "bin"


class ConvertError(RuntimeError):
    """A convert step (decompress / CRX2RNX / gfzrnx / packaging) failed."""


# Raw extensions handled by the raw→rinex fallback (Trimble only in this build).
_TRIMBLE_RAW = (".t02", ".t00")


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
    """Outcome of one convert call — the cached canonical *plain obs* file.

    Packaging (Hatanaka / compression) and the published filename are applied
    separately by :func:`package` / :func:`published_name`, per the version policy.
    """

    output_path: Path  # cached plain RINEX obs (.rnx for R3, .YYo for R2)
    obs_name: str
    rinex_version: int  # the SOURCE's version, preserved (Model B)
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
    monument_number: str = "00",
) -> str:
    """The RINEX 3 long IGS filename for this station/epoch (e.g. ``...MO.rnx``)."""
    namer = RinexNamer(
        station,
        RinexVersion.RINEX_3,
        country_code=country_code,
        monument_number=monument_number,
    )
    return namer.generate_filename(
        observation_dt,
        convention=NamingConvention.LONG,
        data_source="R",
        file_period=file_period,
        data_frequency=data_frequency,
        file_type="MO",
    )


def _obs_to_crinex_name(name: str) -> str:
    """Inverse of :func:`_crinex_to_obs_name`: obs → CRINEX (.rnx→.crx, .YYo→.YYd)."""
    if name.endswith(".rnx"):
        return name[:-4] + ".crx"
    if name.endswith(".RNX"):
        return name[:-4] + ".CRX"
    last = name[-1]
    if last == "o":
        return name[:-1] + "d"
    if last == "O":
        return name[:-1] + "D"
    raise ConvertError(f"cannot derive CRINEX name from obs {name!r}")


def detect_rinex_version(obs_path: Path) -> int:
    """The major RINEX version (2 or 3) from a plain obs file's first header line."""
    with open(obs_path, encoding="utf-8", errors="ignore") as fh:
        first = fh.readline()
    try:
        return int(float(first[:9].strip()))
    except ValueError as exc:
        raise ConvertError(f"no RINEX version in {obs_path.name}: {first!r}") from exc


def detect_interval(obs_path: Path) -> Optional[float]:
    """The observation sampling interval (seconds) from the ``INTERVAL`` header.

    Returns ``None`` when the (optional) INTERVAL record is absent — callers fall
    back to the configured ``sample`` or a logged default rather than guessing.
    """
    with open(obs_path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            label = line[60:].strip()
            if label.startswith("INTERVAL"):
                try:
                    return float(line[:10])
                except ValueError:
                    return None
            if label.startswith("END OF HEADER"):
                break
    return None


def _frequency_token(interval_seconds: float) -> str:
    """RINEX 3 long-name data-frequency token, e.g. 15 → ``15S``, 1 → ``01S``."""
    return f"{int(round(interval_seconds)):02d}S"


def _resolve_data_frequency(plain_obs: Path, sample: Optional[int]) -> str:
    """Frequency token: the config ``sample`` override, else the file's INTERVAL.

    Never invents a rate silently — if neither is known we log and fall back to
    ``15S`` (the legacy default) so the filename is at least deterministic, but
    the warning flags that the source had no INTERVAL record.
    """
    if sample is not None:
        return _frequency_token(sample)
    interval = detect_interval(plain_obs)
    if interval is not None:
        return _frequency_token(interval)
    logger.warning(
        "no INTERVAL in %s and no configured sample — naming as 15S", plain_obs.name
    )
    return "15S"


def _to_plain_obs(src: Path, tmpdir: Path) -> Path:
    """Decompress (.gz/.Z) and un-Hatanaka ``src`` into a plain obs in ``tmpdir``."""
    stripped, was_compressed = _strip_compression(src.name)
    if was_compressed:
        dec = tmpdir / stripped
        with open(dec, "wb") as fh:
            proc = subprocess.run(
                ["gzip", "-dc", str(src)], stdout=fh, stderr=subprocess.PIPE
            )
        if proc.returncode != 0:
            raise ConvertError(f"decompress {src.name}: {proc.stderr.decode().strip()}")
    else:
        dec = tmpdir / src.name
        if dec != src:
            shutil.copy2(src, dec)

    if _is_hatanaka(dec.name):
        crx2rnx = resolve_tool("CRX2RNX")
        _run([crx2rnx, "-f", str(dec)])
        obs = tmpdir / _crinex_to_obs_name(dec.name)
        if not obs.is_file():
            raise ConvertError(f"CRX2RNX produced no {obs.name}")
        return obs
    return dec


def _short_obs_name(station: str, observation_dt: datetime, country_code: str) -> str:
    namer = RinexNamer(station, RinexVersion.RINEX_2, country_code=country_code)
    return namer.generate_filename(
        observation_dt, convention=NamingConvention.SHORT, file_type="o"
    )


def _canonical_obs(
    plain_obs: Path,
    station: str,
    observation_dt: datetime,
    version: int,
    naming: str,
    country_code: str,
    tmpdir: Path,
    *,
    sample: Optional[int] = None,
    file_period: str = "01D",
    monument_number: str = "00",
) -> tuple[Path, str]:
    """Rename/normalise ``plain_obs`` to the policy's canonical obs name.

    R3 long → gfzrnx ``-vo {version}`` (version PRESERVED — Model B never up/down-
    converts) writing the long IGS name, with the data-frequency token derived from
    the obs (config ``sample`` override else the file's INTERVAL). R2 short → plain
    rename, no gfzrnx, so the R2 product is byte-for-byte the source obs.

    When ``sample`` is set the obs is decimated to that interval via ``gfzrnx -smp``
    (both versions) — this is the only case the R2 product is rewritten, and it is
    the dissemination-boundary 30s-product path, never the archive.
    """
    data_frequency = _resolve_data_frequency(plain_obs, sample)
    gfzrnx = resolve_tool("gfzrnx")

    if naming == "long":
        obs_name = long_rinex3_name(
            station,
            observation_dt,
            country_code=country_code,
            data_frequency=data_frequency,
            file_period=file_period,
            monument_number=monument_number,
        )
        out = tmpdir / obs_name
        cmd = [gfzrnx, "-finp", str(plain_obs), "-fout", str(out), "-vo", str(version)]
        if sample is not None:
            cmd += ["-smp", str(sample)]
        cmd.append("-f")
        _run(cmd)
        if not out.is_file():
            raise ConvertError(f"gfzrnx produced no {obs_name}")
        return out, obs_name

    obs_name = _short_obs_name(station, observation_dt, country_code)
    out = tmpdir / obs_name
    if sample is not None:
        # Decimate the R2 product too (the only case it is not byte-for-byte).
        _run(
            [
                gfzrnx,
                "-finp",
                str(plain_obs),
                "-fout",
                str(out),
                "-vo",
                str(version),
                "-smp",
                str(sample),
                "-f",
            ]
        )
        if not out.is_file():
            raise ConvertError(f"gfzrnx produced no {obs_name}")
        return out, obs_name
    if out != plain_obs:
        shutil.move(str(plain_obs), str(out))
    return out, obs_name


def convert_for_dissemination(
    source_path: Path,
    station: str,
    observation_dt: datetime,
    *,
    fmt,
    cache_dir: Path,
    tos_fingerprint: str = "",
    set_header: bool = False,
    domes: str = "",
) -> ConvertResult:
    """Produce the cached canonical plain obs for dissemination (Model B).

    The source's RINEX version is preserved; naming follows ``fmt`` (R2→short,
    R3→long). Output is cached under ``cache_dir/<key>/`` keyed on
    :func:`cache_key` (content + TOS fingerprint). Packaging (Hatanaka/compression)
    is applied later by :func:`package`. ``set_header`` rewrites the header from TOS.
    """
    source_path = Path(source_path)
    is_raw = any(e in source_path.name.lower() for e in _TRIMBLE_RAW + (".sbf",))

    key = cache_key(source_path, tos_fingerprint)
    out_dir = cache_dir.expanduser() / key
    if out_dir.is_dir():
        existing = [p for p in out_dir.iterdir() if p.is_file()]
        if existing:
            obs = existing[0]
            logger.info("convert cache hit %s → %s", source_path.name, obs.name)
            return ConvertResult(
                obs, obs.name, detect_rinex_version(obs), True, source_path
            )

    out_dir.mkdir(parents=True, exist_ok=True)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="epos_convert_") as tmp:
        tmpdir = Path(tmp)
        if is_raw:
            decoded = _decode_trimble_raw(source_path, station, observation_dt, tmpdir)
            plain = _to_plain_obs(decoded, tmpdir)
        else:
            plain = _to_plain_obs(source_path, tmpdir)
        version = detect_rinex_version(plain)
        naming = fmt.policy_for(version).naming
        canon, obs_name = _canonical_obs(
            plain,
            station,
            observation_dt,
            version,
            naming,
            fmt.country_code,
            tmpdir,
            sample=getattr(fmt, "sample", None),
            file_period=getattr(fmt, "file_period", "01D"),
            monument_number=getattr(fmt, "monument_number", "00"),
        )
        final_obs = out_dir / obs_name
        shutil.move(str(canon), str(final_obs))

    if set_header:
        set_header_from_tos(final_obs, station, observation_dt)
        # EPOS-specific header finalization (4.1.7) that the general TOS corrector
        # does not do: 9-char MARKER NAME (R3), DOMES in MARKER NUMBER, generic
        # OBSERVER/AGENCY. Done here so the cached product is EPOS-complete.
        finalize_epos_header(
            final_obs,
            station,
            version,
            country_code=fmt.country_code,
            monument_number=getattr(fmt, "monument_number", "00"),
            domes=domes,
            observer=getattr(fmt, "observer", ""),
            agency=getattr(fmt, "agency", ""),
        )
    logger.info(
        "converted %s → %s (RINEX %d)", source_path.name, final_obs.name, version
    )
    return ConvertResult(final_obs, obs_name, version, False, source_path)


def epos_marker_name(
    station: str, version: int, country_code: str, monument_number: str = "00"
) -> str:
    """EPOS MARKER NAME: 9-char ID for RINEX 3 (``RHOF00ISL``), 4-char for R2.

    Per EPOS 4.1.7 — "the 9-character station ID (4-character for RINEX 2 data)
    must be found in the MARKER NAME field". ``monument_number`` and
    ``country_code`` come from config (will become per-station TOS attributes);
    the 9-char ID here MUST match the long filename's, so both read the same knobs.
    """
    sid = station.upper()
    mon = str(monument_number)[:2].rjust(2, "0")
    return f"{sid}{mon}{country_code.upper()}" if version >= 3 else sid


def _set_header_records(rinex_file: Path, records: dict[str, str]) -> None:
    """Update-or-insert fixed-column header records ``{label: value(cols 1-60)}``.

    RINEX header lines are ``value[1:60] + label[61:80]``. Existing records with a
    matching label are rewritten; any not present are inserted just before
    ``END OF HEADER`` (in dict order). Idempotent.
    """
    lines = rinex_file.read_text().splitlines(keepends=True)
    out: list[str] = []
    applied: set[str] = set()
    for line in lines:
        label = line[60:80].strip() if len(line) >= 61 else ""
        if label == "END OF HEADER":
            for lbl, val in records.items():
                if lbl not in applied:
                    out.append(f"{val:<60}{lbl:<20}\n")
                    applied.add(lbl)
            out.append(line)
            continue
        if label in records and label not in applied:
            out.append(f"{records[label]:<60}{label:<20}\n")
            applied.add(label)
            continue
        out.append(line)
    rinex_file.write_text("".join(out))


def finalize_epos_header(
    rinex_file: Path,
    station: str,
    version: int,
    *,
    country_code: str,
    monument_number: str = "00",
    domes: str = "",
    observer: str = "",
    agency: str = "",
) -> None:
    """Write the EPOS-mandated marker / observer header records (4.1.7).

    - MARKER NAME  ← 9-char ID (R3) / 4-char (R2)
    - MARKER NUMBER ← DOMES when known, else the 4-char ID
    - OBSERVER / AGENCY ← generic team name + agency (never personal initials)

    Best-effort like :func:`set_header_from_tos`; the QC gate is the safety net.
    """
    try:
        sid = station.upper()
        records = {
            "MARKER NAME": epos_marker_name(
                sid, version, country_code, monument_number
            ),
            "MARKER NUMBER": (domes or sid).strip(),
        }
        if observer or agency:
            records["OBSERVER / AGENCY"] = f"{observer:<20}{agency:<40}"
        _set_header_records(rinex_file, records)
    except Exception as exc:  # noqa: BLE001 - never fail convert on a header write
        logger.warning("EPOS header finalize failed for %s: %s", station, exc)


def published_name(obs_name: str, policy) -> str:
    """The published filename for an obs name under a :class:`VersionPolicy`."""
    name = _obs_to_crinex_name(obs_name) if policy.hatanaka else obs_name
    ext = {"gz": ".gz", "Z": ".Z", "none": ""}.get(policy.compression, ".gz")
    return name + ext


def package(obs_path: Path, policy, out_dir: Path) -> Path:
    """Hatanaka-compress (optional) + compress an obs into the published file.

    Returns the published path in ``out_dir``, leaving the cached plain obs
    untouched. Idempotent: if the published file already exists it is returned
    as-is (so a cache-hit convert doesn't re-package). ``Z`` uses the system
    ``compress`` (available on rek_new). Works in an internal temp dir so it never
    clobbers the source obs even when ``out_dir`` is the obs's own directory.
    """
    import tempfile

    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / published_name(obs_path.name, policy)
    if final.is_file():
        return final

    with tempfile.TemporaryDirectory(prefix="epos_pkg_") as tmp:
        work = Path(tmp) / obs_path.name
        shutil.copy2(obs_path, work)

        if policy.hatanaka:
            rnx2crx = resolve_tool("RNX2CRX")
            _run([rnx2crx, "-f", str(work)])
            crx = work.parent / _obs_to_crinex_name(work.name)
            if not crx.is_file():
                raise ConvertError(f"RNX2CRX produced no {crx.name}")
            work = crx

        if policy.compression == "gz":
            _run(["gzip", "-f", str(work)])
            work = Path(str(work) + ".gz")
        elif policy.compression == "Z":
            compress = resolve_tool("compress")
            _run([compress, "-f", str(work)])
            work = Path(str(work) + ".Z")

        if not work.is_file():
            raise ConvertError(f"packaging produced no {work.name}")
        shutil.move(str(work), str(final))
    return final


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
