"""archive_catalog upsert — the forward-free index write.

Called per file the sync actually transferred (never for a skipped/failed file),
so a catalog row always means "we believe this reached the archive". The
``content_sha256`` is computed on the local source file at push time;
``last_verified_at`` stays NULL until a separate read-back verify runs.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from ..utils.canonical_key import canonical_key, strip_compression

logger = logging.getLogger(__name__)


def compression_suffix(filename: str) -> str:
    """The on-disk compression suffix ('.gz' / '.Z' / '' ) of ``filename``."""
    _, suffix = strip_compression(filename)
    return suffix


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
    content_sha256: str,
    file_tracking_id: Optional[int] = None,
) -> None:
    """Insert/refresh the catalog row for one archived file.

    Keyed on the migration-050 logical identity
    ``(storage_location, session_type, file_category, canonical_key)`` so a
    re-run (or a later back-zip that changes path/compression) updates the same
    row rather than duplicating it.
    """
    key = canonical_key(filename)
    compression = compression_suffix(filename)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO archive_catalog
                (storage_location, station, file_date, session_type,
                 file_category, canonical_key, file_path, compression,
                 file_size, content_sha256, file_tracking_id, indexed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (storage_location, session_type, file_category, canonical_key)
            DO UPDATE SET
                file_path        = EXCLUDED.file_path,
                compression      = EXCLUDED.compression,
                file_size        = EXCLUDED.file_size,
                content_sha256   = EXCLUDED.content_sha256,
                station          = EXCLUDED.station,
                file_date        = EXCLUDED.file_date,
                file_tracking_id = COALESCE(EXCLUDED.file_tracking_id,
                                            archive_catalog.file_tracking_id),
                indexed_at       = now()
            """,
            (
                storage_location,
                station,
                file_date,
                session_type,
                file_category,
                key,
                archive_path,
                compression,
                file_size,
                content_sha256,
                file_tracking_id,
            ),
        )
