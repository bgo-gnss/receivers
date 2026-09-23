"""archive_catalog restamp after an ``archive-sort`` relocation.

``archive-sort`` moves misfiled files through the rawdata gateway
(:func:`~receivers.archive.relocate.relocate_archive_files`). Until this module
existed it never touched ``archive_catalog``, so every executed sort left the
catalog wrong in both directions:

* the OLD path's row lingered, pointing at a file that is gone — it surfaced as
  ``missing`` in ``archive-verify`` (burying real gaps) and, worse, it was a
  **phantom**: ``archive-prune`` gates local deletion on catalog presence alone
  (``prune._archived_keys`` selects ``canonical_key`` with no existence or hash
  check), so a phantom row could authorise deleting the last local copy;
* the NEW path had no row at all — invisible to every catalog consumer.

This module closes that gap by **repointing** the existing row:

* **Driven off the gateway's confirmed set only.** The caller passes the
  ``(src, dst)`` pairs the remote script reported ``MOVED`` for
  (``RelocateResult.moved``). Requested pairs, ``would_move``, ``dst_exists``,
  ``missing`` and ``failed`` never reach a write — those files did not move in
  this run. A dry run may *preview* the ``would_move`` set (read-only).
* **Carry the hashes, never re-hash.** ``mv`` preserves bytes, so
  ``content_sha256``, ``compressed_sha256`` and ``file_size`` are invariant
  across the move and are deliberately absent from the ``UPDATE``. Re-hashing
  over the read-only NFS mount would be pure cost and would reintroduce the
  hash-a-file-that-may-have-changed race. ``last_verified_at`` is likewise left
  alone — verified-ness is a property of the bytes.
* **Identity from ``dst`` via :func:`~receivers.archive.path_parse.parse_archive_path`**
  — the codebase's single identity derivation (shared with ``reindex_files`` and
  ``backfill_archive_catalog``). The premise of a sort-move is that ``dst`` is
  now CORRECT; it is the plan's output made canonical, not a lossy proxy for a
  ``MovePlan`` (which one of the two call sites — ``--apply-plan`` — does not
  even have). The OLD row is found by the old logical key derived the same way
  from ``src``.
* **The logical key itself changes.** ``archive_catalog`` is UNIQUE on
  ``(storage_location, session_type, file_category, canonical_key)`` and a
  sort-move changes station, date and therefore ``canonical_key`` — so this is
  not a plain non-key UPDATE. A row already occupying the DESTINATION key is
  handled explicitly (:ref:`collision guard <collision>`), and the collision
  delete + repoint are ONE transaction per pair per host.

.. _collision:

**Collision guard — the one place that can destroy data.** ``relocate`` SKIPs a
pair whose destination FILE exists, so for a ``MOVED`` pair the destination did
not exist before the move; a row at the destination key is therefore almost
certainly a phantom — but it is proved, not assumed:

* occupying row's ``file_path`` == ``dst`` → phantom (that path was empty at
  move time) → removed, then repoint;
* occupying row's ``file_path`` != ``dst`` and that file is **absent** on the
  local read-only mount → phantom → removed, then repoint;
* occupying row's ``file_path`` != ``dst`` and that file **exists** — or its
  existence **cannot be probed** (no read mount, unmappable path) → **REFUSED**:
  both rows untouched, counted in ``refused_collision``. Never delete the only
  pointer to a live file.

**Source row missing → skip and report** (``uncatalogued``). If the file was
never catalogued there is nothing to repoint; inserting a fresh row would need
an NFS read + hash, which this path deliberately does not do (the same
``--only-existing`` semantics ``archive-reindex`` uses). Catalog it with
``archive-index-backfill``.

**Unreported pairs → a named unknown bucket, never a silent skip.** A gateway
reset mid-stream leaves pairs with no status line (``RelocateResult.unreported``);
their catalog state is unknown, the catalog is not touched, and they are listed
loudly with the remediation. No move+catalog atomicity across SSH is attempted.

All SQL is keyed on the NATURAL key, never ``id``: the identical-catalog set
(rek-d01 + pgdev) holds different surrogate ids per host, and the dual-write
connection refuses ``WHERE id = %s`` (``health.database_factory._DualCursor``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..utils.canonical_key import canonical_key
from .catalog import compression_suffix
from .path_parse import ParsedArchivePath, parse_archive_path

logger = logging.getLogger(__name__)

#: Existence probe for an archive-relative path on the local read-only mount.
#: ``None`` means "no mount to probe" — every collision is then refused.
ExistsProbe = Optional[Callable[[str], bool]]

_SQL_SELECT_ROW = """
    SELECT file_path FROM archive_catalog
     WHERE storage_location = %s AND session_type = %s
       AND file_category = %s AND canonical_key = %s
