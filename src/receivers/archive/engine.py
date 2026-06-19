"""ArchiveSync — host-level batch delta sweep to the archive gateway.

Per run, for one target:
  1. floor = max(last_success - overlap, cutover)          (watermark, bounded work)
  2. delta = local files under source_root, category match, mtime > floor,
     session in target.sessions, station not excluded       (find + path filter)
  3. rsync --files-from --ignore-existing (raw is immutable) to user@host:dest
  4. for each file rsync ACTUALLY transferred: content_sha256 on the local file,
     upsert archive_catalog                                  (forward-free index)
  5. advance the watermark only when rsync succeeded.

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
_SKIP_COMPRESS = "gz,Z,bz2,T02,T00,t02,t00,m00,M00,sbf"


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
        rsync_timeout: int = 1800,
    ) -> None:
        self.target = target
        self.conn = conn
        self.dry_run = dry_run
        self.dest_override = dest_override
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

    def find_delta(self, floor: datetime) -> list[str]:
        """Absolute paths of candidate files newer than ``floor``.

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
            f"*/{self.target.file_category}/*",
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

    def _build_rsync_cmd(self, files_from: str) -> list[str]:
        cmd = [
            "rsync",
            "-a",  # archive mode (no -z: raw is already compressed)
            "--ignore-existing",  # raw immutability: never overwrite the archive
            "--partial",  # resume interrupted transfers
            "--itemize-changes",  # so we learn what actually transferred
            f"--skip-compress={_SKIP_COMPRESS}",
            f"--files-from={files_from}",
        ]
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

    def _rsync(self, rel_paths: list[str]) -> tuple[bool, list[str], str]:
        """Run rsync for the given relative paths; return (ok, transferred, stderr)."""
        with tempfile.NamedTemporaryFile("w", suffix=".files-from", delete=False) as fh:
            fh.write("\n".join(rel_paths) + "\n")
            files_from = fh.name
        try:
            cmd = self._build_rsync_cmd(files_from)
            logger.info(
                "rsync %d file(s) -> %s%s",
                len(rel_paths),
                self.remote_dest,
                " [dry-run]" if self.dry_run else "",
            )
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.rsync_timeout
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
        floor = compute_floor(last, self.target.cutover, self.target.overlap_minutes)

        result = SyncRunResult(
            target=self.target.name, floor=floor, dry_run=self.dry_run
        )

        if not self.target.active:
            result.ok = True
            result.message = "target inactive — skipped"
            return result

        delta = self.find_delta(floor)
        result.delta_count = len(delta)

        if not delta:
            result.ok = True
            result.message = "no files newer than watermark"
            if not self.dry_run and self.conn is not None:
                record_run(
                    self.conn,
                    self.target.name,
                    ran_at=scan_start,
                    files=0,
                    ok=True,
                    advance_to=scan_start,
                )
            return result

        rel_paths = [os.path.relpath(p, self.target.source_root) for p in delta]
        rsync_ok, transferred, stderr = self._rsync(rel_paths)
        result.transferred = len(transferred)
        if stderr:
            result.errors.append(stderr)

        if not self.dry_run:
            cataloged, cat_errors = self._catalog_transferred(transferred)
            result.cataloged = cataloged
            result.errors.extend(cat_errors)
            if self.conn is not None:
                record_run(
                    self.conn,
                    self.target.name,
                    ran_at=scan_start,
                    files=len(transferred),
                    ok=rsync_ok,
                    advance_to=scan_start if rsync_ok else None,
                )

        result.ok = rsync_ok
        result.message = (
            f"{'would transfer' if self.dry_run else 'transferred'} "
            f"{result.transferred}/{result.delta_count} file(s)"
        )
        return result
