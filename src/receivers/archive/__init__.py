"""Archive sync & dissemination — feed the IMO long-term archive and external consumers.

rek_new (rek-d01) collects to local ``/mnt/data/gpsdata`` and pushes to the
long-term archive through the one write gateway, ``gpsops@rawdata:~/gpsdata``
(a disk on ananas — the SOLE writer, by design, for traceability). This package
is the host-level batch delta sweep that replaces the legacy ``:45`` rsync cron,
plus the forward-free ``archive_catalog`` indexing that gives the archive an
integrity ledger keyed on a compression-invariant ``content_sha256``.

Design: ``1781867391-data-dissemination-archive-sync-design`` (vault todo #36).

NOT to be confused with ``receivers.scheduling.tasks.SyncTask`` — that is a
dormant per-station pipeline task; this is the authoritative host-level feed.
"""

from .config import SyncTarget, load_sync_config
from .engine import ArchiveSync, SyncRunResult
from .verify import VerifyStats, verify_archive_catalog

__all__ = [
    "SyncTarget",
    "load_sync_config",
    "ArchiveSync",
    "SyncRunResult",
    "VerifyStats",
    "verify_archive_catalog",
]
