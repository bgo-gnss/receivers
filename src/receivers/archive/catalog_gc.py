"""archive_catalog phantom-row GC — delete catalog rows whose archive file is gone.

``archive_catalog`` (``storage_location='imo_archive'``) holds a measured
**14,324 phantom rows**: rows whose archive file does not exist. Every one
carries :data:`~receivers.utils.content_hash.EMPTY_CONTENT_SHA256` — the
SHA-256 of zero bytes — and no other digest occurs in the set. By ``file_size``
they are a 3-byte ``compress`` header (``1f 9d 90``, an empty ``.Z``; 8,535
rows), a literal 0-byte file (5,677), a gzip-of-nothing carrying its stored
filename (33 B, 154) and one 42-byte one-off. They were written by earlier
reindex/backfill passes before the empty-digest guard existed.

They are dangerous, not cosmetic. ``archive-prune`` gates LOCAL deletion on
catalog presence alone — ``prune._archived_keys`` selects ``canonical_key``
with no existence or hash check — so a phantom row can authorise deleting the
last local copy of data whose archive copy is gone. That cost one day of raw
(ROTH 2026-07-04, since recovered).

No existing verb fits: ``archive-prune`` is the local ring-buffer prune,
``archive-rm`` prunes rows only for files IT deletes, ``archive-reindex`` only
upserts. This module is the missing one, and it is built so a *wrong*
invocation under-deletes rather than over-deletes:

* **Selection is the empty-digest sweep, not a 9.2M-row walk.** Rows are
  fetched ``WHERE storage_location = %s AND content_sha256 = EMPTY_CONTENT_SHA256``
  — exactly the provable-stub population, cheap, no full-table scan. The
  discriminator is imported from :mod:`receivers.utils.content_hash`, never
  re-declared, so it stays the single value the backfill guard also uses.
* **The path is mapped with verify's OWN helper** (``_local_archive_path``).
  A second, divergent mapping is the anti-pattern that produced this class of
  bug in the first place. A ``file_path`` not under ``dest_prefix`` is
  *unmappable* and reported, never deleted — existence cannot be proved.
* **State is re-derived from the FILESYSTEM for every row; the row alone is
  never trusted** (its ``file_size`` can lie — that is how it got this way):

  - file ABSENT + empty digest → **GC candidate** (delete the row);
  - file **PRESENT** (any inode, even a dangling symlink) → **never deleted**
    (``present_kept``). A present file with the empty digest is a *live stub*
    on the archive — a different problem (``archive-rm``), reported here;
  - file ABSENT + a REAL digest → **report only** ("relocated or lost"): those
    need a repoint or a re-walk, never a delete, because the row may be the
    only pointer to data whose archive copy merely moved. Empty under the
    default selection; kept because a caller may feed ``VerifyStats.missing_rows``
    (structured, carries ``content_sha256``) through the same classifier.

* **Two bounded guards.** ``max_size`` refuses a row whose claimed size exceeds
  it — checked at classification AND re-read from each host immediately before
  the delete. A fraction guard refuses the whole run when it would delete more
  than ``fraction_limit`` of the location's rows (14,324 of 9.2M is 0.16 %;
  the 2 % default is far above a correct run) unless ``force`` is set.
* **Writes fan out to every catalog host** on a single-host connection each
  (never the dual-write mirror — a delete must be explicit per host), keyed on
  the natural logical key ``(storage_location, session_type, file_category,
  canonical_key)`` and **never on ``id``**, which is assigned independently per
  host. Per-host counts are compared; a difference is reported as DIVERGENCE.
* **Dry-run is the default.** Nothing is deleted without ``dry_run=False``.
* ``rinex_org`` rows are out of scope by constraint and are reported, never
  touched.

**Known limit — the truncated-header stub.** A third stub class exists: a
RINEX truncated inside its header (e.g. 268 B, no ``END OF HEADER``, zero
epochs — ``2017/mar/HLID/15s_24hr/rinex/HLID0900.17D.Z``). It carries a REAL
digest, so an absent one lands in report-only here — the safe direction. For
an ABSENT file there is no content to inspect, so this pass deliberately does
not attempt header inspection; that belongs to a verify/audit of PRESENT files.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..db.tx import read_only_cursor
from ..utils.content_hash import EMPTY_CONTENT_SHA256

# Deliberately the module-private helper from verify: the ONE stored-path →
# read-root translation. Reimplementing it here (even identically) would be
# a second mapping that can drift from the one archive-verify uses to decide
# "missing" — and two mappings disagreeing about where a file lives is exactly
# how phantom rows are minted. Reuse it; if it needs to change, change it there.
from .verify import _local_archive_path

logger = logging.getLogger(__name__)
audit = logging.getLogger("receivers.audit")

#: Classification of one selected row. Exactly one per row.
CLASSES = (
    "gc_candidate",  # absent + empty digest + size within cap → delete the row
    "present_kept",  # the file EXISTS — never deleted, whatever the digest
    "report_only",  # absent + REAL digest (relocated/lost), unmappable, rinex_org
    "oversized_refused",  # absent + empty digest but claimed size > max_size
)

#: Default ``max_size``: every MEASURED stub shape (0 / 3 / 33 / 42 bytes) fits
#: under it with margin; a row claiming more is not a known stub shape and is
#: refused until an operator raises the cap deliberately (bounded, like
#: ``archive-rm --max-size``).
DEFAULT_MAX_SIZE = 64

#: Default fraction guard: refuse a run that would delete more than this share
#: of the location's rows. The measured phantom set is 0.16 % of the catalog.
DEFAULT_FRACTION_LIMIT = 0.02

#: Rows per commit on the delete path (idempotent — a re-run continues).
_COMMIT_EVERY = 500

_SQL_SELECT_STUBS = """
    SELECT station, session_type, file_category, file_date, file_path,
           canonical_key, content_sha256, file_size
      FROM archive_catalog
     WHERE storage_location = %s AND content_sha256 = %s
     ORDER BY file_date NULLS LAST, station, session_type, file_category,
              canonical_key
