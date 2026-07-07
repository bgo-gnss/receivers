"""Incremental batched push of many files over the network.

One rsync per file (a fresh SSH handshake each) is what tripped epos-portal's
PerSourcePenalties during a full-history dissemination (~1880 transfers). And a
single end-of-job push holds all progress until the very end. This stages files
into a mirror-of-destination tree and flushes them in ONE rsync every
``flush_every`` files (and again at close), so a long job makes steady
incremental progress over a single (ControlMaster-multiplexed) connection.

Generic on purpose — the same shape ``fix-headers`` already used inline
(``--push-batch``): epos-disseminate (scattered convert-cache files → portal),
``receivers rinex --push``, and any verb sending many files can share it. The
optional ``on_flush(refs, stats)`` callback runs the per-batch post-push work
(index, supersede, reindex, backup cleanup) AFTER the batch is durably on the
far side — which is exactly the ordering EPOS needs (never remove a superseded
file before its replacement is on the portal).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


def rsync_tree(
    src_dir: Any,
    dest_base: str,
    *,
    ssh_target: Optional[str] = None,
    dry_run: bool = False,
    timeout: int = 1800,
) -> dict:
    """One rsync of ``src_dir/`` into ``dest_base/`` (creating dirs as needed).

    Returns ``{"transferred": n, "rc": 0, "seconds": s}`` where ``transferred``
    counts files rsync actually sent (already-identical files are skipped, so
    ``transferred`` < staged is normal on a re-run). Raises ``RuntimeError`` on a
    non-zero rsync exit — the caller decides whether that aborts the job.
    """
    src = f"{str(src_dir).rstrip('/')}/"
    base = dest_base.rstrip("/")
    dest = f"{ssh_target}:{base}/" if ssh_target else f"{base}/"
    cmd = ["rsync", "-a", "--itemize-changes", "--mkpath"]
    if dry_run:
        cmd.append("--dry-run")
    cmd += [src, dest]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    secs = time.monotonic() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"rsync rc={proc.returncode}: {proc.stderr.strip()[:400]}")
    # itemize lines start with '<' or '>' for a transfer; skip dir entries.
    transferred = sum(
        1
        for ln in proc.stdout.splitlines()
        if ln[:1] in "<>" and not ln.rstrip().endswith("/")
    )
    return {"transferred": transferred, "rc": proc.returncode, "seconds": secs}


def _link_or_copy(src: Path, dst: Path) -> None:
    """Cheap same-filesystem hardlink into the stage tree, else a copy."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


class BatchPush:
    """Stage files → flush to the network every ``flush_every`` (and at close).

    ``add(local_file, rel_dir, name, ref=None)`` hardlinks-or-copies the file
    into an internal mirror tree ``<stage>/<rel_dir>/<name>``. When the pending
    count reaches ``flush_every`` it flushes: ONE ``rsync_tree`` of the whole
    stage into ``dest_base`` (over ``ssh_target`` if remote), then — only if the
    rsync succeeded — ``on_flush(refs, stats)`` for the durably-pushed batch,
    then the stage is cleared. ``close()`` flushes the remainder.

    ``ref`` is caller-defined and threaded back to ``on_flush`` in add order, so
    the caller can do post-push work (index/supersede/reindex) for exactly the
    files that just landed. Use as a context manager.
    """

    def __init__(
        self,
        dest_base: str,
        *,
        ssh_target: Optional[str] = None,
        flush_every: int = 300,
        dry_run: bool = False,
        on_flush: Optional[Callable[[List[Any], dict], None]] = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.dest_base = str(dest_base)
        self.ssh_target = ssh_target
        self.flush_every = max(1, int(flush_every))
        self.dry_run = dry_run
        self.on_flush = on_flush
        self._log = log or logger
        self._stage = Path(tempfile.mkdtemp(prefix="netpush-"))
        self._refs: List[Any] = []
        self.total_staged = 0
        self.total_flushed = 0
        self.total_transferred = 0

    def add(self, local_file: Any, rel_dir: str, name: str, ref: Any = None) -> None:
        dst_dir = self._stage / str(rel_dir).strip("/")
        dst_dir.mkdir(parents=True, exist_ok=True)
        _link_or_copy(Path(local_file), dst_dir / name)
        self._refs.append(ref)
        self.total_staged += 1
        if len(self._refs) >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if not self._refs:
            return
        refs = self._refs
        try:
            stats = rsync_tree(
                self._stage,
                self.dest_base,
                ssh_target=self.ssh_target,
                dry_run=self.dry_run,
            )
        finally:
            # Always reset the stage — a failed rsync's stage is disposable; the
            # source files still exist and a re-run re-stages them (idempotent).
            self._reset_stage()
            self._refs = []
        self.total_transferred += stats["transferred"]
        self.total_flushed += len(refs)
        self._log.info(
            "batch push: flushed %d file(s) (%d transferred) in %.1fs → %s",
            len(refs),
            stats["transferred"],
            stats["seconds"],
            self.ssh_target or self.dest_base,
        )
        if self.on_flush is not None:
            self.on_flush(refs, stats)

    def _reset_stage(self) -> None:
        shutil.rmtree(self._stage, ignore_errors=True)
        self._stage = Path(tempfile.mkdtemp(prefix="netpush-"))

    def close(self) -> None:
        try:
            self.flush()
        finally:
            shutil.rmtree(self._stage, ignore_errors=True)

    def __enter__(self) -> BatchPush:
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False
