"""T4 — index a disseminated RINEX file into the EPOS ``rinex_file`` table.

Records each pushed file with TWO md5s, exactly as EPOS expects (and distinct
from the archive's ``content_sha256``):

* ``md5checksum``     — md5 of the file **as published** (compressed bytes), and
* ``md5uncompressed`` — md5 of the fully-decompressed RINEX observation content
  (gunzip/.Z + un-Hatanaka), so a consumer can verify the data independent of the
  on-disk packaging.

Upserts the supporting ``data_center`` / ``file_type`` / ``data_center_structure``
rows (IMO data centre, hardcoded like the legacy importer) and then the
``rinex_file`` row, keyed on ``(name, relative_path)`` via SELECT-then-write (the
schema has no UNIQUE there, so we don't rely on ON CONFLICT). A re-index updates
the row and stamps ``revision_date`` — the hook the retroactive header-correction
re-push needs.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from .convert import _is_hatanaka, _strip_compression, resolve_tool
from .epos_db import get_or_create, insert_row, update_row

logger = logging.getLogger("receivers.dissemination.index")

# IMO data centre (hardcoded, as in the legacy importer).
_DATA_CENTER = {
    "acronym": "IMO",
    "hostname": "data.epos-iceland.is",
    "root_path": "",
    "name": "IMO",
    "protocol": "https",
}


def _md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


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


def _file_type_for(rinex_version: int, session: str) -> dict[str, str]:
    """The ``file_type`` row values for a session (24h/15s assumptions for now)."""
    window = "24hour" if "24hr" in session or "24h" in session else "N/N"
    freq = "15s" if "15s" in session else "N/N"
    return {
        "format": f"RINEX{rinex_version}",
        "sampling_window": window,
        "sampling_frequency": freq,
    }


def index_rinex_file(
    conn,
    file_path: Path,
    station: str,
    observation_dt: datetime,
    *,
    relative_path: str,
    session: str = "15s_24hr",
    rinex_version: int = 3,
    published_dt: Optional[datetime] = None,
) -> Optional[int]:
    """Upsert one ``rinex_file`` row for a disseminated file. Returns its id.

    Returns ``None`` (and logs) if the station isn't in the EPOS DB yet — the
    metadata ETL (T5) must run first (the row FKs to ``station``).
    """
    file_path = Path(file_path)
    marker = station.upper()

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM station WHERE upper(marker) = %s", (marker,))
        row = cur.fetchone()
        if row is None:
            logger.warning(
                "station %s not in EPOS DB — run the metadata ETL first; "
                "skipping rinex_file index",
                marker,
            )
            return None
        id_station = row[0]

        id_agency = get_or_create(
            cur, "agency", {"abbreviation": "IMO"}, {"name": "IMO"}
        )
        id_data_center = get_or_create(
            cur,
            "data_center",
            {"acronym": "IMO"},
            {
                **{k: v for k, v in _DATA_CENTER.items() if k != "acronym"},
                "id_agency": id_agency,
            },
        )
        id_file_type = get_or_create(
            cur, "file_type", _file_type_for(rinex_version, session)
        )
        get_or_create(
            cur,
            "data_center_structure",
            {"id_data_center": id_data_center, "id_file_type": id_file_type},
            {"directory_naming": "unknown", "comments": None},
        )

        md5checksum, md5uncompressed = rinex_md5s(file_path)
        values = {
            "name": file_path.name,
            "id_station": id_station,
            "id_data_center": id_data_center,
            "file_size": file_path.stat().st_size,
            "id_file_type": id_file_type,
            "relative_path": relative_path,
            "reference_date": observation_dt,
            "creation_date": None,
            "published_date": published_dt or datetime.now(),
            "md5checksum": md5checksum,
            "md5uncompressed": md5uncompressed,
            "status": 0,
        }

        # Upsert keyed on (name, relative_path) — no UNIQUE in schema, so do it
        # by hand; a re-index stamps revision_date.
        cur.execute(
            "SELECT id FROM rinex_file WHERE name = %s AND relative_path = %s",
            (file_path.name, relative_path),
        )
        hit = cur.fetchone()
        if hit is not None:
            values["revision_date"] = datetime.now()
            update_row(cur, "rinex_file", int(hit[0]), values)
            rid = int(hit[0])
        else:
            rid = insert_row(cur, "rinex_file", values)

    conn.commit()
    logger.info("indexed rinex_file %s (id=%s) for %s", file_path.name, rid, marker)
    return rid


# Supersede-cleanup: a new R3 long-name product replaces the legacy short-name
# file the old epos-gnss container pushed for the same day. After a durable
# push + index of the new file, remove the superseded legacy file (portal + DB).
# Bounded so a runaway can't delete an unexpectedly-large file; a RINEX daily is
# single-digit MB.
_SUPERSEDE_MAX_BYTES = 100 * 1024 * 1024


def deindex_rinex_file(conn, name: str) -> list[int]:
    """Delete the ``rinex_file`` row(s) for the exact ``name`` and return the ids.

    Keyed on ``name`` alone: a short RINEX name (``RHOF1770.26D.Z``) encodes
    station+year+DOY and is unique, so this targets exactly the superseded legacy
    row — NOT a DOY glob. Legacy rows carry a dir-only ``relative_path`` that
    differs from ours, so matching on name (not name+path) is both correct and
    deliberate here.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rinex_file WHERE name = %s RETURNING id", (name,))
        ids = [int(r[0]) for r in cur.fetchall()]
    conn.commit()
    if ids:
        logger.info("de-indexed legacy rinex_file %s (ids=%s)", name, ids)
    return ids


def supersede_legacy(
    conn,
    *,
    superseded_name: str,
    relative_dir: str,
    ssh_target: str,
    dest_root: str,
    dry_run: bool = True,
) -> dict:
    """Remove the legacy short-name file replaced by the new long-name product.

    Portal delete via the argv-safe SSH gateway (``remove_archive_files``), then
    (real runs only) de-index the row. Caller MUST gate on a durable push+index
    of the NEW file and ``superseded_name != new name`` (the R3-long case) — this
    function does not re-check that. ``dry_run`` shows intent without touching the
    portal or DB. Never raises (best-effort cleanup; the product is already live).
    """
    from ..archive.remove import remove_archive_files

    rel = (
        f"{relative_dir.rstrip('/')}/{superseded_name}"
        if relative_dir
        else superseded_name
    )
    out: dict = {
        "legacy_rel": rel,
        "removed": [],
        "would_remove": [],
        "skipped": [],
        "deindexed": [],
    }
    try:
        rm = remove_archive_files(
            [rel],
            ssh_target=ssh_target,
            dest_root=dest_root,
            max_size=_SUPERSEDE_MAX_BYTES,
            execute=not dry_run,
        )
        out["removed"] = [r for r, _ in rm.deleted]
        out["would_remove"] = [r for r, _ in rm.would_delete]
        out["skipped"] = (
            [r for r, _ in rm.skipped_toobig] + list(rm.missing) + list(rm.invalid)
        )
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        logger.warning("supersede portal delete failed for %s: %s", rel, exc)
    if not dry_run:
        try:
            out["deindexed"] = deindex_rinex_file(conn, superseded_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("supersede de-index failed for %s: %s", superseded_name, exc)
    return out
