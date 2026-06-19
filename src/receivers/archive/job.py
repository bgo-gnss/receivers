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
