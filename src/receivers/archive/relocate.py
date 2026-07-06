"""Guarded relocation of misfiled archive files (rawdata gateway).

Same safety spine as :mod:`receivers.archive.remove` — a wrong invocation must
be safe:

  * **Enumerated explicit (src, dst) pairs only** — no globs, no recursion.
    Both ends must match the strict archive layout
    (:func:`~receivers.archive.remove.validate_archive_relpath`).
  * **Argv boundary:** paths reach the remote shell as ARGV via a QUOTED
    heredoc script — never interpolated into a command string.
  * **Never overwrites:** the destination is re-checked ON THE ARCHIVE at move
    time; an existing destination SKIPs the pair, it is never replaced.
  * **Dry-run is the default** — nothing moves unless ``execute=True``.

DO NOT refactor the remote command to f-string a path into it — the argv
boundary and the server-side no-overwrite check are the whole point.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

from .remove import clean_ssh_stderr, validate_archive_relpath

logger = logging.getLogger("receivers.archive.relocate")
audit = logging.getLogger("receivers.audit")

# Remote script: root/execute as $1..$2, then src dst pairs as $@ (argv-safe).
_REMOTE_SCRIPT = r"""
set -u
root="$1"; shift
execute="$1"; shift
case "$root" in "~/"*) root="$HOME/${root#\~/}";; "~") root="$HOME";; esac
while [ "$#" -ge 2 ]; do
  src="$1"; dst="$2"; shift 2
  f="$root/$src"; t="$root/$dst"
  if [ ! -e "$f" ]; then echo "MISSING|$src|$dst"; continue; fi
  if [ ! -f "$f" ]; then echo "SKIP_NOTFILE|$src|$dst"; continue; fi
  if [ -e "$t" ]; then echo "SKIP_EXISTS|$src|$dst"; continue; fi
  if [ "$execute" = "1" ]; then
    mkdir -p "$(dirname "$t")" || { echo "FAIL|$src|$dst"; continue; }
    if mv -- "$f" "$t"; then echo "MOVED|$src|$dst"; else echo "FAIL|$src|$dst"; fi
  else
    echo "WOULD_MOVE|$src|$dst"
  fi
done
"""


@dataclass
class RelocateResult:
    moved: list = field(default_factory=list)  # (src, dst)
    would_move: list = field(default_factory=list)  # (src, dst)
    dst_exists: list = field(default_factory=list)  # (src, dst) — never replaced
    missing: list = field(default_factory=list)  # (src, dst)
    not_file: list = field(default_factory=list)  # (src, dst)
    failed: list = field(default_factory=list)  # (src, dst)
    invalid: list = field(default_factory=list)  # (src, dst)
    unreported: list = field(default_factory=list)  # (src, dst) — no status came back

    @property
    def ok(self) -> bool:
        return not self.failed and not self.invalid and not self.unreported


def relocate_archive_files(
    pairs: list[tuple[str, str]],
    *,
    ssh_target: str,
    dest_root: str,
    execute: bool = False,
    timeout: int = 600,
) -> RelocateResult:
    """Move (or dry-run) archive files via the rawdata SSH gateway.

    Args:
        pairs: ``(src_rel, dst_rel)`` archive-relative path pairs.
        ssh_target: ``user@host`` gateway (from the archive sync target).
        dest_root: archive root on the gateway (e.g. ``~/gpsdata``).
        execute: actually move; False (default) = dry-run.
    """
    res = RelocateResult()
    valid: list[tuple[str, str]] = []
    for src, dst in pairs:
        if validate_archive_relpath(src) and validate_archive_relpath(dst):
            valid.append((src, dst))
        else:
            res.invalid.append((src, dst))
            logger.error("refusing invalid archive move: %r -> %r", src, dst)
    if not valid:
        return res

    argv = [p for pair in valid for p in pair]
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        ssh_target,
        "bash",
        "-s",
        "--",
        dest_root,
        "1" if execute else "0",
        *argv,
    ]
    proc = subprocess.run(
        cmd, input=_REMOTE_SCRIPT, capture_output=True, text=True, timeout=timeout
    )
    for line in proc.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        status, src, dst = parts
        if status == "MOVED":
            res.moved.append((src, dst))
            audit.info("archive-sort MOVED %s -> %s", src, dst)
        elif status == "WOULD_MOVE":
            res.would_move.append((src, dst))
        elif status == "SKIP_EXISTS":
            res.dst_exists.append((src, dst))
            logger.warning("archive-sort: destination exists, NOT replaced: %s", dst)
        elif status == "MISSING":
            res.missing.append((src, dst))
        elif status == "SKIP_NOTFILE":
            res.not_file.append((src, dst))
        elif status == "FAIL":
            res.failed.append((src, dst))
            logger.error("archive-sort FAILED to move %s -> %s", src, dst)
    # A mid-stream connection reset leaves pairs with NO status line — a
    # partial result must never read as success (bit us live 2026-07-06:
    # 4 of 6 pairs reported, gateway reset, error swallowed).
    reported = {
        tuple(p)
        for bucket in (
            res.moved,
            res.would_move,
            res.dst_exists,
            res.missing,
            res.not_file,
            res.failed,
        )
        for p in bucket
    }
    res.unreported = [p for p in valid if p not in reported]
    if proc.returncode != 0:
        logger.error(
            "ssh gateway error (rc=%s, %d/%d pair(s) unreported): %s",
            proc.returncode,
            len(res.unreported),
            len(valid),
            clean_ssh_stderr(proc.stderr),
        )
    elif res.unreported:
        logger.error(
            "gateway returned no status for %d/%d pair(s)",
            len(res.unreported),
            len(valid),
        )
    return res