"""

_SQL_COUNT_LOCATION = """
    SELECT count(*) FROM archive_catalog WHERE storage_location = %s
"""

# Natural-key predicates. A literal `IS NULL` branch rather than
# `IS NOT DISTINCT FROM`: the latter is not btree-indexable and demotes
# session_type out of the archive_catalog_logical_key Index Cond (see the
# matching note in verify.py). NEVER `WHERE id = %s` — ids differ per host.
_SQL_ROW_BY_KEY = """
    SELECT file_size, content_sha256 FROM archive_catalog
     WHERE storage_location = %s AND session_type = %s
       AND file_category = %s AND canonical_key = %s
"""
_SQL_ROW_BY_KEY_NULL_SESSION = """
    SELECT file_size, content_sha256 FROM archive_catalog
     WHERE storage_location = %s AND session_type IS NULL
       AND file_category = %s AND canonical_key = %s
"""
_SQL_DELETE_BY_KEY = """
    DELETE FROM archive_catalog
     WHERE storage_location = %s AND session_type = %s
       AND file_category = %s AND canonical_key = %s
"""
_SQL_DELETE_BY_KEY_NULL_SESSION = """
    DELETE FROM archive_catalog
     WHERE storage_location = %s AND session_type IS NULL
       AND file_category = %s AND canonical_key = %s