"""

_SQL_DELETE_ROW = """
    DELETE FROM archive_catalog
     WHERE storage_location = %s AND session_type = %s
       AND file_category = %s AND canonical_key = %s
"""

# Identity columns only. content_sha256 / compressed_sha256 / file_size /
# last_verified_at are NOT in the SET list on purpose — see the module docstring.
_SQL_REPOINT_ROW = """
    UPDATE archive_catalog
       SET station = %s, file_date = %s, file_hour = %s,
           session_type = %s, file_category = %s, canonical_key = %s,
           file_path = %s, compression = %s,
           indexed_at = CURRENT_TIMESTAMP
     WHERE storage_location = %s AND session_type = %s
       AND file_category = %s AND canonical_key = %s
"""

#: Bucket names in report order (per host).
BUCKETS = (
    "repointed",
    "collision_phantom_removed",
    "refused_collision",
    "uncatalogued",
    "unknown_unreported",
    "unparsable",
    "errors",
)


@dataclass
class RestampStats:
    """Per-host outcome of one restamp pass. Every pair lands in exactly one
    bucket (``collision_phantom_removed`` is additional to ``repointed``)."""

    repointed: list = field(default_factory=list)  # (src, dst)
    collision_phantom_removed: list = field(default_factory=list)  # (dst, phantom_path)
    refused_collision: list = field(default_factory=list)  # (src, dst, occ_path, why)
    uncatalogued: list = field(default_factory=list)  # (src, dst)
    unknown_unreported: list = field(default_factory=list)  # (src, dst)
    unparsable: list = field(default_factory=list)  # (src, dst)
    errors: list = field(default_factory=list)  # str
    dry_run: bool = False

    def counts(self) -> dict:
        return {name: len(getattr(self, name)) for name in BUCKETS}

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "counts": self.counts(),
            "repointed": [list(p) for p in self.repointed],
            "collision_phantom_removed": [
                list(p) for p in self.collision_phantom_removed
            ],
            "refused_collision": [list(p) for p in self.refused_collision],
            "uncatalogued": [list(p) for p in self.uncatalogued],
            "unknown_unreported": [list(p) for p in self.unknown_unreported],
            "unparsable": [list(p) for p in self.unparsable],
            "errors": list(self.errors),
        }

    @property
    def ok(self) -> bool:
        return not self.errors


def _rel_from_archive_path(file_path: str, dest_prefix: str) -> Optional[str]:
    """Map a catalog ``file_path`` (``<dest_prefix>/<rel>``) back to ``rel``.

    Returns ``None`` when the path does not sit under ``dest_prefix`` — the
    caller must then treat the file's existence as unknowable (and refuse),
    never as absent.
    """
    prefix = dest_prefix.rstrip("/") + "/"
    if file_path.startswith(prefix) and len(file_path) > len(prefix):
        return file_path[len(prefix) :]
    return None


def _identity(rel: str) -> Optional[tuple[ParsedArchivePath, str]]:
    """``(parsed, canonical_key)`` for an archive-relative path, or None."""
    parsed = parse_archive_path(rel, "")  # root='' → rel is already relative
    if parsed is None:
        return None
    return parsed, canonical_key(rel.rsplit("/", 1)[-1])


def _select_file_path(cur, key: tuple) -> Optional[str]:
    cur.execute(_SQL_SELECT_ROW, key)
    row = cur.fetchone()
    return row[0] if row else None


def _end_tx(conn, *, commit: bool) -> None:
    """Every pair ends its transaction explicitly — the server enforces
    ``idle_in_transaction_session_timeout``, and a dry run must leave no
    open read transaction behind."""
    if commit:
        conn.commit()
    else:
        conn.rollback()


def restamp_relocated_rows(
    conn,
    pairs: list[tuple[str, str]],
    *,
    storage_location: str,
    dest_prefix: str,
    exists_locally: ExistsProbe,
    unreported: Optional[list[tuple[str, str]]] = None,
    dry_run: bool = False,
    log: logging.Logger = logger,
) -> RestampStats:
    """Repoint the ``archive_catalog`` row of every ``(src, dst)`` pair in
    ``pairs`` from its OLD logical key to the NEW one on ``conn``.

    Args:
        conn: gps_health connection for ONE catalog host.
        pairs: the gateway-confirmed ``RelocateResult.moved`` pairs (execute)
            or ``RelocateResult.would_move`` (dry-run preview). Archive-relative
            paths (``YYYY/mon/STA/session/category/FILE``).
        storage_location: ``archive_catalog.storage_location`` of the archive
            (the sync target's ``name``, e.g. ``imo_archive``).
        dest_prefix: archive root the catalog's ``file_path`` is rooted at
            (the sync target's ``dest``, e.g. ``~/gpsdata``).
        exists_locally: probe ``rel -> bool`` against the local read-only mount,
            used ONLY to prove a colliding row is a phantom. ``None`` = no mount
            available; every collision is then refused.
        unreported: ``RelocateResult.unreported`` — bucketed, never touched.
        dry_run: read + classify, write NOTHING (each pair's read transaction
            is rolled back).

    Returns:
        :class:`RestampStats`. A per-pair failure rolls that pair back (the
        collision delete and the repoint share one transaction), is recorded in
        ``errors``, and does not stop the remaining pairs.
    """
    stats = RestampStats(dry_run=dry_run)
    stats.unknown_unreported = [tuple(p) for p in (unreported or [])]
    for src, dst in stats.unknown_unreported:
        log.error(
            "restamp: NO STATUS from gateway for %s -> %s — catalog state "
            "UNKNOWN, not touched",
            src,
            dst,
        )
    if conn is None:
        if pairs:
            stats.errors.append("no DB connection")
        return stats
    dest_prefix = dest_prefix.rstrip("/")

    for src, dst in pairs:
        old = _identity(src)
        new = _identity(dst)
        if old is None or new is None:
            stats.unparsable.append((src, dst))
            log.error("restamp: cannot derive catalog identity: %s -> %s", src, dst)
            continue
        old_parsed, old_key = old
        new_parsed, new_key = new
        old_lk = (
            storage_location,
            old_parsed.session_type,
            old_parsed.file_category,
            old_key,
        )
        new_lk = (
            storage_location,
            new_parsed.session_type,
            new_parsed.file_category,
            new_key,
        )
        new_path = f"{dest_prefix}/{new_parsed.relative_path}"
        filename = dst.rsplit("/", 1)[-1]

        try:
            with conn.cursor() as cur:
                if _select_file_path(cur, old_lk) is None:
                    stats.uncatalogued.append((src, dst))
                    log.warning(
                        "restamp: no catalog row at the old key for %s — "
                        "nothing to repoint (catalog it with archive-index-backfill)",
                        src,
                    )
                    _end_tx(conn, commit=False)
                    continue

                if new_lk != old_lk:
                    occ_path = _select_file_path(cur, new_lk)
                    if occ_path is not None:
                        why = _phantom_or_refuse(
                            occ_path, new_path, dest_prefix, exists_locally
                        )
                        if why is not None:
                            stats.refused_collision.append((src, dst, occ_path, why))
                            log.error(
                                "restamp: REFUSED %s -> %s — a row already occupies "
                                "the destination key and points at %s (%s); both "
                                "rows left untouched",
                                src,
                                dst,
                                occ_path,
                                why,
                            )
                            _end_tx(conn, commit=False)
                            continue
                        stats.collision_phantom_removed.append((dst, occ_path))
                        if dry_run:
                            log.info(
                                "restamp[DRY]: would remove phantom row at the "
                                "destination key (%s)",
                                occ_path,
                            )
                        else:
                            cur.execute(_SQL_DELETE_ROW, new_lk)
                            log.warning(
                                "restamp: removed phantom row at the destination "
                                "key (pointed at %s, absent)",
                                occ_path,
                            )

                if dry_run:
                    stats.repointed.append((src, dst))
                    log.info("restamp[DRY]: would repoint %s -> %s", src, dst)
                    _end_tx(conn, commit=False)
                    continue

                cur.execute(
                    _SQL_REPOINT_ROW,
                    (
                        new_parsed.station,
                        new_parsed.file_date,
                        new_parsed.file_hour,
                        new_parsed.session_type,
                        new_parsed.file_category,
                        new_key,
                        new_path,
                        compression_suffix(filename),
                        *old_lk,
                    ),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"repoint matched {cur.rowcount} row(s) at the old key "
                        f"(expected 1) — row changed under us"
                    )
            _end_tx(conn, commit=True)
            stats.repointed.append((src, dst))
            log.info("restamp: repointed %s -> %s", src, dst)
        except Exception as exc:  # noqa: BLE001 — one pair must not sink the run
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            # Undo the optimistic bookkeeping for this pair.
            if (
                stats.collision_phantom_removed
                and stats.collision_phantom_removed[-1][0] == dst
            ):
                stats.collision_phantom_removed.pop()
            stats.errors.append(f"{src} -> {dst}: {exc}")
            log.error("restamp: FAILED %s -> %s (rolled back): %s", src, dst, exc)
    return stats


def _phantom_or_refuse(
    occ_path: str, new_path: str, dest_prefix: str, exists_locally: ExistsProbe
) -> Optional[str]:
    """Decide the fate of a row already at the destination key.

    Returns ``None`` when the occupying row is a proven phantom (safe to
    remove), else the reason the pair must be REFUSED.
    """
    if occ_path == new_path:
        # The gateway only reports MOVED when dst did not exist, so a row that
        # already pointed at dst pointed at nothing.
        return None
    if exists_locally is None:
        return "cannot probe existence — no local read mount; refusing"
    rel = _rel_from_archive_path(occ_path, dest_prefix)
    if rel is None:
        return f"file_path not under {dest_prefix} — existence unknowable; refusing"
    try:
        present = bool(exists_locally(rel))
    except OSError as exc:
        return f"existence probe failed ({exc}); refusing"
    if present:
        return "occupying row points at a file that EXISTS"
    return None


def restamp_relocated_rows_multi(
    hosts: list,
    pairs: list[tuple[str, str]],
    *,
    storage_location: str,
    dest_prefix: str,
    exists_locally: ExistsProbe,
    unreported: Optional[list[tuple[str, str]]] = None,
    dry_run: bool = False,
    log: logging.Logger = logger,
) -> dict:
    """Restamp ``pairs`` on EVERY host in ``hosts`` (the identical-catalog set).

    Returns ``{host_label: RestampStats | None}`` (None = that host could not be
    connected / errored before any pair). Same shape as
    :func:`~receivers.archive.reindex.reindex_files_multi`; the caller must
    surface a per-host failure or a count mismatch loudly — a catalog that
    wrote to one DB but not the other is the divergence fan-out exists to prevent.
    """
    from ..db.connection import get_connection

    results: dict = {}
    for host in hosts:
        label = host or "localhost"
        conn = None
        try:
            conn = get_connection(host_override=host)
            results[label] = restamp_relocated_rows(
                conn,
                pairs,
                storage_location=storage_location,
                dest_prefix=dest_prefix,
                exists_locally=exists_locally,
                unreported=unreported,
                dry_run=dry_run,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("restamp on %s failed: %s", label, exc)
            results[label] = None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
    return results


def hosts_diverged(results: dict) -> bool:
    """True when the per-host outcomes are not identical — a host errored, or
    the bucket counts differ between hosts (one catalog took a write the other
    did not)."""
    if any(s is None for s in results.values()):
        return True
    counts = [s.counts() for s in results.values()]
    return any(c != counts[0] for c in counts[1:])
