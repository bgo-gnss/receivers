"""ArchiveSync — host-level batch delta sweep to the archive gateway.

Per run, for one target:
  1. floor = max(last_success - overlap, cutover)          (watermark, bounded work)
  2. for each tier in target.file_categories (raw, rinex, …):
       delta = local tier files, mtime > floor, session in target.sessions,
               station not excluded                          (find + path filter)
       rsync --files-from to user@host:dest with the tier's IMMUTABILITY flag
               (raw --ignore-existing, rinex --update)
       for each file rsync ACTUALLY transferred: content_sha256 on the local
               file, upsert archive_catalog                  (forward-free index)
  3. advance the watermark only when EVERY tier's rsync succeeded.

Dry-run does steps 1-3 with rsync --dry-run and writes nothing (no catalog, no
watermark). A missing DB connection is tolerated in dry-run (floor falls back to
cutover) so a preview works on a laptop. See design 1781867391.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..utils.content_hash import CorruptArchiveFileError, content_sha256
from .catalog import upsert_catalog_row
from .config import SyncTarget
from .path_parse import parse_archive_path
from .state import compute_floor, get_last_success, record_run

logger = logging.getLogger("receivers.archive.sync")

# raw archive bytes are already compressed — don't waste CPU re-compressing on the
# wire, and never delete on the archive (single-writer, immutable history).
_SKIP_COMPRESS = "gz,Z,bz2,T02,T00,t02,t00,m00,M00,sbf,crx,d"

# Per-tier archive write policy. raw is the permanent record (never overwrite an
# archived raw file). rinex is regenerable from raw, so a newer local copy may
# replace the archived one (--update). Unknown tiers default to immutable (safe).
# Both flags coexist with "never --delete".
IMMUTABILITY = {"raw": "--ignore-existing", "rinex": "--update"}
_DEFAULT_IMMUTABILITY = "--ignore-existing"


def _immutability_flag(category: str) -> str:
    return IMMUTABILITY.get(category, _DEFAULT_IMMUTABILITY)


# Push-on-download (write-through) tuning. The push runs inside a download worker,
# so it must never stall it: a short rsync timeout bounds a hung archive. Concurrency
# is bounded by the caller (a small semaphore) so parallel workers don't trip
# rawdata's sshd MaxStartups — which manifests as "connection reset/closed". We do
# NOT use SSH ControlMaster here: a shared ControlPath under the live scheduler's
# concurrent pushes failed 100% (spurious "Bad owner or permissions on
# ssh_config.d"); plain ssh under a capped concurrency is reliable.
_PUSH_RSYNC_TIMEOUT = 60


@dataclass
class SyncRunResult:
    """Outcome of one ArchiveSync.run()."""

    target: str
    floor: datetime
    delta_count: int = 0
    transferred: int = 0
    cataloged: int = 0
    ok: bool = False
    dry_run: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)


class ArchiveSync:
    """Run one declarative sync target."""

    def __init__(
        self,
        target: SyncTarget,
        conn=None,
        *,
        dry_run: bool = False,
        dest_override: Optional[str] = None,
        force: bool = False,
        cutover_override: Optional[datetime] = None,
        rsync_timeout: int = 1800,
    ) -> None:
        self.target = target
        self.conn = conn
        self.dry_run = dry_run
        self.dest_override = dest_override
        # Run even when the target is inactive — for the manual pre-stage verify
        # before the cutover. The scheduled :45 job never sets this (it only ever
        # runs already-active targets, filtered in run_archive_sync_job).
        self.force = force
        # Override the target's cutover (watermark floor) for a manual run — e.g.
        # a pre-stage verify with a recent cutover so there is a real delta to push.
        self.cutover_override = cutover_override
        self.rsync_timeout = rsync_timeout

    # ---- destination -------------------------------------------------------

    @property
    def remote_dest(self) -> str:
        """rsync destination, honouring ``--dest-override`` (e.g. staging).

        ``user@host:dest`` for a remote target; a bare local path when the
        target has no ``host`` (local staging / byte-verify / tests).
        """
        dest = (
            self.dest_override if self.dest_override is not None else self.target.dest
        )
        if not self.target.host:
            return dest
        return f"{self.target.user}@{self.target.host}:{dest}"

    # ---- delta discovery ---------------------------------------------------

    def find_delta(self, floor: datetime, category: str) -> list[str]:
        """Absolute paths of ``category`` files newer than ``floor``.

        Uses ``find -newermt`` (fast FS-level mtime prune) for the category
        subtree, then filters to the target's sessions and excludes alias
        stations via the authoritative path components.
        """
        root = self.target.source_root
        if not os.path.isdir(root):
            logger.warning("source_root %s does not exist — empty delta", root)
            return []
        cmd = [
            "find",
            root,
            "-type",
            "f",
            "-path",
            f"*/{category}/*",
            "-newermt",
            floor.strftime("%Y-%m-%d %H:%M:%S"),
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.rsync_timeout
        )
        if proc.returncode != 0:
            logger.error("find failed: %s", proc.stderr.strip())
            return []

        out: list[str] = []
        sessions = set(self.target.sessions)
        for line in proc.stdout.splitlines():
            path = line.strip()
            if not path:
                continue
            parsed = parse_archive_path(path, root)
            if parsed is None:
                continue
            if sessions and parsed.session_type not in sessions:
                continue
            if parsed.station in self.target.exclude_stations:
                continue
            out.append(path)
        return out

    # ---- rsync -------------------------------------------------------------

    def _build_rsync_cmd(
        self,
        files_from: str,
        immutability: str,
        extra_args: Optional[list[str]] = None,
    ) -> list[str]:
        cmd = [
            "rsync",
            "-a",  # archive mode (no -z: raw is already compressed)
            immutability,  # per-tier: raw --ignore-existing, rinex --update
            "--partial",  # resume interrupted transfers
            "--itemize-changes",  # so we learn what actually transferred
            f"--skip-compress={_SKIP_COMPRESS}",
            f"--files-from={files_from}",
        ]
        if extra_args:
            # e.g. -e "ssh -o ControlMaster=..." for the push path's SSH multiplexing
            cmd[1:1] = extra_args
        if self.dry_run:
            cmd.append("--dry-run")
        # NB: never --delete. Source root (trailing slash) + remote dest.
        cmd.append(self.target.source_root.rstrip("/") + "/")
        cmd.append(self.remote_dest.rstrip("/") + "/")
        return cmd

    @staticmethod
    def _parse_transferred(stdout: str) -> list[str]:
        """Relative paths rsync reported as transferred (itemize ``<f``/``>f``)."""
        transferred: list[str] = []
        for line in stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            code, name = parts
            # itemize: col0 = direction (<,>), col1 = entry type (f=file).
            if len(code) >= 2 and code[0] in "<>" and code[1] == "f":
                transferred.append(name)
        return transferred

    def _rsync(
        self,
        rel_paths: list[str],
        immutability: str,
        *,
        timeout: Optional[int] = None,
        extra_args: Optional[list[str]] = None,
    ) -> tuple[bool, list[str], str]:
        """Run rsync for the given relative paths; return (ok, transferred, stderr).

        ``timeout`` overrides the default (the push path uses a short one so a
        hung archive can't stall a download worker); ``extra_args`` injects extra
        rsync flags (the push path adds ``-e ssh …`` for SSH multiplexing).
        """
        with tempfile.NamedTemporaryFile("w", suffix=".files-from", delete=False) as fh:
            fh.write("\n".join(rel_paths) + "\n")
            files_from = fh.name
        try:
            cmd = self._build_rsync_cmd(files_from, immutability, extra_args=extra_args)
            logger.info(
                "rsync %d file(s) [%s] -> %s%s",
                len(rel_paths),
                immutability,
                self.remote_dest,
                " [dry-run]" if self.dry_run else "",
            )
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self.rsync_timeout,
            )
            transferred = self._parse_transferred(proc.stdout)
            if proc.returncode != 0:
                logger.error("rsync rc=%d: %s", proc.returncode, proc.stderr.strip())
            return proc.returncode == 0, transferred, proc.stderr.strip()
        finally:
            os.unlink(files_from)

    # ---- catalog -----------------------------------------------------------

    def _catalog_transferred(self, transferred_rel: list[str]) -> tuple[int, list[str]]:
        """Hash + upsert each transferred file. Returns (cataloged, errors).

        A hash failure (corrupt local file) is surfaced as an integrity finding
        but does NOT block the run: rsync already moved the bytes, and re-playing
        the whole delta over one bad file is the unbounded-work trap. The
        integrity-verify pass reconciles the missing/!= row later.

        Commits per file so a crash mid-loop loses at most one row, not the run.
        NOTE: the forward catalog is best-effort, NOT a complete ledger. If the
        process dies after a file transfers but before its row commits, the next
        run's ``--ignore-existing`` rsync skips it (no itemize line) so it is
        never re-cataloged: it ends up on-archive-but-uncataloged. The backward
        archive-vs-catalog reconciliation pass (post-Monday) must find exactly
        that case; the freshness/integrity story must not assume completeness.
        """
        if self.conn is None:
            return 0, []
        root = self.target.source_root
        cataloged = 0
        errors: list[str] = []
        for rel in transferred_rel:
            local = os.path.join(root, rel)
            parsed = parse_archive_path(local, root)
            if parsed is None:
                continue
            try:
                digest = content_sha256(local)
            except CorruptArchiveFileError as exc:
                msg = f"corrupt local file, not cataloged: {rel}: {exc}"
                logger.error(msg)
                errors.append(msg)
                continue
            except OSError as exc:  # vanished between transfer and hash
                errors.append(f"could not read {rel}: {exc}")
                continue
            archive_path = self._archive_path(rel)
            upsert_catalog_row(
                self.conn,
                storage_location=self.target.name,
                station=parsed.station,
                session_type=parsed.session_type,
                file_category=parsed.file_category,
                file_date=parsed.file_date,
                archive_path=archive_path,
                filename=os.path.basename(rel),
                file_size=os.path.getsize(local),
                content_sha256=digest,
            )
            self.conn.commit()  # per-file: a crash loses one row, not the run
            cataloged += 1
        return cataloged, errors

    # ---- push-on-download (low-latency write-through) -----------------------

    def push_explicit(self, local_paths: list[str]) -> tuple[int, list[str]]:
        """Push an explicit list of just-produced local files to the archive NOW.

        The low-latency write-through path (the download / RINEX hooks) that
        complements the ``:45`` watermark sweep: filters to in-scope archive
        files (same session/category/exclude rules as the sweep), splits raw vs
        rinex for the per-tier immutability flag, rsyncs each group with a SHORT
        timeout + SSH multiplexing, then hashes + catalogs exactly what
        transferred. The ``:45`` sweep stays the backstop for anything missed.

        Best-effort by contract: a single bad file or a failed rsync is recorded
        in the returned errors, never raised — the caller (a download worker)
        must not be harmed by the push. Returns ``(pushed, errors)``.
        """
        root = self.target.source_root
        sessions = set(self.target.sessions)
        cats = set(self.target.file_categories)

        groups: dict[str, list[str]] = {}
        for p in local_paths:
            parsed = parse_archive_path(p, root)
            if parsed is None:
                continue
            if sessions and parsed.session_type not in sessions:
                continue
            if cats and parsed.file_category not in cats:
                continue
            if parsed.station in self.target.exclude_stations:
                continue
            if not os.path.isfile(p):
                continue
            groups.setdefault(parsed.file_category, []).append(parsed.relative_path)

        pushed = 0
        errors: list[str] = []
        for category, rels in groups.items():
            ok, transferred, stderr = self._rsync(
                rels,
                _immutability_flag(category),
                timeout=_PUSH_RSYNC_TIMEOUT,
            )
            if not ok:
                errors.append(
                    stderr or f"rsync failed for {len(rels)} {category} file(s)"
                )
                continue
            n, errs = self._catalog_transferred(transferred)
            pushed += n
            errors.extend(errs)
        return pushed, errors

    def _archive_path(self, rel: str) -> str:
        """Where the file lands on the archive (dest base + relative tree)."""
        base = (
            self.dest_override if self.dest_override is not None else self.target.dest
        )
        return f"{base.rstrip('/')}/{rel}"

    # ---- orchestration -----------------------------------------------------

    def run(self) -> SyncRunResult:
        # Snapshot the candidate new watermark BEFORE scanning: any file written
        # during/after the scan is caught next run (its mtime >= scan_start).
        scan_start = datetime.now()
        last = get_last_success(self.conn, self.target.name) if self.conn else None
        cutover = (
            self.cutover_override
            if self.cutover_override is not None
            else self.target.cutover
        )
        floor = compute_floor(last, cutover, self.target.overlap_minutes)

        result = SyncRunResult(
            target=self.target.name, floor=floor, dry_run=self.dry_run
        )

        if not self.target.active and not self.force:
            result.ok = True
            result.message = "target inactive — skipped"
            return result

        # Each tier (raw, rinex, …) is its own find + rsync with its own
        # immutability rule. The watermark is per-target and advances only if
        # EVERY tier's rsync succeeded.
        all_ok = True
        per_cat: list[str] = []
        for category in self.target.file_categories:
            delta = self.find_delta(floor, category)
            result.delta_count += len(delta)
            transferred: list[str] = []
            if delta:
                rel_paths = [os.path.relpath(p, self.target.source_root) for p in delta]
                rsync_ok, transferred, stderr = self._rsync(
                    rel_paths, _immutability_flag(category)
                )
                if stderr:
                    result.errors.append(f"{category}: {stderr}")
                if not rsync_ok:
                    all_ok = False
                if not self.dry_run:
                    cataloged, cat_errors = self._catalog_transferred(transferred)
                    result.cataloged += cataloged
                    result.errors.extend(f"{category}: {e}" for e in cat_errors)
            result.transferred += len(transferred)
            per_cat.append(f"{category} {len(transferred)}/{len(delta)}")

        result.ok = all_ok
        if not self.dry_run and self.conn is not None:
            record_run(
                self.conn,
                self.target.name,
                ran_at=scan_start,
                files=result.transferred,
                ok=all_ok,
                advance_to=scan_start if all_ok else None,
            )

        verb = "would transfer" if self.dry_run else "transferred"
        result.message = f"{verb} " + ", ".join(per_cat)
        return result
