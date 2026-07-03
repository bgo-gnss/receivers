"""In-place RINEX header correction driven by TOS (``receivers rinex --fix-headers``).

Walks the **archived RINEX** files (not raw) for a station/session/date-range,
finds the ones whose headers disagree with TOS, and rewrites **only the
discrepant header fields** in place — no SBF re-conversion. This is the
field-selective counterpart to ``--validate-only``: validate finds the
mismatches, ``--fix-headers`` fixes exactly those.

TOS efficiency: uses :class:`TOSSesionCache` — 1 API call per station, not
per-file. The device history is cached and reused for every observation date.

Reuses the legacy TOS/RINEX stack end-to-end — no header-editing logic is
duplicated here:

* :func:`tostools.rinex.reader.read_rinex_header` — reads a compressed
  (``.Z``/``.gz``) or plain RINEX header.
* :func:`tostools.rinex.reader.extract_header_info` — header → ``label → value``.
* :func:`tostools.gps_metadata_qc.gps_metadata` — TOS station metadata.
* :func:`receivers.dissemination.qc_gate.select_session` — merge the device
  sessions covering the observation date into one ``tos_session``.
* :func:`tostools.rinex.validator.compare_rinex_to_tos` — diff header vs TOS;
  its ``corrections`` dict is **only the discrepant fields** (label → value).
* :func:`tostools.rinex.correct_rinex_from_tos` — the in-place corrector
  (handles compression), now field-selective via ``only_fields``.

The only new code here is: archive-file discovery, the ``--archive-old``
parallel-directory move, and the orchestration that threads the legacy pieces.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("receivers.rinex.header_fix")


def archive_old_file(
    rinex_file: Path,
    *,
    reason: str = "fix-headers",
    stamp: Optional[str] = None,
) -> Optional[Path]:
    """Move ``rinex_file`` to a parallel archive directory, filename unchanged.

    The destination is a sibling directory of the file's parent, named
    ``<parent>_archive/<reason>_<stamp>/``. For example::

        /data/2026/jun/RHOF/15s_24hr/rinex/RHOF1800.26D.Z
        → /data/2026/jun/RHOF/15s_24hr/rinex_archive/fix-headers_20260702/RHOF1800.26D.Z

    Returns the archived path, or None if the move failed (logged).
    """
    rinex_file = Path(rinex_file)
    if not rinex_file.is_file():
        return None

    stamp = stamp or datetime.now().strftime("%Y%m%d")
    parent = rinex_file.parent
    # parent is e.g. .../rinex → archive sibling is .../rinex_archive
    archive_root = parent.parent / f"{parent.name}_archive"
    archive_dir = archive_root / f"{reason}_{stamp}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    dest = archive_dir / rinex_file.name
    # If a same-named file already exists in the archive dir (re-run), don't
    # clobber — append a counter suffix so the operator can tell runs apart.
    if dest.exists():
        i = 1
        while True:
            cand = dest.with_name(f"{rinex_file.stem}_{i}{rinex_file.suffix}")
            if not cand.exists():
                dest = cand
                break
            i += 1
    try:
        shutil.move(str(rinex_file), str(dest))
        logger.info("archived %s → %s", rinex_file.name, dest)
        return dest
    except OSError as exc:
        logger.warning("archive-old failed for %s: %s", rinex_file, exc)
        return None


def _read_header_info(rinex_file: Path, loglevel: int) -> dict[str, str]:
    """Read a (possibly compressed) RINEX header → ``label → value`` dict.

    Reuses :func:`tostools.rinex.reader.read_rinex_header` (handles ``.Z``/``.gz``)
    + :func:`tostools.rinex.reader.extract_header_info`. Returns ``{}`` on any
    read failure — the caller treats that as "skip, can't validate".
    """
    try:
        from tostools.rinex.reader import extract_header_info, read_rinex_header

        rheader = read_rinex_header(rinex_file, loglevel=loglevel)
        if not rheader or "header" not in rheader:
            logger.warning("no header read from %s", rinex_file.name)
            return {}
        info = extract_header_info(rheader, loglevel=loglevel)
        return info or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("header read failed for %s: %s", rinex_file.name, exc)
        return {}


        return None


def fix_headers_in_file(
    rinex_file: Path,
    station_id: str,
    *,
    observation_date: Optional[datetime] = None,
    archive_old: bool = False,
    dry_run: bool = False,
    work_dir: Optional[Path] = None,
    source_base: Optional[Path] = None,
    tos_cache: Any = None,
    loglevel: int = logging.INFO,
) -> dict:
    """Fix the discrepant TOS header fields of one RINEX file.

    When ``work_dir`` is given, the file is COPIED to a mirror path under
    ``work_dir`` (preserving the archive-relative path from ``source_base``)
    and fixed there — the original source file is never touched. The copy
    only happens on a real write (not dry_run).

    ``tos_cache`` is a :class:`TOSSesionCache` (or compatible) providing
    ``.get_session(station, datetime)`` — avoids per-file TOS API calls.

    Returns a summary dict: ``{file, fixed, changed_labels, archived, error}``.
    """
    from tostools.rinex import correct_rinex_from_tos
    from tostools.rinex.reader import _parse_daily_rinex_date
    from tostools.rinex.validator import compare_rinex_to_tos

    source_path = Path(rinex_file)  # the original (may be read-only)
    result: dict = {
        "file": str(source_path),
        "fixed": False,
        "changed_labels": [],
        "archived": None,
        "error": None,
    }
    if not source_path.is_file():
        result["error"] = "file not found"
        return result

    if observation_date is None:
        observation_date = _parse_daily_rinex_date(source_path.name, station_id)
    if observation_date is None:
        result["error"] = "could not parse observation date from filename"
        return result

    # 1. Read the file's header (handles .Z/.gz) and the TOS session for the date.
    rinex_info = _read_header_info(source_path, loglevel)
    if not rinex_info:
        result["error"] = "could not read RINEX header"
        return result
    if tos_cache is None:
        from ..dissemination.tos_access import TOSSesionCache

        tos_cache = TOSSesionCache()
    tos_session = tos_cache.get_session(station_id, observation_date)
    if tos_session is None:
        result["error"] = "no TOS session covers this date"
        return result

    # 2. compare_rinex_to_tos → corrections = ONLY the discrepant fields.
    comparison = compare_rinex_to_tos(
        rinex_info, tos_session, loglevel=loglevel
    )
    discrepant_labels = set(comparison.get("corrections", {}).keys())
    if not discrepant_labels:
        logger.info("%s: header agrees with TOS — no fix needed", source_path.name)
        return result

    # Filter out formatting-noise fields (the validator flags receiver/antenna
    # string fields unconditionally — same values, different format). Only real
    # value discrepancies (marker, antenna_height, coordinates) justify a fix.
    # Mirrors the QC gate's DEFAULT_BLOCKING_FIELDS logic.
    discrepancy_keys = set(comparison.get("discrepancies", {}).keys())
    real_keys = discrepancy_keys & {"marker", "antenna_height", "coordinates"}
    if not real_keys:
        logger.debug(
            "%s: formatting-only discrepancies (%s) — skipping",
            source_path.name,
            ", ".join(sorted(discrepancy_keys)),
        )
        return result
    # Keep only labels that correspond to real discrepancies.
    # Map: antenna_height→ANTENNA: DELTA H/E/N, marker→MARKER NAME, coordinates→APPROX POSITION XYZ
    _key_label = {
        "antenna_height": "ANTENNA: DELTA H/E/N",
        "marker": "MARKER NAME",
        "coordinates": "APPROX POSITION XYZ",
    }
    real_labels = {label for key, label in _key_label.items() if key in real_keys}
    # Intersect with the corrections labels (only fix if the validator also
    # produced a correction for that field — it usually does).
    discrepant_labels = real_labels & discrepant_labels
    if not discrepant_labels:
        return result
    result["changed_labels"] = sorted(discrepant_labels)

    if dry_run:
        logger.info(
            "[DRY RUN] %s: would fix %d field(s): %s",
            source_path.name,
            len(discrepant_labels),
            ", ".join(result["changed_labels"]),
        )
        return result

    # 3. If staging to a work_dir, copy the file from the (possibly read-only)
    #    source archive into the writable work_dir before any other mutations.
    fix_target = source_path
    if work_dir is not None:
        try:
            rel = source_path.relative_to(source_base) if source_base else Path(source_path.parent.name) / source_path.name
        except ValueError:
            rel = Path(source_path.parent.name) / source_path.name
        fix_target = Path(work_dir) / rel
        fix_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, fix_target)
        result["file"] = str(fix_target)

    # 4. Archive the original (writable copy) to a parallel dir if requested.
    if archive_old:
        archived = archive_old_file(fix_target, reason="fix-headers")
        if archived is not None:
            shutil.copy2(archived, fix_target)
            result["archived"] = str(archived)

    # 5. Rewrite only the discrepant fields in place (corrector handles
    #    compression + Hatanaka internally).
    try:
        out = correct_rinex_from_tos(
            fix_target,
            station_id,
            observation_date=observation_date,
            output_file=fix_target,  # overwrite in place
            loglevel=loglevel,
            only_fields=discrepant_labels,
        )
        result["fixed"] = out is not None
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"in-place corrector failed: {exc}"
        return result

    logger.info(
        "%s: fixed %d field(s): %s",
        fix_target.name,
        len(discrepant_labels),
        ", ".join(result["changed_labels"]),
    )
    return result


def discover_all_rinex_files(
    station_id: str,
    session: str,
    data_prepath: str,
) -> list[Path]:
    """Discover ALL archived RINEX files for a station/session — every year/month.

    Walks ``<data_prepath>/<YYYY>/<mon>/<STA>/<session>/rinex/`` for every
    year+month that exists on disk. Used by ``--fix-headers --all``.
    """
    from tostools.rinex.reader import _parse_daily_rinex_date

    root = Path(data_prepath)
    if not root.is_dir():
        return []

    files: list[Path] = []
    for year_dir in sorted(root.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            rinex_dir = month_dir / station_id / session / "rinex"
            if not rinex_dir.is_dir():
                continue
            for p in sorted(rinex_dir.iterdir()):
                if p.is_file() and (p.name.endswith((".Z", ".gz")) or p.suffix in (".o", ".O")):
                    files.append(p)
    return files


def discover_rinex_files(
    station_id: str,
    session: str,
    start_time: datetime,
    end_time: datetime,
    data_prepath: str,
) -> list[Path]:
    """Discover archived RINEX files for a station/session/date-range.

    Walks ``<data_prepath>/<YYYY>/<mon>/<STA>/<session>/rinex/`` and returns
    the RINEX files (``.YYD.Z`` / ``.crx.gz`` / ``.YYo`` etc.) whose observation
    date falls in ``[start_time, end_time)``. Daily session → one file per day
    (matched by DOY in the filename); hourly → one per hour.
    """
    from tostools.rinex.reader import _parse_daily_rinex_date

    files: list[Path] = []
    cur = start_time
    step = timedelta(hours=1) if "1hr" in session.lower() else timedelta(days=1)
    while cur < end_time:
        year = cur.strftime("%Y")
        month = cur.strftime("%b").lower()
        rinex_dir = Path(data_prepath) / year / month / station_id / session / "rinex"
        if rinex_dir.is_dir():
            for p in sorted(rinex_dir.iterdir()):
                if not p.is_file():
                    continue
                # Filter by the file's own observation date so a dir listing
                # the whole month doesn't bleed into a one-day run.
                obs = _parse_daily_rinex_date(p.name, station_id)
                if obs is None:
                    # Non-daily-named file (e.g. long-name .crx.gz) — include
                    # only if we can't tell; the caller will skip on parse fail.
                    if p.name.endswith((".Z", ".gz")) or p.suffix in (".o", ".O"):
                        files.append(p)
                    continue
                if start_time <= obs < end_time:
                    files.append(p)
        cur += step
    # Dedup (a file matching two iteration steps shouldn't appear twice)
    seen: set[Path] = set()
    deduped: list[Path] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    return deduped


def fix_headers_station(
    station_id: str,
    session: str,
    start_time: datetime,
    end_time: datetime,
    *,
    archive_old: bool = False,
    dry_run: bool = False,
    all_files: bool = False,
    work_dir: Optional[Path] = None,
    source_dir: Optional[Path] = None,
    tos_cache: Any = None,
    loglevel: int = logging.INFO,
) -> dict:
    """Run ``--fix-headers`` across a station's archived RINEX.

    When ``work_dir`` is given, files are discovered from ``source_dir``
    (or ``data_prepath``) and COPIED to a mirror path under ``work_dir``
    before fixing — the source archive (read-only NFS) is never touched.

    ``tos_cache`` is a shared :class:`TOSSesionCache` (or compatible).
    When None a fresh one is created — but passing one from the fleet sweep
    (one call to ``fix_headers_station`` per station) means only 1 TOS call
    per station total, regardless of how many files are processed.
    After fixing, print an rsync command to push the staged fixes back.

    Returns ``{station, scanned, fixed, skipped, errors, details: [...]}``.
    """
    from ..config.receivers_config import get_receivers_config

    cfg = get_receivers_config()
    data_prepath = source_dir or Path(cfg.get_data_prepath())
    source_base = Path(data_prepath) if isinstance(data_prepath, str) else data_prepath

    if all_files:
        files = discover_all_rinex_files(station_id, session, str(source_base))
    else:
        files = discover_rinex_files(
            station_id, session, start_time, end_time, str(source_base)
        )
    summary = {
        "station": station_id,
        "scanned": len(files),
        "fixed": 0,
        "skipped": 0,
        "errors": 0,
        "details": [],
    }
    if not files:
        logger.info("[%s] no archived RINEX files in range", station_id)
        return summary

    if work_dir is not None and not dry_run:
        print(
            f"   Staging fixed files into {work_dir} "
            f"(source archive NOT modified — push back with rsync)"
        )

    # One TOS call per station (cached device_history), not per file.
    if tos_cache is None:
        from ..dissemination.tos_access import TOSSesionCache

        tos_cache = TOSSesionCache()

    # Progress bar — shows current file + live counts.
    try:
        from tqdm import tqdm

        pbar = tqdm(
            files,
            desc=f"  {station_id}",
            unit="file",
            ncols=120,
            postfix={"disc": 0, "clean": 0, "err": 0},
        )
    except ImportError:
        pbar = files

    for f in pbar:
        r = fix_headers_in_file(
            f,
            station_id,
            archive_old=archive_old,
            dry_run=dry_run,
            work_dir=work_dir,
            source_base=source_base,
            tos_cache=tos_cache,
            loglevel=loglevel,
        )
        summary["details"].append(r)
        if r.get("error"):
            summary["errors"] += 1
        elif r.get("changed_labels"):
            if dry_run:
                summary.setdefault("would_fix", 0)
                summary["would_fix"] += 1
            else:
                summary["fixed"] += 1
        else:
            summary["skipped"] += 1
        # Update progress bar postfix every file (cheap — just dict assignment).
        if hasattr(pbar, "set_postfix"):
            pbar.set_postfix(
                disc=summary.get("would_fix", summary.get("fixed", 0)),
                clean=summary.get("clean", summary.get("skipped", 0)),
                err=summary["errors"],
                refresh=False,
            )
    # On dry_run, "skipped" in the CLI output means "no discrepancy", so
    # rename for clarity.
    if dry_run:
        summary.setdefault("would_fix", 0)
        summary["clean"] = summary.pop("skipped", 0)
    return summary
