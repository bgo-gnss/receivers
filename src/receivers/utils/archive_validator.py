"""Thin backward-compat re-export of archive validation helpers.

The real implementation now lives in
:mod:`tostools.utils.archive` — the logic is domain-generic (gzip/zip magic
bytes, compression integrity, archive-vs-tmp discovery) and other projects
in the GPS library ecosystem can reuse it from there. This module exists
only so existing receivers imports continue to work without churn.

New code should import directly from tostools::

    from tostools.utils.archive import ArchiveValidator, ArchiveLocation
"""

from tostools.utils.archive import (  # noqa: F401
    ArchiveLocation,
    ArchiveValidator,
    CompressionValidator,
    GzipValidator,
)

__all__ = [
    "ArchiveLocation",
    "ArchiveValidator",
    "CompressionValidator",
    "GzipValidator",
]