"""


def _key_sql(select: bool, session_type: Optional[str]) -> str:
    if session_type is None:
        return (
            _SQL_ROW_BY_KEY_NULL_SESSION if select else _SQL_DELETE_BY_KEY_NULL_SESSION
        )
    return _SQL_ROW_BY_KEY if select else _SQL_DELETE_BY_KEY


def _key_params(storage_location: str, r: GcRow) -> tuple:
    """Bind params matching :func:`_key_sql` — 3 for a NULL session, else 4."""
    if r.session_type is None:
        return (storage_location, r.file_category, r.canonical_key)
    return (storage_location, r.session_type, r.file_category, r.canonical_key)


@dataclass
class GcRow:
    """One selected catalog row, classified against the filesystem."""

    station: Optional[str]
    session_type: Optional[str]
    file_category: str
    file_date: Optional[str]  # ISO string (JSON-safe), or None
    file_path: str  # the catalog's stored archive path
    canonical_key: str
    content_sha256: Optional[str]
    file_size: Optional[int]  # the row's CLAIM — never trusted for existence
    local_path: Optional[str] = None  # where this pass looked (under read_root)
    cls: str = "report_only"
    reason: str = ""

    @property
    def year(self) -> str:
        if self.file_date:
            return str(self.file_date)[:4]
        # Fall back to the leading YYYY segment of the archive path.
        for seg in self.file_path.replace("\\", "/").split("/"):
            if len(seg) == 4 and seg.isdigit():
                return seg
        return "unknown"

    def to_dict(self) -> dict:
        return {
            "station": self.station,
            "session_type": self.session_type,
            "file_category": self.file_category,
            "file_date": self.file_date,
            "file_path": self.file_path,
            "canonical_key": self.canonical_key,
            "content_sha256": self.content_sha256,
            "file_size": self.file_size,
            "local_path": self.local_path,
            "class": self.cls,
            "reason": self.reason,
        }


@dataclass
class HostGcResult:
    """Outcome of the delete pass on ONE catalog host."""

    deleted: int = 0
    would_delete: int = 0  # dry-run
    refused_oversized: list = field(default_factory=list)  # (file_path, size)
    digest_changed: list = field(
        default_factory=list
    )  # file_path — row no longer a stub
    file_appeared: list = field(
        default_factory=list
    )  # file_path — present at delete time
    absent_on_host: list = field(default_factory=list)  # file_path — no row here
    error: Optional[str] = None  # host unreachable / statement failed

    def counts(self) -> tuple:
        return (
            self.deleted,
            self.would_delete,
            len(self.refused_oversized),
            len(self.digest_changed),
            len(self.file_appeared),
            len(self.absent_on_host),
        )

    @property
    def ok(self) -> bool:
        return self.error is None and not self.absent_on_host

    def to_dict(self) -> dict:
        return {
            "deleted": self.deleted,
            "would_delete": self.would_delete,
            "refused_oversized": [list(p) for p in self.refused_oversized],
            "digest_changed": list(self.digest_changed),
            "file_appeared": list(self.file_appeared),
            "absent_on_host": list(self.absent_on_host),
            "error": self.error,
            "ok": self.ok,
        }


@dataclass
class GcStats:
    """Outcome of one GC pass."""

    dry_run: bool = True
    max_size: int = DEFAULT_MAX_SIZE
    fraction_limit: float = DEFAULT_FRACTION_LIMIT
    force: bool = False
    storage_location: str = "imo_archive"
    total_rows: Optional[int] = None  # rows at storage_location (fraction base)
    items: list = field(default_factory=list)  # every GcRow, classified
    hosts: dict = field(default_factory=dict)  # {host_label: HostGcResult}
    diverged: bool = False
    refused_fraction: bool = False
    problems: list = field(default_factory=list)  # anything that exits non-zero

    def _by_class(self, cls: str) -> list:
        return [r for r in self.items if r.cls == cls]

    @property
    def candidates(self) -> list:
        return self._by_class("gc_candidate")

    @property
    def present_kept(self) -> list:
        return self._by_class("present_kept")

    @property
    def report_only(self) -> list:
        return self._by_class("report_only")

    @property
    def oversized_refused(self) -> list:
        return self._by_class("oversized_refused")

    @property
    def live_stubs(self) -> list:
        """Present files whose catalog digest is the empty one — stubs that
        still exist on the archive. Not GC; surfaced for ``archive-rm``."""
        return [
            r for r in self.present_kept if r.content_sha256 == EMPTY_CONTENT_SHA256
        ]

    @property
    def fraction(self) -> Optional[float]:
        if not self.total_rows:
            return None
        return len(self.candidates) / self.total_rows

    def counts(self) -> dict:
        return {name: len(self._by_class(name)) for name in CLASSES}

    def breakdown(self, rows: Optional[list] = None) -> dict:
        """Candidate counts by station, by year and by session."""
        rows = self.candidates if rows is None else rows
        return {
            "by_station": dict(
                sorted(Counter(r.station or "unknown" for r in rows).items())
            ),
            "by_year": dict(sorted(Counter(r.year for r in rows).items())),
            "by_session": dict(
                sorted(Counter(r.session_type or "(none)" for r in rows).items())
            ),
        }

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "max_size": self.max_size,
            "fraction_limit": self.fraction_limit,
            "force": self.force,
            "storage_location": self.storage_location,
            "total_rows": self.total_rows,
            "fraction": self.fraction,
            "counts": self.counts(),
            "breakdown": self.breakdown(),
            "items": [r.to_dict() for r in self.items],
            "hosts": {label: h.to_dict() for label, h in self.hosts.items()},
            "diverged": self.diverged,
            "refused_fraction": self.refused_fraction,
            "problems": list(self.problems),
            "ok": self.ok,
        }


# ------------------------------------------------------------------- select


def _iso(d) -> Optional[str]:
    if d is None:
        return None
    return d.isoformat() if isinstance(d, date) else str(d)


def select_stub_rows(
    conn,
    *,
    storage_location: str,
    limit: Optional[int] = None,
) -> list[dict]:
    """The empty-digest sweep: rows at ``storage_location`` whose
    ``content_sha256`` is :data:`EMPTY_CONTENT_SHA256`, oldest first.

    Returns dicts in the same shape ``verify._row_record`` produces (plus
    ``file_size``) so :func:`classify_rows` accepts either source.
    """
    sql = _SQL_SELECT_STUBS
    params: tuple = (storage_location, EMPTY_CONTENT_SHA256)
    if limit is not None:
        sql += " LIMIT %s"
        params += (int(limit),)
    with read_only_cursor(conn) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {
            "station": r[0],
            "session_type": r[1],
            "file_category": r[2],
            "file_date": _iso(r[3]),
            "file_path": r[4],
            "canonical_key": r[5],
            "content_sha256": r[6],
            "file_size": r[7],
        }
        for r in rows
    ]


def count_location_rows(conn, *, storage_location: str) -> int:
    """Row count at ``storage_location`` — the base of the fraction guard."""
    with read_only_cursor(conn) as cur:
        cur.execute(_SQL_COUNT_LOCATION, (storage_location,))
        row = cur.fetchone()
    return int(row[0]) if row else 0


# ----------------------------------------------------------------- classify


def _under_prefix(file_path: str, dest_prefix: str) -> bool:
    prefix = dest_prefix.rstrip("/") + "/"
    return file_path.startswith(prefix) and len(file_path) > len(prefix)


def _present(local_path: str) -> bool:
    """Any inode at the path — a regular file, a directory, even a dangling
    symlink — counts as PRESENT. Presence is the never-delete signal, so the
    probe errs towards 'present'."""
    return os.path.lexists(local_path)


def classify_rows(
    rows: list,
    *,
    read_root: str,
    dest_prefix: str,
    max_size: int = DEFAULT_MAX_SIZE,
) -> list:
    """Classify every selected row into one of :data:`CLASSES` — from the
    FILESYSTEM, never from the row alone.

    Args:
        rows: dicts from :func:`select_stub_rows` or ``VerifyStats.missing_rows``
            (``file_size`` optional in the latter).
        read_root: local read-only mount of the archive.
        dest_prefix: archive dest the catalog's ``file_path`` is rooted at.
        max_size: refuse a candidate whose CLAIMED size exceeds this.
    """
    out: list = []
    for row in rows:
        r = GcRow(
            station=row.get("station"),
            session_type=row.get("session_type"),
            file_category=row["file_category"],
            file_date=_iso(row.get("file_date")),
            file_path=row["file_path"],
            canonical_key=row["canonical_key"],
            content_sha256=row.get("content_sha256"),
            file_size=row.get("file_size"),
        )
        out.append(r)

        if r.file_category == "rinex_org" or "/rinex_org/" in r.file_path:
            r.cls = "report_only"
            r.reason = "rinex_org is out of scope — never touched"
            continue
        if not _under_prefix(r.file_path, dest_prefix):
            r.cls = "report_only"
            r.reason = (
                f"file_path not under --dest-prefix {dest_prefix!r} — existence "
                "cannot be proved (misconfigured --dest-prefix/--read-root?)"
            )
            continue

        r.local_path = _local_archive_path(r.file_path, dest_prefix, read_root)
        if _present(r.local_path):
            r.cls = "present_kept"
            if r.content_sha256 == EMPTY_CONTENT_SHA256:
                r.reason = (
                    "file EXISTS — a LIVE STUB (empty content) on the archive; "
                    "not a phantom, row kept (see archive-rm)"
                )
            else:
                r.reason = "file EXISTS — row describes a real file, kept"
            continue

        if r.content_sha256 != EMPTY_CONTENT_SHA256:
            r.cls = "report_only"
            r.reason = (
                "absent with a REAL digest — relocated or lost; needs a repoint "
                "or re-walk, never a delete"
            )
            continue

        if r.file_size is None:
            r.cls = "oversized_refused"
            r.reason = "absent stub but file_size is NULL — size cannot be bounded"
            continue
        if r.file_size > max_size:
            r.cls = "oversized_refused"
            r.reason = (
                f"absent stub but claimed size {r.file_size} > --max-size "
                f"{max_size} — not a known stub shape"
            )
            continue

        r.cls = "gc_candidate"
        r.reason = f"absent + empty digest + {r.file_size} B claimed ≤ {max_size}"
    return out


# -------------------------------------------------------------------- apply


def _gc_on_host(
    conn,
    candidates: list,
    *,
    storage_location: str,
    max_size: int,
    dry_run: bool,
    log: logging.Logger,
) -> HostGcResult:
    """Delete ``candidates`` on one host, re-checking EACH row right before
    its delete: the row must still exist here, still carry the empty digest,
    still claim a size within ``max_size`` — and the file must still be absent.
    """
    res = HostGcResult()
    pending = 0
    try:
        with conn.cursor() as cur:
            for r in candidates:
                params = _key_params(storage_location, r)
                cur.execute(_key_sql(True, r.session_type), params)
                row = cur.fetchone()
                if row is None:
                    res.absent_on_host.append(r.file_path)
                    continue
                size_now, sha_now = row[0], row[1]
                if sha_now != EMPTY_CONTENT_SHA256:
                    res.digest_changed.append(r.file_path)
                    continue
                # D5: the size cap is re-applied at delete time from THIS
                # host's row, not from the classification-time value.
                if size_now is None or size_now > max_size:
                    res.refused_oversized.append((r.file_path, size_now))
                    continue
                if r.local_path is not None and _present(r.local_path):
                    res.file_appeared.append(r.file_path)
                    continue
                if dry_run:
                    res.would_delete += 1
                    continue
                cur.execute(_key_sql(False, r.session_type), params)
                n = cur.rowcount
                if n != 1:
                    # The natural key is UNIQUE; anything but exactly one row
                    # means the world moved under us — stop this host here.
                    raise RuntimeError(
                        f"DELETE by natural key affected {n} row(s) for "
                        f"{r.file_path} — expected exactly 1"
                    )
                res.deleted += 1
                pending += 1
                audit.info(
                    "archive-catalog-gc DELETED phantom row %s (%s, %s B claimed)",
                    r.file_path,
                    storage_location,
                    size_now,
                )
                if pending >= _COMMIT_EVERY:
                    conn.commit()
                    pending = 0
        if dry_run:
            conn.rollback()  # leave no read transaction open
        else:
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — must report, never raise
        res.error = str(exc)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.error("archive-catalog-gc on host FAILED: %s", exc)
    return res


def gc_hosts_diverged(results: dict) -> bool:
    """True when the per-host outcomes are not identical — a host errored, a
    candidate had no row on some host, or the counts differ."""
    if any(h.error is not None for h in results.values()):
        return True
    counts = [h.counts() for h in results.values()]
    return any(c != counts[0] for c in counts[1:])


def apply_gc(
    hosts: list,
    candidates: list,
    *,
    storage_location: str,
    max_size: int = DEFAULT_MAX_SIZE,
    dry_run: bool = True,
    log: logging.Logger = logger,
) -> dict:
    """Run the delete pass on EVERY host in ``hosts`` (``None`` = the default
    connection). Each host is opened ``single_host=True``: a delete must be
    explicit per host, never an implicit mirror fan-out (which would also make
    the per-host counts lie). Returns ``{host_label: HostGcResult}``.
    """
    from ..db.connection import get_connection

    results: dict = {}
    for host in hosts:
        label = host or "localhost"
        conn = None
        try:
            conn = get_connection(host_override=host, single_host=True)
        except Exception as exc:  # noqa: BLE001
            results[label] = HostGcResult(error=f"connect failed: {exc}")
            log.error("archive-catalog-gc: could not connect to %s: %s", label, exc)
            continue
        try:
            results[label] = _gc_on_host(
                conn,
                candidates,
                storage_location=storage_location,
                max_size=max_size,
                dry_run=dry_run,
                log=log,
            )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        log.info(
            "archive-catalog-gc %s: %d %s",
            label,
            results[label].would_delete if dry_run else results[label].deleted,
            "would delete" if dry_run else "deleted",
        )
    return results


# ------------------------------------------------------------------ orchestr.


def gc_catalog_rows(
    rows: list,
    *,
    hosts: list,
    read_root: str,
    storage_location: str,
    dest_prefix: str,
    total_rows: Optional[int],
    max_size: int = DEFAULT_MAX_SIZE,
    fraction_limit: float = DEFAULT_FRACTION_LIMIT,
    force: bool = False,
    dry_run: bool = True,
    log: logging.Logger = logger,
) -> GcStats:
    """Classify ``rows``, apply the guards, and delete the candidates on every
    host — or refuse loudly.

    Args:
        rows: from :func:`select_stub_rows` (or ``VerifyStats.missing_rows``).
        hosts: catalog hosts from ``resolve_catalog_hosts``.
        read_root / dest_prefix: the stored-path → mount mapping inputs.
        storage_location: ``archive_catalog.storage_location``.
        total_rows: rows at ``storage_location`` (from
            :func:`count_location_rows`); ``None`` disables the fraction guard
            and is itself reported as a problem.
        max_size: size cap, checked at classification and again at delete time.
        fraction_limit: refuse when ``candidates / total_rows`` exceeds it.
        force: override the fraction guard.
        dry_run: classify + preview; delete NOTHING.

    Returns:
        :class:`GcStats`; ``problems`` non-empty means exit non-zero.
    """
    stats = GcStats(
        dry_run=dry_run,
        max_size=max_size,
        fraction_limit=fraction_limit,
        force=force,
        storage_location=storage_location,
        total_rows=total_rows,
    )
    stats.items = classify_rows(
        rows, read_root=read_root, dest_prefix=dest_prefix, max_size=max_size
    )

    if stats.report_only:
        stats.problems.append(
            f"{len(stats.report_only)} row(s) report-only (real digest / "
            "unmappable / rinex_org) — NOT deleted; they need a repoint or re-walk"
        )
    if stats.live_stubs:
        stats.problems.append(
            f"{len(stats.live_stubs)} LIVE stub file(s) present on the archive "
            "(empty content) — not phantoms; rows kept, files are archive-rm's job"
        )
    if stats.oversized_refused:
        stats.problems.append(
            f"{len(stats.oversized_refused)} absent stub row(s) over --max-size "
            f"{max_size} — refused"
        )

    candidates = stats.candidates
    if not candidates:
        return stats

    # The blast-radius brake.
    if total_rows is None:
        stats.problems.append(
            "catalog row count unavailable — fraction guard cannot run; refusing"
        )
        stats.refused_fraction = True
        return stats
    frac = stats.fraction or 0.0
    if frac > fraction_limit and not force:
        stats.refused_fraction = True
        stats.problems.append(
            f"REFUSED: run would delete {len(candidates)} of {total_rows} rows "
            f"({frac:.2%}) — over --fraction-limit {fraction_limit:.2%}. "
            "Verify the selection, then re-run with --force to override."
        )
        log.error("archive-catalog-gc: %s", stats.problems[-1])
        return stats
    if frac > fraction_limit and force:
        log.warning(
            "archive-catalog-gc: fraction guard OVERRIDDEN by --force "
            "(%d of %d rows, %.2f%%)",
            len(candidates),
            total_rows,
            frac * 100,
        )

    stats.hosts = apply_gc(
        hosts,
        candidates,
        storage_location=storage_location,
        max_size=max_size,
        dry_run=dry_run,
        log=log,
    )
    stats.diverged = gc_hosts_diverged(stats.hosts)
    for label, h in stats.hosts.items():
        if h.error:
            stats.problems.append(f"{label}: FAILED — {h.error}; catalogs may DIVERGE")
        if h.absent_on_host:
            stats.problems.append(
                f"{label}: {len(h.absent_on_host)} candidate row(s) have NO row on "
                "this host — catalogs already DIVERGED"
            )
        if h.digest_changed:
            stats.problems.append(
                f"{label}: {len(h.digest_changed)} row(s) no longer carry the "
                "empty digest at delete time — skipped"
            )
        if h.file_appeared:
            stats.problems.append(
                f"{label}: {len(h.file_appeared)} file(s) APPEARED between "
                "classification and delete — rows kept"
            )
        if h.refused_oversized:
            stats.problems.append(
                f"{label}: {len(h.refused_oversized)} row(s) over --max-size at "
                "delete time — refused"
            )
    if stats.diverged:
        stats.problems.append(
            "per-host outcomes differ — catalogs DIVERGED (compare the per-host "
            "counts above)"
        )
    return stats
