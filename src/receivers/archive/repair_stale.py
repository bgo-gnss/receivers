"""archive_catalog stale-hash repair — re-hash rows whose catalog digest went stale.

``archive-verify`` reports a row as CORRUPT when the archive file's read-back
``content_sha256`` differs from the one the catalog holds. That finding has TWO
possible causes, and only one of them is fixed by re-hashing:

* **stale catalog hash** — the archive file was rewritten (a header fix pushed
  through rawdata) AFTER the catalog captured it, and the reindex never ran.
  The bytes are good; the row is out of date. Re-hashing is the repair.
* **genuine archive corruption** — the archive file is damaged. Re-hashing
  would BLESS the corruption: it overwrites the last record of what the file
  was supposed to be, silently, and the verify pass would then call the
  damaged file clean forever.

So this module never re-hashes a mismatch blindly. Each candidate must carry
the measured STALE signature before it is touched; anything else is reported,
never repaired:

1. **The file must decompress cleanly** — :func:`content_sha256` raises
   :class:`CorruptArchiveFileError` when it cannot. That is "not stale":
   bucket ``undecompressable``.
2. **The shape check, the real discriminator.** In every measured stale case
   (30 rows 2026-08-08..11, 28 rows 2020-2021, all ``gzip -t`` intact and
   parsing as valid RINEX) the pattern was ``file_tracking.content_sha256 ==
   archive_catalog.content_sha256`` while the on-disk hash differed from BOTH
   — the fingerprint of "the archive copy was rewritten after the catalog
   captured it, and the catalog still agrees with the pre-rewrite local
   record". Where a ``file_tracking`` row exists that exact shape is REQUIRED
   (``stale_confirmed``). Where none exists the evidence is weaker: the row is
   ``stale_unconfirmed`` and is repaired ONLY under an explicit opt-in
   (``include_unconfirmed``), never by default.
3. **Anything else** → ``report_only``: a three-way divergence, a file that
   decompresses to nothing (a stub — re-hashing it would catalogue a phantom),
   a file whose hash changed between the verify pass and this one, a path
   that does not parse to an archive identity.

The candidates come from :func:`~receivers.archive.verify.verify_archive_catalog`
(its structured ``mismatched_rows``), never from regexing its prose findings.
The repair itself is :func:`~receivers.archive.reindex.reindex_files_multi`
with an EXPLICIT file list and ``only_existing=True`` — this verb repairs rows
it was handed; it must never expand catalog coverage to files that were never
catalogued. No symlink tree: the archive mount is already in the
``YYYY/mon/STA/session/cat/FILE`` layout ``parse_archive_path`` needs.

Finally the write is PROVED, not assumed: every repaired file is re-hashed
once more and compared against EVERY catalog host by ``file_path`` (never by
``id`` — ids are assigned per host). The 116/116 by-hand result (58 files x 2
hosts) was established exactly this way; the reindex's own "N updated" is not
a substitute for it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..db.tx import read_only_cursor
from ..utils.content_hash import (
    EMPTY_CONTENT_SHA256,
    CorruptArchiveFileError,
    content_sha256,
)
from .path_parse import parse_archive_path
from .reindex import reindex_files_multi
from .verify import _local_session

logger = logging.getLogger(__name__)

#: Classification of one mismatch candidate. Exactly one per candidate.
CLASSES = (
    "stale_confirmed",  # file_tracking == catalog, on-disk differs from both
    "stale_unconfirmed",  # no file_tracking record — weaker evidence
    "already_current",  # on-disk == catalog now (repaired since the verify)
    "undecompressable",  # cannot be decompressed — possible REAL corruption
    "report_only",  # any other shape — never touched
)

#: Report buckets, in print order.
BUCKETS = (
    "repaired",
    "unconfirmed_skipped",
    "report_only",
    "undecompressable",
    "already_current",
)


@dataclass
class Candidate:
    """One ``archive-verify`` mismatch row, classified."""

    station: str
    session_type: Optional[str]
    file_category: str
    file_date: Optional[str]  # ISO string, as verify's to_dict() carries it
    file_path: str  # the catalog's stored archive path
    canonical_key: str
    catalog_sha256: str
    verify_on_disk_sha256: Optional[str]  # what the verify pass read back
    local_path: str  # where the verify pass found the file (under read_root)
    # Derived here.
    relative_path: Optional[str] = None  # archive-relative, from the path parse
    archive_path: Optional[str] = None  # <dest_prefix>/<relative_path>
    file_hour: Optional[int] = None
    on_disk_sha256: Optional[str] = None  # recomputed by this pass
    local_sha256: Optional[str] = None  # file_tracking.content_sha256
    cls: str = "report_only"
    reason: str = ""
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "station": self.station,
            "session_type": self.session_type,
            "file_category": self.file_category,
            "file_date": self.file_date,
            "file_hour": self.file_hour,
            "file_path": self.file_path,
            "archive_path": self.archive_path,
            "canonical_key": self.canonical_key,
            "local_path": self.local_path,
            "catalog_sha256": self.catalog_sha256,
            "verify_on_disk_sha256": self.verify_on_disk_sha256,
            "on_disk_sha256": self.on_disk_sha256,
            "local_sha256": self.local_sha256,
            "class": self.cls,
            "reason": self.reason,
            "notes": list(self.notes),
        }


@dataclass
class RecheckResult:
    """E4 outcome for ONE catalog host: the repaired rows re-read by file_path
    and compared to a FRESH on-disk hash."""

    matched: int = 0
    mismatched: list = field(default_factory=list)  # (file_path, catalog, on_disk)
    absent: list = field(default_factory=list)  # file_path with no row on this host
    error: Optional[str] = None  # host unreachable / query failed

    @property
    def ok(self) -> bool:
        return self.error is None and not self.mismatched and not self.absent

    def to_dict(self) -> dict:
        return {
            "matched": self.matched,
            "mismatched": [list(m) for m in self.mismatched],
            "absent": list(self.absent),
            "error": self.error,
            "ok": self.ok,
        }


@dataclass
class RepairStats:
    """Outcome of one repair pass."""

    dry_run: bool = True
    include_unconfirmed: bool = False
    items: list = field(default_factory=list)  # every Candidate, classified
    # {host_label: ReindexStats | None} — None = that host errored.
    hosts: dict = field(default_factory=dict)
    # {host_label: RecheckResult} — empty on a dry run (nothing to prove).
    recheck: dict = field(default_factory=dict)
    # A file the E4 pass could not re-hash (changed / vanished mid-run).
    recheck_unhashable: list = field(default_factory=list)  # (local_path, why)
    diverged: bool = False  # per-host reindex outcomes not identical
    problems: list = field(default_factory=list)  # anything that must exit non-zero

    # -- buckets --------------------------------------------------------
    def _by_class(self, cls: str) -> list:
        return [c for c in self.items if c.cls == cls]

    @property
    def repair_set(self) -> list:
        """The candidates this pass repairs (or would, on a dry run)."""
        out = self._by_class("stale_confirmed")
        if self.include_unconfirmed:
            out += self._by_class("stale_unconfirmed")
        return out

    @property
    def repaired(self) -> list:
        return self.repair_set

    @property
    def unconfirmed_skipped(self) -> list:
        return [] if self.include_unconfirmed else self._by_class("stale_unconfirmed")

    @property
    def report_only(self) -> list:
        return self._by_class("report_only")

    @property
    def undecompressable(self) -> list:
        return self._by_class("undecompressable")

    @property
    def already_current(self) -> list:
        return self._by_class("already_current")

    def counts(self) -> dict:
        return {name: len(getattr(self, name)) for name in BUCKETS}

    @property
    def recheck_ok(self) -> bool:
        if self.dry_run:
            return True
        if not self.repair_set:
            return True
        if not self.recheck or set(self.recheck) != set(self.hosts):
            return False
        return not self.recheck_unhashable and all(r.ok for r in self.recheck.values())

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "include_unconfirmed": self.include_unconfirmed,
            "counts": self.counts(),
            "items": [c.to_dict() for c in self.items],
            "hosts": {
                label: (st.to_dict() if st is not None else None)
                for label, st in self.hosts.items()
            },
            "diverged": self.diverged,
            "recheck": {label: r.to_dict() for label, r in self.recheck.items()},
            "recheck_unhashable": [list(p) for p in self.recheck_unhashable],
            "recheck_ok": self.recheck_ok,
            "problems": list(self.problems),
            "ok": self.ok,
        }


# ------------------------------------------------------------------ classify


def _tracking_sha(
    conn,
    *,
    station: str,
    session_type: str,
    file_category: str,
    file_date: Optional[date],
    file_hour: Optional[int],
) -> tuple[bool, Optional[str]]:
    """``(found, content_sha256)`` of the local ``file_tracking`` record for
    this archive identity.

    Same mapping as the verify cross-check (``_local_session``), but keyed on
    the FULL slot ``(sid, session_type, file_date, file_hour)`` — unique per
    migration 063 — so an hourly product is corroborated by its own hour's
    record, not silently by nothing. ``found`` is False when no record carries
    a hash; a NULL hash is the same as no record for this purpose (the
    integrity checker fills it lazily).
    """
    if conn is None or file_date is None:
        return False, None
    local_session = _local_session(session_type, file_category)
    # A literal predicate per branch (not IS NOT DISTINCT FROM) so the slot
    # index stays usable — see the matching note in verify.py.
    if file_hour is None:
        sql = """SELECT content_sha256 FROM file_tracking
                 WHERE sid = %s AND session_type = %s AND file_date = %s
                   AND file_hour IS NULL AND content_sha256 IS NOT NULL"""
        params: tuple = (station, local_session, file_date)
    else:
        sql = """SELECT content_sha256 FROM file_tracking
                 WHERE sid = %s AND session_type = %s AND file_date = %s
                   AND file_hour = %s AND content_sha256 IS NOT NULL"""
        params = (station, local_session, file_date, file_hour)
    with read_only_cursor(conn) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if row is None or row[0] is None:
        return False, None
    return True, row[0]


def _candidate_from_row(row: dict) -> Candidate:
    """Build a Candidate from one ``VerifyStats.mismatched_rows`` dict.

    Indexes the keys ``verify._row_record`` guarantees — a missing one is a
    broken contract and must raise here, not surface later as a silent None.
    ``on_disk_sha256`` is present on mismatches only, hence ``.get``.
    """
    return Candidate(
        station=row["station"],
        session_type=row["session_type"],
        file_category=row["file_category"],
        file_date=row["file_date"],
        file_path=row["file_path"],
        canonical_key=row["canonical_key"],
        catalog_sha256=row["content_sha256"],
        verify_on_disk_sha256=row.get("on_disk_sha256"),
        local_path=row["local_path"],
    )


def classify_candidates(
    rows: list,
    *,
    read_root: str,
    dest_prefix: str,
    tracking_conn,
    log: logging.Logger = logger,
) -> list:
    """Classify every ``archive-verify`` mismatch row into one of :data:`CLASSES`.

    Args:
        rows: ``VerifyStats.mismatched_rows`` — dicts carrying the catalog's
            ``content_sha256``, the verify pass's ``on_disk_sha256`` and the
            ``local_path`` it read.
        read_root: the archive mount the ``local_path`` values sit under.
        dest_prefix: archive dest the catalog's ``file_path`` is rooted at
            (what the reindex will write).
        tracking_conn: gps_health connection holding ``file_tracking`` (the
            operational host). ``None`` → every candidate is at best
            ``stale_unconfirmed``.

    Every file is re-hashed HERE, independently of the verify pass — the
    decompress-cleanly guard must hold at repair time, and a file whose hash
    moved since the verify is in flux and must not be touched.
    """
    dest_prefix = dest_prefix.rstrip("/")
    out: list = []
    for row in rows:
        c = _candidate_from_row(row)
        out.append(c)

        if not c.local_path or not c.catalog_sha256:
            c.cls, c.reason = (
                "report_only",
                "verify row lacks local_path/content_sha256",
            )
            continue

        parsed = parse_archive_path(c.local_path, read_root)
        if parsed is None:
            c.cls = "report_only"
            c.reason = (
                f"local_path does not parse to an archive identity under "
                f"{read_root} (misconfigured --read-root/--dest-prefix?)"
            )
            continue
        c.relative_path = parsed.relative_path
        c.archive_path = f"{dest_prefix}/{parsed.relative_path}"
        c.file_hour = parsed.file_hour
        # The reindex derives identity from the PATH; note any disagreement with
        # the row so a repaired row that also moved its date/station is visible.
        if parsed.file_date is not None and c.file_date not in (
            None,
            parsed.file_date.isoformat(),
        ):
            c.notes.append(
                f"row file_date {c.file_date} != path-derived "
                f"{parsed.file_date.isoformat()} (reindex writes the latter)"
            )
        if c.file_path != c.archive_path:
            c.notes.append(
                f"row file_path {c.file_path} will be normalised to {c.archive_path}"
            )
        if (
            parsed.station != c.station
            or parsed.session_type != c.session_type
            or parsed.file_category != c.file_category
        ):
            c.cls = "report_only"
            c.reason = (
                "path-derived identity "
                f"{parsed.station}/{parsed.session_type}/{parsed.file_category} "
                f"!= row {c.station}/{c.session_type}/{c.file_category}"
            )
            continue

        # Guard 1 — the file must be present and decompress cleanly NOW.
        if not os.path.isfile(c.local_path):
            c.cls, c.reason = "report_only", "file vanished since the verify pass"
            continue
        try:
            c.on_disk_sha256 = content_sha256(c.local_path)
        except (CorruptArchiveFileError, OSError) as exc:
            c.cls = "undecompressable"
            c.reason = f"cannot decompress — possible REAL corruption: {exc}"
            log.error("repair-stale: NOT re-hashed, %s: %s", c.reason, c.local_path)
            continue

        if c.on_disk_sha256 == EMPTY_CONTENT_SHA256:
            c.cls = "report_only"
            c.reason = (
                "decompresses to NOTHING (a stub) — re-hashing would catalogue "
                "a phantom row"
            )
            continue
        if c.on_disk_sha256 == c.catalog_sha256:
            c.cls, c.reason = (
                "already_current",
                "catalog already holds the on-disk hash",
            )
            continue
        if (
            c.verify_on_disk_sha256 is not None
            and c.on_disk_sha256 != c.verify_on_disk_sha256
        ):
            c.cls = "report_only"
            c.reason = (
                "on-disk hash changed between the verify pass and now — file in "
                "flux, re-run later"
            )
            continue

        # Guard 2 — the shape check against the local file_tracking record.
        found, local_sha = _tracking_sha(
            tracking_conn,
            station=parsed.station,
            session_type=parsed.session_type,
            file_category=parsed.file_category,
            file_date=parsed.file_date,
            file_hour=parsed.file_hour,
        )
        c.local_sha256 = local_sha
        if not found:
            c.cls = "stale_unconfirmed"
            c.reason = "no file_tracking hash to corroborate (repair needs opt-in)"
            continue
        if local_sha == c.catalog_sha256:
            # local == catalog, on-disk differs from both: the measured signature.
            c.cls = "stale_confirmed"
            c.reason = "file_tracking == catalog, on-disk differs from both"
            continue
        if local_sha == c.on_disk_sha256:
            c.cls = "report_only"
            c.reason = (
                "file_tracking == on-disk, catalog differs — not the measured "
                "stale signature (local copy rewritten too?)"
            )
            continue
        c.cls = "report_only"
        c.reason = "three-way divergence: file_tracking, catalog and on-disk all differ"
    return out


# --------------------------------------------------------------------- apply


def _reindex_counts(st) -> tuple:
    return (
        st.updated,
        st.inserted,
        st.unchanged,
        st.skipped,
        st.skipped_new,
        len(st.errors),
    )


def reindex_hosts_diverged(results: dict) -> bool:
    """True when the per-host reindex outcomes are not identical — a host
    errored, or the counts differ (one catalog took a write the other did
    not, or lacked a row the other had)."""
    if any(st is None for st in results.values()):
        return True
    counts = [_reindex_counts(st) for st in results.values()]
    return any(c != counts[0] for c in counts[1:])


def recheck_repaired(
    hosts: list,
    repair_set: list,
    *,
    storage_location: str,
    log: logging.Logger = logger,
) -> tuple[dict, list]:
    """E4 — prove the write. Re-hash every repaired file ONCE more and compare
    against EVERY host's row, looked up by ``file_path`` (never ``id``).

    Returns ``({host_label: RecheckResult}, unhashable)`` where ``unhashable``
    lists ``(local_path, why)`` for files that could not be re-hashed (they
    are then reported against every host as a problem, not silently skipped).
    """
    from ..db.connection import get_connection

    fresh: dict = {}
    unhashable: list = []
    for c in repair_set:
        try:
            digest = content_sha256(c.local_path)
        except (CorruptArchiveFileError, OSError) as exc:
            unhashable.append((c.local_path, str(exc)))
            log.error("repair-stale recheck: cannot re-hash %s: %s", c.local_path, exc)
            continue
        if c.on_disk_sha256 is not None and digest != c.on_disk_sha256:
            unhashable.append(
                (c.local_path, "hash changed during the run — file in flux")
            )
            log.error(
                "repair-stale recheck: %s changed during the run (%s -> %s)",
                c.local_path,
                c.on_disk_sha256[:12],
                digest[:12],
            )
            continue
        fresh[c.archive_path] = digest

    results: dict = {}
    for host in hosts:
        label = host or "localhost"
        res = RecheckResult()
        results[label] = res
        conn = None
        try:
            # single_host: a read must not go through the dual-write wrapper;
            # each host is proved on its own.
            conn = get_connection(host_override=host, single_host=True)
            for c in repair_set:
                if c.archive_path not in fresh:
                    continue
                with read_only_cursor(conn) as cur:
                    cur.execute(
                        """SELECT content_sha256 FROM archive_catalog
                           WHERE storage_location = %s AND file_path = %s""",
                        (storage_location, c.archive_path),
                    )
                    rows = cur.fetchall()
                if not rows:
                    res.absent.append(c.archive_path)
                    continue
                stored = [r[0] for r in rows]
                if all(s == fresh[c.archive_path] for s in stored):
                    res.matched += 1
                else:
                    res.mismatched.append(
                        (
                            c.archive_path,
                            next(s for s in stored if s != fresh[c.archive_path]),
                            fresh[c.archive_path],
                        )
                    )
        except Exception as exc:  # noqa: BLE001 — must report, never raise
            res.error = str(exc)
            log.error("repair-stale recheck on %s FAILED: %s", label, exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        if res.ok:
            log.info(
                "repair-stale recheck %s: %d/%d matched", label, res.matched, len(fresh)
            )
        else:
            log.error(
                "repair-stale recheck %s: %d/%d matched, %d MISMATCH, %d absent%s",
                label,
                res.matched,
                len(fresh),
                len(res.mismatched),
                len(res.absent),
                f", error: {res.error}" if res.error else "",
            )
    return results, unhashable


def repair_stale_rows(
    rows: list,
    *,
    hosts: list,
    read_root: str,
    storage_location: str,
    dest_prefix: str,
    tracking_conn,
    dry_run: bool = True,
    include_unconfirmed: bool = False,
    verify_unreadable: int = 0,
    log: logging.Logger = logger,
) -> RepairStats:
    """Classify ``rows`` (``VerifyStats.mismatched_rows``), repair the provably
    stale ones on every host in ``hosts``, and PROVE the write.

    Args:
        rows: structured mismatch rows from an in-process verify pass.
        hosts: catalog hosts from :func:`~receivers.archive.reindex.resolve_catalog_hosts`
            (``None`` in the list = the default connection).
        read_root: local archive mount the verify pass read (files are hashed
            from here; it is the ``root`` the reindex parses identity against).
        storage_location: ``archive_catalog.storage_location``.
        dest_prefix: archive dest the ``file_path`` is rooted at.
        tracking_conn: connection holding ``file_tracking`` for the shape check.
        dry_run: classify + report; write NOTHING (the reindex runs in its own
            dry-run so the per-host "would update / no prior row" is still shown).
        include_unconfirmed: also repair ``stale_unconfirmed`` rows.
        verify_unreadable: how many of the verify pass's ``mismatched`` were
            NOT enumerable (unreadable at verify time — they live only in its
            prose findings). Counted as a problem so they cannot go unnoticed.

    Returns:
        :class:`RepairStats`; ``problems`` non-empty means exit non-zero.
    """
    stats = RepairStats(dry_run=dry_run, include_unconfirmed=include_unconfirmed)
    stats.items = classify_candidates(
        rows,
        read_root=read_root,
        dest_prefix=dest_prefix,
        tracking_conn=tracking_conn,
        log=log,
    )

    if verify_unreadable:
        stats.problems.append(
            f"{verify_unreadable} file(s) unreadable at verify time (not enumerable "
            "— see the verify findings); possible corruption, never re-hashed"
        )
    if stats.undecompressable:
        stats.problems.append(
            f"{len(stats.undecompressable)} candidate(s) do not decompress — "
            "possible REAL corruption, NOT re-hashed"
        )
    if stats.report_only:
        stats.problems.append(
            f"{len(stats.report_only)} candidate(s) do not carry the stale "
            "signature — reported, not repaired"
        )

    repair_set = stats.repair_set
    if not repair_set:
        return stats

    # E3 — explicit file list, root = the mount, only_existing mandatory.
    stats.hosts = reindex_files_multi(
        hosts,
        [c.local_path for c in repair_set],
        root=read_root,
        storage_location=storage_location,
        dest_prefix=dest_prefix,
        dry_run=dry_run,
        only_existing=True,
        log=log,
    )
    stats.diverged = reindex_hosts_diverged(stats.hosts)
    for label, st in stats.hosts.items():
        if st is None:
            stats.problems.append(f"reindex FAILED on {label} — catalogs may DIVERGE")
        elif st.errors:
            stats.problems.append(f"reindex on {label}: {len(st.errors)} error(s)")
        elif st.skipped_new:
            stats.problems.append(
                f"{label}: {st.skipped_new} repaired row(s) have NO row on this "
                "host (only_existing — not inserted); catalogs DIVERGE"
            )
    if stats.diverged:
        stats.problems.append("per-host reindex outcomes differ — catalogs DIVERGED")

    if dry_run:
        return stats

    # E4 — prove it.
    stats.recheck, stats.recheck_unhashable = recheck_repaired(
        hosts, repair_set, storage_location=storage_location, log=log
    )
    for lp, why in stats.recheck_unhashable:
        stats.problems.append(f"re-check could not re-hash {lp}: {why}")
    for label, res in stats.recheck.items():
        if res.error:
            stats.problems.append(f"re-check on {label} FAILED: {res.error}")
        if res.mismatched:
            stats.problems.append(
                f"re-check on {label}: {len(res.mismatched)} row(s) do NOT hold "
                "the on-disk hash after the write"
            )
        if res.absent:
            stats.problems.append(
                f"re-check on {label}: {len(res.absent)} repaired row(s) ABSENT"
            )
    return stats
