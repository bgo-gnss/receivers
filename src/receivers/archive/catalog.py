"""archive_catalog upsert — the forward-free index write.

Called per file the sync actually transferred (never for a skipped/failed file),
so a catalog row always means "we believe this reached the archive". The
``content_sha256`` is computed on the local source file at push time;
``last_verified_at`` stays NULL until a separate read-back verify runs.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Optional

from ..utils.canonical_key import canonical_key, strip_compression

logger = logging.getLogger(__name__)

# Lowercase 3-letter month, matching the collection-tree layout
# (YYYY/mon/STA/session/category/FILE) — locale-independent on purpose.
_MON = (
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
)


def compression_suffix(filename: str) -> str:
    """The on-disk compression suffix ('.gz' / '.Z' / '' ) of ``filename``."""
    _, suffix = strip_compression(filename)
    return suffix


def local_catalog_target(ft_session_type: str) -> Optional[tuple[str, str, str]]:
    """Map a ``file_tracking`` session_type to a local catalog identity.

    ``file_tracking`` distinguishes RINEX with a ``_rinex`` suffix on the
    session_type; ``archive_catalog`` splits the same into
    ``(session_type, file_category)`` and a per-tier ``storage_location``.

    Returns ``(storage_location, catalog_session_type, file_category)`` — e.g.
    ``'15s_24hr_rinex' → ('local_rinex', '15s_24hr', 'rinex')`` and
    ``'15s_24hr' → ('local_raw', '15s_24hr', 'raw')`` — or ``None`` for a
    session_type that has no local file tier (defensive; today all do).
    """
    if not ft_session_type:
        return None
    if ft_session_type.endswith("_rinex"):
        return ("local_rinex", ft_session_type[: -len("_rinex")], "rinex")
    return ("local_raw", ft_session_type, "raw")


def local_archive_path(
    data_prepath: str,
    station: str,
    catalog_session_type: str,
    file_category: str,
    file_date: Optional[date],
    filename: str,
) -> str:
    """Reconstruct the on-disk path of a local ring file from its identity.

    The collection tree is
    ``<data_prepath>/YYYY/<mon>/<STA>/<session>/<category>/<filename>``. When
    ``file_date`` is unknown (should not happen for tracked files — the date is
    part of the file_tracking identity) the dated prefix is dropped so the
    returned path is still non-empty (archive_catalog.file_path is NOT NULL).
    """
    root = data_prepath.rstrip("/")
    if file_date is not None:
        return os.path.join(
            root,
            f"{file_date.year:04d}",
            _MON[file_date.month - 1],
            station,
            catalog_session_type,
            file_category,
            filename,
        )
    return os.path.join(root, station, catalog_session_type, file_category, filename)


def catalog_local_file(
    conn,
    *,
    ft_session_type: str,
    station: str,
    file_date: Optional[date],
    file_hour: Optional[int],
    filename: str,
    file_size: Optional[int],
    data_prepath: str,
    content_sha256: Optional[str] = None,
    file_tracking_id: Optional[int] = None,
) -> bool:
    """Upsert a ``local_raw``/``local_rinex`` catalog row for a locally-held file.

    The forward-free local index write: called when a file is archived to the
    local ring (and by the backfill). Hashes are DEFERRED — ``content_sha256``/
    ``compressed_sha256`` stay NULL for the integrity checker to lazy-fill, so
    this never decompresses on the hot path. Best-effort: returns False (and
    logs at debug) on any failure rather than raising, so a catalog hiccup can
    never break the operational archive record. The caller owns the commit.
    """
    target = local_catalog_target(ft_session_type)
    if target is None or not filename or not station:
        return False
    storage_location, catalog_session, file_category = target
    try:
        upsert_catalog_row(
            conn,
            storage_location=storage_location,
            station=station,
            session_type=catalog_session,
            file_category=file_category,
            file_date=file_date,
            file_hour=file_hour,
            archive_path=local_archive_path(
                data_prepath,
                station,
                catalog_session,
                file_category,
                file_date,
                filename,
            ),
            filename=filename,
            file_size=file_size if file_size is not None else 0,
            content_sha256=content_sha256,
            file_tracking_id=file_tracking_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort; never break the caller
        logger.debug(
            "local catalog upsert failed for %s/%s: %s", station, filename, exc
        )
        return False


def backfill_local_catalog(
    conn,
    data_prepath: str,
    *,
    statuses: tuple[str, ...] = ("archived", "downloaded"),
    batch_size: int = 1000,
    verify_exists: bool = True,
    dry_run: bool = False,
    log: logging.Logger = logger,
) -> dict:
    """Seed ``local_raw``/``local_rinex`` catalog rows from ``file_tracking``.

    One-time (idempotent) backfill so every locally-held file already tracked
    gains a durable catalog row — the M1 exit criterion. Carries any
    ``file_tracking.content_sha256`` already computed (free — no re-hash) and the
    soft ``file_tracking_id`` link. With ``verify_exists`` (default) a row whose
    reconstructed on-disk path is absent is SKIPPED, so "present@local" reflects
    real disk state (a file_tracking row can outlive the file). Pages by id so it
    is restart-safe and bounded. Returns counts.
    """
    stats = {"cataloged": 0, "skipped_missing": 0, "skipped_unmapped": 0, "scanned": 0}
    last_id = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, sid, session_type, file_date, file_hour, filename,
                          file_size, content_sha256
                   FROM file_tracking
                   WHERE status = ANY(%s) AND filename IS NOT NULL AND id > %s
                   ORDER BY id
                   LIMIT %s""",
                (list(statuses), last_id, batch_size),
            )
            rows = cur.fetchall()
        if not rows:
            break
        for fid, sid, ft_session, fdate, fhour, filename, fsize, csha in rows:
            last_id = fid
            stats["scanned"] += 1
            target = local_catalog_target(ft_session)
            if target is None:
                stats["skipped_unmapped"] += 1
                continue
            _loc, cat_session, category = target
            if verify_exists:
                path = local_archive_path(
                    data_prepath, sid, cat_session, category, fdate, filename
                )
                if not os.path.isfile(path):
                    stats["skipped_missing"] += 1
                    continue
            if dry_run:
                stats["cataloged"] += 1
                continue
            if catalog_local_file(
                conn,
                ft_session_type=ft_session,
                station=sid,
                file_date=fdate,
                file_hour=fhour,
                filename=filename,
                file_size=fsize,
                data_prepath=data_prepath,
                content_sha256=csha,
                file_tracking_id=fid,
            ):
                stats["cataloged"] += 1
        if not dry_run:
            conn.commit()
        log.info(
            "backfill_local_catalog: scanned=%d cataloged=%d skipped_missing=%d",
            stats["scanned"],
            stats["cataloged"],
            stats["skipped_missing"],
        )
    return stats


