"""EPOS md5 pair for a disseminated RINEX file.

Each pushed file carries TWO md5s, exactly as EPOS expects (and distinct from the
archive's ``content_sha256``):

* ``md5checksum``     — md5 of the file **as published** (compressed bytes), and
* ``md5uncompressed`` — md5 of the fully-decompressed RINEX observation content
  (gunzip/.Z + un-Hatanaka), so a consumer can verify the data independent of the
  on-disk packaging.

They are stored on the ``archive_catalog(storage_location='epos_portal')`` row in
gps_health (see ``job._catalog_epos_push``), the only place they live since the
EPOS gnss-europe database was retired (2026-11).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .convert import _is_hatanaka, _strip_compression, resolve_tool


def _md5_bytes(data: bytes) -> str:
    # usedforsecurity=False: MD5 is not a security control here — it is the
    # interchange format EPOS requires for md5checksum / md5uncompressed (see
    # this module's docstring). Saying so explicitly satisfies bandit B324 and,
    # more usefully, keeps this working on a FIPS-mode Python, where an
    # unqualified hashlib.md5() raises instead of hashing.
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def rinex_md5s(path: Path) -> tuple[str, str]:
    """Return ``(md5checksum, md5uncompressed)`` for a RINEX file.

    ``md5checksum`` is over the file bytes as-is; ``md5uncompressed`` is over the
    decompressed, un-Hatanaka RINEX observation content (so a plain ``.rnx`` gives
    equal values, while a ``.crx.gz`` gives the compressed vs the obs md5).
    """
    path = Path(path)
    raw = path.read_bytes()
    md5checksum = _md5_bytes(raw)

    _, was_compressed = _strip_compression(path.name)
    if was_compressed:
        decompressed = subprocess.run(
            ["gzip", "-dc", str(path)], capture_output=True, check=True
        ).stdout
    else:
        decompressed = raw

    # If the decompressed content is Hatanaka (CRINEX), un-Hatanaka it for the
    # "uncompressed" md5 (CRX2RNX reads stdin with '-').
    inner_name, _ = _strip_compression(path.name)
    if _is_hatanaka(inner_name):
        crx2rnx = resolve_tool("CRX2RNX")
        decompressed = subprocess.run(
            [crx2rnx, "-"], input=decompressed, capture_output=True, check=True
        ).stdout

    return md5checksum, _md5_bytes(decompressed)
