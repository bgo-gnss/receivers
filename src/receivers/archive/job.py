"""APScheduler entry point for the :45 archive-sync job.

Module-level callable (APScheduler persists jobs by import path, like the
reconciler / integrity jobs). Syncs every active target, then evaluates
freshness so a stalled feed logs a WARNING. Double-gated: the scheduler only
schedules this when ``archive_sync.enabled`` is true in scheduler.yaml AND a
target is ``active`` in sync.yaml.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("receivers.archive.sync")


def run_archive_sync_job(
    config_path: Optional[str] = None,
    host: Optional[str] = None,
    max_age_minutes: int = 120,
) -> None:
    """Run all active sync targets, then log freshness. Never raises out."""
    from ..db.connection import get_connection
    from .config import load_sync_config
    from .engine import ArchiveSync
    from .freshness import evaluate_all

    try:
        targets = load_sync_config(Path(config_path) if config_path else None)
    except Exception:
        logger.exception("archive-sync: failed to load sync.yaml")
        return

    active = [t for t in targets if t.active]
    if not active:
        logger.info("archive-sync: no active targets — nothing to do")
        return

    conn = None
    try:
        conn = get_connection(host_override=host)
        for target in active:
            try:
                result = ArchiveSync(target, conn=conn).run()
                logger.info(
                    "archive-sync %s: %s (transferred=%d cataloged=%d)",
                    target.name,
                    result.message,
                    result.transferred,
                    result.cataloged,
                )
                for err in result.errors:
                    logger.warning("archive-sync %s: %s", target.name, err)
            except Exception:
                logger.exception("archive-sync %s: run failed", target.name)
        evaluate_all(conn, active, now=datetime.now(), max_age_minutes=max_age_minutes)
    except Exception:
        logger.exception("archive-sync job failed")
    finally:
        if conn is not None:
            conn.close()


def run_archive_verify_job(
    config_path: Optional[str] = None,
    host: Optional[str] = None,
    read_root: Optional[str] = None,
    storage_location: str = "imo_archive",
    limit: int = 500,
    reverify_after_days: Optional[int] = None,
) -> None:
    """Re-hash archived files vs the catalog + local cross-check. Never raises out.

    Module-level callable (APScheduler persists by import path). With
    ``read_root`` (the archive's read-only mount, rek-d01 /mnt/rawgpsdata) it
    re-hashes each archive file and stamps last_verified_at on match / logs
    ARCHIVE CORRUPT on mismatch; without it, only the DB-only local↔archive
    cross-check runs. ``dest_prefix`` is taken from the matching sync target so
    the stored archive path maps onto the mount.
    """
    from ..db.connection import get_connection
    from .config import load_sync_config
    from .verify import verify_archive_catalog

    dest_prefix = None
    try:
        targets = load_sync_config(Path(config_path) if config_path else None)
        target = next((t for t in targets if t.name == storage_location), None)
        if target is None and targets:
            target = targets[0]
        if target is not None:
            dest_prefix = target.dest
    except Exception:
        # No sync.yaml / parse error — read-back falls back to the gpsdata/ split.
        logger.exception("archive-verify: failed to load sync.yaml")

    conn = None
    try:
        conn = get_connection(host_override=host)
        stats = verify_archive_catalog(
            conn,
            storage_location=storage_location,
            read_root=read_root,
            dest_prefix=dest_prefix,
            limit=limit,
            reverify_after_days=reverify_after_days,
        )
        if stats.mismatched or stats.local_divergent:
            logger.warning(
                "archive-verify: %d CORRUPT, %d local-divergent "
                "(checked=%d verified=%d missing=%d)",
                stats.mismatched,
                stats.local_divergent,
                stats.checked,
                stats.verified,
                stats.missing,
            )
    except Exception:
        logger.exception("archive-verify job failed")
    finally:
        if conn is not None:
            conn.close()