def upsert_catalog_row(
    conn,
    *,
    storage_location: str,
    station: str,
    session_type: str,
    file_category: str,
    file_date: Optional[date],
    archive_path: str,
    filename: str,
    file_size: int,
    content_sha256: Optional[str],
    file_tracking_id: Optional[int] = None,
    file_hour: Optional[int] = None,
    compressed_sha256: Optional[str] = None,
    md5checksum: Optional[str] = None,
    md5uncompressed: Optional[str] = None,
) -> None:
    """Insert/refresh the catalog row for one archived file.

    Keyed on the migration-050 logical identity
    ``(storage_location, session_type, file_category, canonical_key)`` so a
    re-run (or a later back-zip that changes path/compression) updates the same
    row rather than duplicating it.

    ``file_hour`` (mig 055) is the hour-of-day for hourly products (NULL for
    daily). ``compressed_sha256`` (mig 055) is the on-disk-bytes hash — usually
    NULL on the forward write (lazy-filled by the integrity checker), so on
    conflict it is COALESCE'd to never wipe a previously filled value.
    ``content_sha256`` may also be NULL (a forward local-ring write that defers
    hashing off the hot path).
    """
    key = canonical_key(filename)
    compression = compression_suffix(filename)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO archive_catalog
                (storage_location, station, file_date, file_hour, session_type,
                 file_category, canonical_key, file_path, compression,
                 file_size, content_sha256, compressed_sha256,
                 md5checksum, md5uncompressed, file_tracking_id, indexed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (storage_location, session_type, file_category, canonical_key)
            DO UPDATE SET
                file_path         = EXCLUDED.file_path,
                compression       = EXCLUDED.compression,
                file_size         = EXCLUDED.file_size,
                content_sha256    = COALESCE(EXCLUDED.content_sha256,
                                             archive_catalog.content_sha256),
                compressed_sha256 = COALESCE(EXCLUDED.compressed_sha256,
                                             archive_catalog.compressed_sha256),
                md5checksum       = COALESCE(EXCLUDED.md5checksum,
                                             archive_catalog.md5checksum),
                md5uncompressed   = COALESCE(EXCLUDED.md5uncompressed,
                                             archive_catalog.md5uncompressed),
                station           = EXCLUDED.station,
                file_date         = EXCLUDED.file_date,
                file_hour         = COALESCE(EXCLUDED.file_hour,
                                             archive_catalog.file_hour),
                file_tracking_id  = COALESCE(EXCLUDED.file_tracking_id,
                                             archive_catalog.file_tracking_id),
                indexed_at        = now()
            """,
            (
                storage_location,
                station,
                file_date,
                file_hour,
                session_type,
                file_category,
                key,
                archive_path,
                compression,
                file_size,
                content_sha256,
                compressed_sha256,
                md5checksum,
                md5uncompressed,
                file_tracking_id,
            ),
        )
