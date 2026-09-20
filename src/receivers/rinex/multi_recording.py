"""A session-date with MORE THAN ONE raw recording.

THE DEFECT THIS EXISTS FOR
--------------------------
``cli/main.py`` skips a date whose output is already staged, so an interrupted
re-rinex can be finished by re-running the same command::

    staged = _staged_rinex_for_date(output_dir, _obs, station_id, args.session)
    if staged is not None: skipped += 1; continue

That check is keyed on the **date**, and it cannot tell "a previous run already
did this" from "an earlier raw file in THIS run produced this". So when one day
carries two raw recordings, the second is converted *nowhere* and counted as an
idempotent resume — silent data loss reported as success.

Measured on RJUC 2015-09-15: the day has two raws covering two disjoint windows
(20:49:15-21:56:45 and 21:57:15-23:59:45 — the receiver's first hours were
recorded in two parts, the first under a wrongly named ``TEST201509150000a.T02``).
The converter emitted ONE product covering only 21:57:15-23:59:45 and reported
the other ``already staged``. The archive's single day file therefore omits
1h 7m of the station's first day, and its raw has been invisible to every
station-prefixed scan since.

WHAT IS (AND IS NOT) A SECOND RECORDING
---------------------------------------
Most apparent "duplicates" are the SAME recording stored twice — an
uncompressed ``X.T00`` beside a ``X.T00.gz``. JOKU alone has ~365 such days.
Those are correctly skipped, so ``collapse_compression_twins`` drops the
redundant copy up front instead of converting-and-skipping it.

A genuine second recording differs in its *filename stem*, e.g.
``RJUC201509150000a.T02`` vs ``RJUC201509152049a.T02``.

WHY THE MERGE IS GATED ON DISJOINTNESS
--------------------------------------
Joining two recordings of one day is only unambiguous when their observation
windows do not overlap. If they DO overlap, the likely cause is a truncated or
partial re-fetch of the same data, and a naive concatenation would double
epochs — or abort the compressor. So ``spans_overlap`` refuses: an overlapping
pair is reported for an operator to judge, never merged automatically.

The archive's own convention is honoured on the way out: Hatanaka via
``rnx2crx`` with the UPPERCASE ``.YYD`` suffix, then real ``compress(1)`` LZW —
matching ``converter_base._apply_hatanaka_compression`` / ``._compress_file``,
which fail loudly rather than writing gzip bytes under a ``.Z`` name.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Sequence

logger = logging.getLogger(__name__)

#: A RINEX header is short; stop well before an observation body.
_MAX_HEADER_LINES = 400

_GZIP_MAGIC = b"\x1f\x8b"
_LZW_MAGIC = b"\x1f\x9d"


def recording_key(path: Path) -> str:
    """Filename identity of a RECORDING, ignoring how it is compressed.

    ``X.T00`` and ``X.T00.gz`` are one recording; ``X0000a.T02`` and
    ``X2049a.T02`` are two.
    """
    name = Path(path).name
    return name[:-3] if name.endswith(".gz") else name


def collapse_compression_twins(paths: Iterable[Path]) -> List[Path]:
    """Drop an `X.gz` that merely duplicates `X` (same recording, twice on disk).

    Prefers the already-compressed copy when both exist: it is what the archive
    keeps and what the converter's pre-decompress step expects. Order is
    preserved so the caller's date ordering is untouched.
    """
    by_key: dict[str, Path] = {}
    order: List[str] = []
    for p in paths:
        p = Path(p)
        key = recording_key(p)
        if key not in by_key:
            by_key[key] = p
            order.append(key)
            continue
        # Same recording twice: keep the .gz copy if this one is it.
        if p.name.endswith(".gz") and not by_key[key].name.endswith(".gz"):
            by_key[key] = p
    kept = [by_key[k] for k in order]
    dropped = len(list(paths)) if not isinstance(paths, list) else len(paths)
    if len(kept) != dropped:
        logger.debug(
            "collapsed %d duplicate-compression raw file(s) to %d recording(s)",
            dropped - len(kept),
            len(kept),
        )
    return kept


def group_recordings(paths: Sequence[Path]) -> dict[str, List[Path]]:
    """Distinct recordings keyed by :func:`recording_key` (order preserved)."""
    out: dict[str, List[Path]] = {}
    for p in paths:
        out.setdefault(recording_key(Path(p)), []).append(Path(p))
    return out


def _open_maybe_compressed(path: Path):
    """Yield header lines from a plain, gzip or LZW-compressed RINEX file.

    The archive convention is LZW `.Z` (`compress(1)`), but the post-2026-07
    cutover also left ~286 gzip-magic `.Z` files behind — both are read here so
    the span check works on either, and neither is rewritten.

    Raises a contextual `RuntimeError` rather than letting a bare `OSError`
    escape; callers that treat the span as advisory (:func:`read_time_span`)
    catch it, and callers that need to fail loudly get a named file.
    """
    try:
        with open(path, "rb") as fh:
            magic = fh.read(2)
    except OSError as exc:
        raise RuntimeError(f"cannot read {Path(path).name}: {exc}") from exc

    if magic == _GZIP_MAGIC:
        import gzip

        try:
            return gzip.open(path, "rt", encoding="latin-1", errors="replace")
        except OSError as exc:
            raise RuntimeError(
                f"cannot open gzip {Path(path).name}: {exc}"
            ) from exc
    if magic == _LZW_MAGIC:
        proc = subprocess.run(
            ["uncompress", "-c", str(path)],
            capture_output=True,
            timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"uncompress failed for {Path(path).name}: "
                f"{proc.stderr.decode(errors='replace').strip()[:160]}"
            )
        return proc.stdout.decode("latin-1", errors="replace").splitlines()
    try:
        return open(path, encoding="latin-1", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"cannot open {Path(path).name}: {exc}") from exc


def _parse_obs_epoch(value: str) -> datetime | None:
    """``2015 9 15 20 49 15.0000000 GPS`` -> datetime; None if unparseable."""
    from datetime import timedelta

    parts = value.split()
    try:
        year, month, day = (int(p) for p in parts[:3])
        base = datetime(year, month, day)
    except (ValueError, IndexError):
        return None
    try:
        hour, minute = (int(p) for p in parts[3:5])
        seconds = float(parts[5])
    except (ValueError, IndexError):
        return base
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= seconds < 61):
        return base
    return base + timedelta(hours=hour, minutes=minute, seconds=seconds)


def read_time_span(path: Path) -> tuple[datetime, datetime] | None:
    """``(TIME OF FIRST OBS, TIME OF LAST OBS)`` from a RINEX header, or None.

    Returns None when either record is absent — including every RINEX 2 header,
    which carries no `TIME OF LAST OBS`. The caller must then NOT merge.
    """
    first = last = None
    try:
        handle = _open_maybe_compressed(Path(path))
    except Exception as exc:  # noqa: BLE001 - a span is advisory, never fatal
        logger.warning("time-span read failed for %s: %s", Path(path).name, exc)
        return None
    try:
        lines = handle if isinstance(handle, list) else handle
        for i, line in enumerate(lines):
            if isinstance(line, bytes):
                line = line.decode("latin-1", errors="replace")
            label = line[60:].strip()
            if label == "TIME OF FIRST OBS":
                first = _parse_obs_epoch(line[:60])
            elif label == "TIME OF LAST OBS":
                last = _parse_obs_epoch(line[:60])
            elif label == "END OF HEADER" or i > _MAX_HEADER_LINES:
                break
    finally:
        if not isinstance(handle, list):
            handle.close()
    if first is None or last is None:
        return None
    return first, last


def spans_overlap(
    a: tuple[datetime, datetime] | None, b: tuple[datetime, datetime] | None
) -> bool | None:
    """True/False for two spans; None when either is unknown (=> do not merge)."""
    if a is None or b is None:
        return None
    return a[0] <= b[1] and b[0] <= a[1]


def _tool(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise FileNotFoundError(
            f"{name} not on PATH — required to merge multiple raw recordings"
        )
    return p


def _uncompress_to(src: Path, dest: Path) -> None:
    """LZW/gzip/plain -> an uncompressed CRINEX (or RINEX) file at ``dest``.

    Every failure is re-raised as a contextual `RuntimeError`: a merge that
    cannot unpack one of its parts must abort loudly, never yield a silently
    truncated day file.
    """
    try:
        with open(src, "rb") as fh:
            magic = fh.read(2)
    except OSError as exc:
        raise RuntimeError(f"cannot read {Path(src).name}: {exc}") from exc

    if magic == _LZW_MAGIC:
        try:
            with open(dest, "wb") as out:
                subprocess.run(
                    ["uncompress", "-c", str(src)],
                    stdout=out,
                    check=True,
                    timeout=600,
                )
        except OSError as exc:
            raise RuntimeError(
                f"cannot write {Path(dest).name} while unpacking {Path(src).name}: {exc}"
            ) from exc
        return
    if magic == _GZIP_MAGIC:
        import gzip

        try:
            with gzip.open(src, "rb") as fin, open(dest, "wb") as out:
                shutil.copyfileobj(fin, out)
        except OSError as exc:
            raise RuntimeError(
                f"cannot unpack gzip {Path(src).name}: {exc}"
            ) from exc
        return
    try:
        shutil.copyfile(src, dest)
    except OSError as exc:
        raise RuntimeError(
            f"cannot copy {Path(src).name} to {Path(dest).name}: {exc}"
        ) from exc


def merge_disjoint(parts: Sequence[Path], dest: Path, *, version: int = 3) -> None:
    """Merge >=2 RINEX products into one at ``dest`` (archive convention).

    Callers MUST have established the parts are disjoint (:func:`spans_overlap`);
    this function does not re-check, and would duplicate epochs if handed
    overlapping inputs.

    ``dest`` is written as UPPERCASE ``.YYD.Z`` — Hatanaka + real LZW
    ``compress(1)`` — matching every other product in the archive.
    """
    parts = [Path(p) for p in parts]
    if len(parts) < 2:
        raise ValueError("merge_disjoint needs at least two parts")

    gfzrnx, crx2rnx, rnx2crx = _tool("gfzrnx"), _tool("CRX2RNX"), _tool("RNX2CRX")

    with tempfile.TemporaryDirectory(prefix="merge_recordings_") as tmp:
        tmpdir = Path(tmp)
        rinex_inputs: List[Path] = []
        for idx, part in enumerate(parts):
            # Step 1: unpack the container (LZW .Z / gzip / plain).
            raw = tmpdir / f"part{idx}.crx"
            _uncompress_to(part, raw)
            # Step 2: Hatanaka CRINEX -> RINEX. CRX2RNX insists on a .crx name.
            subprocess.run(
                [crx2rnx, raw.name], cwd=tmpdir, check=True, capture_output=True
            )
            produced = raw.with_suffix(".rnx")
            if not produced.exists():
                raise RuntimeError(
                    f"CRX2RNX produced no output for {part.name} (looked for {produced.name})"
                )
            rinex_inputs.append(produced)

        # Step 3: merge in time order into one canonical RINEX 3 document.
        #
        # The output is named from the DESTINATION, not an arbitrary "merged.rnx":
        # rnx2crx keys its own output name off the input's IGS-style suffix. Given
        # "merged.rnx" it writes "merged.crx" — not a Hatanaka .d — so the next
        # step finds nothing and the merge looks like it silently failed. Deriving
        # the observation name from the product also guarantees the archive's
        # UPPERCASE convention:
        #     RJUC2580.15D.Z  ->  obs RJUC2580.15O  ->  RJUC2580.15D  ->  .15D.Z
        stem = dest.name[:-2] if dest.name.endswith(".Z") else dest.name
        obs_name = stem[:-1] + "O"
        obs = tmpdir / obs_name

        cmd = [gfzrnx]
        for f in rinex_inputs:
            cmd += ["-finp", str(f)]
        cmd += ["-fout", str(obs), "-vo", str(version)]
        proc = subprocess.run(cmd, capture_output=True, timeout=1800)
        if proc.returncode != 0 or not obs.exists():
            raise RuntimeError(
                "gfzrnx merge failed: "
                + proc.stderr.decode(errors="replace").strip()[:300]
            )

        # Step 4: Hatanaka. rnx2crx writes the compact file beside its input,
        # swapping the .YYo suffix; accept either case (this build emits .15D
        # uppercase for an IGS-style name, while converter_base expects lowercase
        # from other paths).
        subprocess.run([rnx2crx, obs_name], cwd=tmpdir, check=True, capture_output=True)
        upper = obs.with_suffix(obs.suffix[:-1] + "D")
        lower = obs.with_suffix(obs.suffix[:-1] + "d")
        hat = upper if upper.exists() else (lower if lower.exists() else None)
        if hat is None:
            raise RuntimeError(
                f"rnx2crx produced no output for {obs_name} — expected "
                f"{upper.name} or {lower.name}"
            )
        if hat != upper:
            hat.rename(upper)

        # Step 5: real compress(1) LZW — never gzip bytes under a .Z name.
        subprocess.run([_tool("compress"), "-f", str(upper)], check=True, capture_output=True)
        produced = upper.parent / (upper.name + ".Z")
        if not produced.exists():
            raise RuntimeError("compress produced no output for the merged file")

        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(produced), str(dest))
        except OSError as exc:
            raise RuntimeError(
                f"cannot publish the merged product to {dest}: {exc}"
            ) from exc

    logger.info(
        "merged %d recordings into %s (%d bytes)",
        len(parts),
        Path(dest).name,
        Path(dest).stat().st_size,
    )
