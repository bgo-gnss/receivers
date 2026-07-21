"""Parse a collection-tree path into its catalog dimensions.

The local/archive layout is ``<root>/YYYY/mon/STATION/SESSION/CATEGORY/filename``
(e.g. ``.../2026/apr/AKUR/15s_24hr/raw/AKUR202604070000a.T02.gz``). Station,
session and category come from the **directory components** — unambiguous and
authoritative — not from fragile filename inference. Only the date/hour is read
from the filename, reusing the existing parser. ``session_type`` in particular
is part of the ``archive_catalog`` UNIQUE key, so it must never be guessed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Optional

from ..utils.download_tracker import parse_date_from_filename

# Index of each dimension within the path components relative to source_root:
#   [0]=YYYY  [1]=mon  [2]=STATION  [3]=SESSION  [4]=CATEGORY  [5]=filename
_YEAR_IDX = 0
_STATION_IDX = 2
_SESSION_IDX = 3
_CATEGORY_IDX = 4
_MIN_PARTS = 6


def _is_year_component(part: str) -> bool:
    """True iff ``part`` is a 4-digit calendar year (the archive tree's top level).

    This is the load-bearing guard against mis-parsing paths whose components are
    shifted out of the ``YYYY/mon/STA/...`` layout — most importantly NFS/NetApp
    ``.snapshot/<snap>/YYYY/...`` snapshot trees, where the two extra prefix dirs
    would otherwise make the year read as the station, the month as the session,
    and the real station as the category (all with a NULL file_date). Because the
    year slot then holds ``.snapshot`` (not a year), rejecting here returns
    ``None`` — the "not a catalogable archive file" contract every caller honours.
    """
    return len(part) == 4 and part.isdigit()


@dataclass(frozen=True)
class ParsedArchivePath:
    """Catalog dimensions extracted from a collection-tree path."""

    station: str
    session_type: str
    file_category: str
    relative_path: str
    file_date: Optional[date]
    file_hour: Optional[int]


def parse_archive_path(abs_path: str, source_root: str) -> Optional[ParsedArchivePath]:
    """Extract catalog dimensions from ``abs_path`` under ``source_root``.

    Returns ``None`` when the path is not under ``source_root`` or does not have
    the expected ``YYYY/mon/STATION/SESSION/CATEGORY/filename`` depth — callers
    treat that as "not a catalogable archive file" and skip it.
    """
    rel = os.path.relpath(abs_path, source_root)
    if rel.startswith(".."):
        return None
    parts = rel.split(os.sep)
    if len(parts) < _MIN_PARTS:
        return None
    if not _is_year_component(parts[_YEAR_IDX]):
        # Not a ``YYYY/mon/STA/...`` archive path (e.g. an NFS ``.snapshot``
        # tree, whose leading dirs shift every dimension) — not catalogable.
        return None

    station = parts[_STATION_IDX]
    session = parts[_SESSION_IDX]
    category = parts[_CATEGORY_IDX]
    filename = parts[-1]

    # The archive path carries the unambiguous 4-digit year (already validated
    # by ``_is_year_component`` above) and the session type. Feed both to the
    # filename parser so a short-name RINEX-2 file (``NYLA060a.21D.Z``) is dated
    # by its *observation* year — not the current year — and hour 0 of an hourly
    # session is not mistaken for a daily file. See parse_date_from_filename.
    parsed = parse_date_from_filename(
        filename,
        station,
        default_year=int(parts[_YEAR_IDX]),
        session_type=session,
    )
    file_date: Optional[date] = parsed[0] if parsed else None
    file_hour: Optional[int] = parsed[1] if parsed else None

    return ParsedArchivePath(
        station=station,
        session_type=session,
        file_category=category,
        relative_path=rel,
        file_date=file_date,
        file_hour=file_hour,
    )
