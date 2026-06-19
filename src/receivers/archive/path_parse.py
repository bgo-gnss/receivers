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
_STATION_IDX = 2
_SESSION_IDX = 3
_CATEGORY_IDX = 4
_MIN_PARTS = 6


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

    station = parts[_STATION_IDX]
    session = parts[_SESSION_IDX]
    category = parts[_CATEGORY_IDX]
    filename = parts[-1]

    parsed = parse_date_from_filename(filename, station)
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
