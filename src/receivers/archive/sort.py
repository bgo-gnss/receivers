"""Plan corrective moves for misfiled/misnamed raw archive files.

For each candidate the TRUE observation date is decoded from the file content
(:func:`~receivers.archive.raw_format.decoded_span` — the receiver's embedded
GPS week), compared against what the filename and directory claim, and a
corrected ``(src_rel, dst_rel)`` move is proposed when they disagree.
Station identity is NOT decided here — TOS is canonical for marker/antenna and
the raw-derived coordinates confirm the station in QC; this module only fixes
the one thing those checks cannot see: a wrong DATE in the name/path
(e.g. the RHOF ``2000/``+``2001/`` batches that hold 2010/2011 data).

Planning is read-only (works off the read-only mount). Execution goes through
:func:`~receivers.archive.relocate.relocate_archive_files` (rawdata gateway,
dry-run default, never overwrites).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .raw_format import (
    MONTH_DIRS,
    TRIMBLE,
    UNKNOWN,
    build_raw_name,
    classify_raw,
    decoded_span,
    parse_raw_name,
)

logger = logging.getLogger("receivers.archive.sort")

# Files smaller than this are stubs (0-byte / truncated header fragments seen
# in the .atc sweeps) — flagged, never relocated.
MIN_RAW_BYTES = 4096


@dataclass(frozen=True)
class MovePlan:
    src_rel: str
    dst_rel: str
    fmt: str
    decoded_start: datetime
    claimed: datetime


@dataclass(frozen=True)
class SkipInfo:
    rel: str
    reason: str
    detail: str = ""


def _expected_rel(rel: str, decoded_start: datetime, new_name: str) -> Optional[str]:
    """Correct archive path for a raw file: fix year/month dirs + filename,
    keep station/session/category segments as they are."""
    parts = rel.split("/")
    if len(parts) != 6:
        return None
    _y, _mon, sta, session, category, _name = parts
    return "/".join(
        [
            f"{decoded_start:%Y}",
            MONTH_DIRS[decoded_start.month],
            sta,
            session,
            category,
            new_name,
        ]
    )


def plan_relocations(
    root: Path, rel_files: list[str], *, min_bytes: int = MIN_RAW_BYTES
) -> tuple[list[MovePlan], list[SkipInfo]]:
    """Classify + decode each file under ``root`` and propose corrective moves.

    Returns ``(plans, skips)``: plans only for files whose decoded date
    disagrees with the filename/path claim; everything else (verified-correct,
    stubs, undecodable formats, unreadable files) lands in skips with a reason.
    """
    root = Path(root)
    plans: list[MovePlan] = []
    skips: list[SkipInfo] = []
    for rel in rel_files:
        path = root / rel
        name = path.name
        parsed = parse_raw_name(name)
        if parsed is None:
            skips.append(SkipInfo(rel, "unparseable-name"))
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            skips.append(SkipInfo(rel, "unreadable", str(exc)))
            continue
        if size < min_bytes:
            skips.append(SkipInfo(rel, "stub", f"{size} bytes < {min_bytes}"))
            continue
        fmt = classify_raw(path)
        if fmt == UNKNOWN:
            skips.append(SkipInfo(rel, "unknown-format"))
            continue
        span = decoded_span(path, fmt)
        if span is None:
            reason = "no-date-decoder" if fmt == TRIMBLE else "decode-failed"
            skips.append(SkipInfo(rel, reason, fmt))
            continue
        start, _end = span
        if start.date() == parsed.claimed.date():
            skips.append(SkipInfo(rel, "verified-correct", fmt))
            continue
        new_name = build_raw_name(parsed, start)
        dst_rel = _expected_rel(rel, start, new_name)
        if dst_rel is None:
            skips.append(SkipInfo(rel, "unexpected-layout"))
            continue
        plans.append(
            MovePlan(
                src_rel=rel,
                dst_rel=dst_rel,
                fmt=fmt,
                decoded_start=start,
                claimed=parsed.claimed,
            )
        )
        logger.info(
            "misfiled: %s claims %s but decodes to %s -> %s",
            rel,
            parsed.claimed.date(),
            start.date(),
            dst_rel,
        )
    return plans, skips
