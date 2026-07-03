"""archive_catalog reindex — refresh content_sha256 for files changed out-of-band.

The forward catalog (``catalog.upsert_catalog_row``) is written by the sync
engine for every file it *transfers*. But a file can be modified on the archive
by a path that does NOT go through the engine — notably
``receivers rinex --fix-headers --push``, which rsyncs corrected RINEX straight
to the archive. After such a write the archive bytes change but the catalog's
``content_sha256`` still reflects the pre-edit content, so the scheduled
integrity verify would flag the row as corrupt (a false positive).

Reindex closes that gap: re-hash the authoritative bytes and upsert the row.
The bytes are taken from a local *staging mirror* (the ``--work-dir`` tree that
``--fix-headers`` pushed from) — byte-identical to what rsync placed on the
archive — so no archive mount or ssh read-back is needed. This makes it usable
from a laptop, where this kind of maintenance work actually happens (the
production server is busy with the daily runs).

``content_sha256`` here matches the verify pass exactly: it is taken over the
DECOMPRESSED content (see :mod:`receivers.utils.content_hash`), so a ``.d.Z``
Hatanaka file hashes identically to its decompressed twin, and a header rewrite
changes the hash (which is the whole point).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from ..utils.canonical_key import canonical_key
from ..utils.content_hash import CorruptArchiveFileError, content_sha256
from .catalog import upsert_catalog_row
from .path_parse import parse_archive_path

logger = logging.getLogger(__name__)


@dataclass
class ReindexStats:
    """Outcome of a reindex run."""

    updated: int = 0          # existing row, content_sha256 changed
    inserted: int = 0         # no prior row for this file
    unchanged: int = 0        # row already held the correct hash
    errors: list[str] = field(default_factory=list)
    skipped: int = 0          # file could not be parsed to an archive identity
    skipped_new: int = 0      # only_existing: no prior row, insert suppressed

    @property
    def touched(self) -> int:
        return self.updated + self.inserted

    def to_dict(self) -> dict:
        return {
            "updated": self.updated,
            "inserted": self.inserted,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "skipped_new": self.skipped_new,
            "errors": self.errors,
        }


def reindex_files(
    conn,
    files: list[str],
    *,
    root: str,
    storage_location: str,
    dest_prefix: str,
    dry_run: bool = False,
    only_existing: bool = False,
    log: logging.Logger = logger,
) -> ReindexStats:
    """Re-hash each local file and upsert its ``archive_catalog`` row.

    Args:
        conn: gps_health DB connection (the catalog host — pass a pgdev
            connection to update production).
        files: local file paths, each under ``root`` in the archive mirror
            layout (``YYYY/mon/STA/session/category/FILE``).
        root: the mirror root the ``files`` are relative to (e.g. the
            ``--fix-headers`` work-dir). Used only to derive the archive-relative
            path; the bytes hashed are the local file's.
        storage_location: ``archive_catalog.storage_location`` to write
            (e.g. ``imo_archive``).
        dest_prefix: the archive dest the files live at (e.g. ``~/gpsdata``);
            combined with the relative path to form ``file_path``.
        dry_run: classify + log but do not write.
        only_existing: only repair rows that already exist (skip inserts) — for
            surgically fixing sha256 the caller knows went stale, without
            expanding catalog coverage to previously-uncataloged files. Skipped
            inserts are counted in ``stats.skipped``.

    Returns:
        :class:`ReindexStats`.
    """
    stats = ReindexStats()
    if conn is None:
        stats.errors.append("no DB connection")
        return stats
    dest_prefix = dest_prefix.rstrip("/")

    for f in files:
        parsed = parse_archive_path(f, root)
        if parsed is None:
            stats.skipped += 1
            log.warning("reindex: cannot parse archive identity from %s", f)
            continue
        try:
            digest = content_sha256(f)
        except CorruptArchiveFileError as exc:
            stats.errors.append(f"corrupt, not reindexed: {f}: {exc}")
            log.error("reindex: corrupt local file %s: %s", f, exc)
            continue
        except OSError as exc:
            stats.errors.append(f"could not read {f}: {exc}")
            continue

        key = canonical_key(os.path.basename(f))
        archive_path = f"{dest_prefix}/{parsed.relative_path}"

        # Classify against the existing row so the report distinguishes a genuine
        # correction (updated) from a no-op (unchanged) or a first index (inserted).
        prior = _existing_sha(
            conn, storage_location, parsed.session_type, parsed.file_category, key
        )
        if prior is None:
            outcome = "inserted"
        elif prior == digest:
            outcome = "unchanged"
        else:
            outcome = "updated"

        if outcome == "unchanged":
            stats.unchanged += 1
            continue
        if outcome == "inserted" and only_existing:
            stats.skipped_new += 1  # no prior row and caller asked to skip inserts
            continue

        if dry_run:
            log.info(
                "reindex[DRY]: %s %s %s → %s (%s)",
                storage_location, parsed.station, key, digest[:12], outcome,
            )
        else:
            upsert_catalog_row(
                conn,
                storage_location=storage_location,
                station=parsed.station,
                session_type=parsed.session_type,
                file_category=parsed.file_category,
                file_date=parsed.file_date,
                archive_path=archive_path,
                filename=os.path.basename(f),
                file_size=os.path.getsize(f),
                content_sha256=digest,
            )
            conn.commit()  # per-file: a crash loses one row, not the run
        if outcome == "updated":
            stats.updated += 1
        else:
            stats.inserted += 1
    return stats


def _existing_sha(
    conn, storage_location: str, session_type: str, file_category: str, key: str
) -> Optional[str]:
    """Return the current content_sha256 for the catalog row, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT content_sha256 FROM archive_catalog
               WHERE storage_location = %s AND session_type = %s
                 AND file_category = %s AND canonical_key = %s""",
            (storage_location, session_type, file_category, key),
        )
        row = cur.fetchone()
    return row[0] if row else None
