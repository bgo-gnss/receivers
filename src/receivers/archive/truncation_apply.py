"""Apply a truncation repair plan: local copy, archive copy, catalog, ledger, RINEX.

Every write is preceded by a re-check of the facts the plan recorded. A file
that changed since the plan was made is REFUSED, never overwritten.

Order per item, chosen so a failure part-way always leaves a state the next
``plan`` repairs (it converges):

1. Produce the repaired bytes: a complete copy (REPLACE), the receiver (REFETCH),
   or the salvaged prefix (SALVAGE, re-derived and checked against the plan's
   content hash, which is the determinism check).
2. The local copy (rek-d01's rolling window): the old copy is copied into the
   local quarantine, then the new one is renamed into place. There is no window
   in which the file is absent.
3. The archive copy, batched for all items: ONE rsync of ``<rel>.repair-tmp``
   files and ONE ssh call that, per file, checks the tmp file's sha256, checks
   the current archive file is still the one the plan saw, copies it into
   ``~/quarantine/truncation/<run>/`` and renames the tmp file over it. The
   regular push path cannot do this: raw goes out with ``--ignore-existing``
   (``engine.IMMUTABILITY``), so it never replaces a file already there.
4. Read-back through the read-only archive mount, then catalog reindex on every
   catalog host, a ledger row, and RINEX for the period.

The remote script takes paths as ARGV only, never interpolated into a command
string (same rule as :mod:`receivers.archive.remove`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .remove import clean_ssh_stderr, validate_archive_relpath
from .truncation import (
    Action,
    GzipStatus,
    probe_gzip,
    salvage,
)
from .truncation_scan import Item, Plan

logger = logging.getLogger("receivers.archive.truncation")
audit = logging.getLogger("receivers.audit")

TMP_SUFFIX = ".repair-tmp"
ACTIONABLE = {Action.REPLACE.value, Action.SALVAGE.value, Action.REFETCH.value}
RECORD_ONLY = {Action.UNRECOVERABLE.value, Action.UNSUPPORTED.value}

_REMOTE_SWAP = r"""
set -u
root="$1"; shift
qroot="$1"; shift
case "$root" in "~/"*) root="$HOME/${root#\~/}";; esac
case "$qroot" in "~/"*) qroot="$HOME/${qroot#\~/}";; esac
while [ $# -ge 3 ]; do
  rel="$1"; old="$2"; new="$3"; shift 3
  f="$root/$rel"; t="$f.repair-tmp"
  if [ ! -f "$t" ]; then echo "FAIL|$rel|tmp file missing"; continue; fi
  got=$(sha256sum < "$t" | cut -c1-64)
  if [ "$got" != "$new" ]; then rm -f -- "$t"; echo "FAIL|$rel|tmp sha mismatch"; continue; fi
  if [ -f "$f" ] && [ "$(sha256sum < "$f" | cut -c1-64)" = "$new" ]; then
    rm -f -- "$t"; echo "REPLACED|$rel|$new"; continue   # already the new file: idempotent rerun
  fi
  if [ "$old" = "-" ]; then
    if [ -e "$f" ]; then rm -f -- "$t"; echo "REFUSED|$rel|a file appeared since the plan"; continue; fi
  else
    if [ ! -f "$f" ]; then rm -f -- "$t"; echo "REFUSED|$rel|archive file vanished since the plan"; continue; fi
    cur=$(sha256sum < "$f" | cut -c1-64)
    if [ "$cur" != "$old" ]; then rm -f -- "$t"; echo "REFUSED|$rel|archive file changed since the plan"; continue; fi
    q="$qroot/$rel"
    mkdir -p -- "$(dirname -- "$q")" && cp -p -- "$f" "$q" || { rm -f -- "$t"; echo "FAIL|$rel|could not quarantine old copy"; continue; }
  fi
  if mv -f -- "$t" "$f"; then echo "REPLACED|$rel|$new"; else echo "FAIL|$rel|rename failed"; fi
done
"""


def _csha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ApplyConfig:
    ssh_target: str  # user@host of the archive gateway
    dest_root: str  # archive root on the gateway, e.g. ~/gpsdata
    catalog_hosts: list
    local_quarantine: str  # see default_local_quarantine()
    archive_quarantine: str = "~/quarantine/truncation"
    work_dir: Optional[str] = None
    run_id: str = field(
        default_factory=lambda: datetime.now().strftime("%Y%m%dT%H%M%S")
        + "-"
        + uuid.uuid4().hex[:6]
    )
    storage_location: str = "imo_archive"
    rinex: bool = True
    max_items: int = 50
    readback_tries: int = 4
    readback_wait_s: float = 5.0
    ssh_timeout: int = 900


@dataclass
class ItemResult:
    rel: str
    action: str
    outcome: str  # repaired | refused | failed | deferred | not_on_receiver | unreachable | recorded | skipped
    detail: str = ""
    new_content_sha256: Optional[str] = None
    new_size: Optional[int] = None


@dataclass
class ApplyResult:
    results: list = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    @property
    def ok(self) -> bool:
        return not any(r.outcome in ("failed",) for r in self.results)


# ---------------------------------------------------------------------------
# Re-check
# ---------------------------------------------------------------------------


def recheck(item: Item, plan: Plan) -> Optional[str]:
    """None if every copy is still exactly what the plan saw, else why not."""
    roots = {"archive": plan.archive_root, "local": plan.local_root}
    for loc, planned in item.copies.items():
        p = os.path.join(roots[loc], item.rel)
        exists = os.path.isfile(p)
        if planned is None:
            if exists:
                return f"{loc} copy appeared since the plan"
            continue
        if not exists:
            return f"{loc} copy vanished since the plan"
        if os.path.getsize(p) != planned["size"]:
            return f"{loc} copy size changed since the plan"
        if _csha(p) != planned["compressed_sha256"]:
            return f"{loc} copy changed since the plan"
    return None


# ---------------------------------------------------------------------------
# Producing the repaired bytes
# ---------------------------------------------------------------------------


def _replacement_bytes(item: Item, plan: Plan) -> tuple[bytes, str, bool]:
    """(gz bytes, content_sha256, is_partial) for REPLACE / SALVAGE."""
    roots = {"archive": plan.archive_root, "local": plan.local_root}
    src = item.decision["source"]
    path = os.path.join(roots[src], item.rel)
    if item.action == Action.REPLACE.value:
        pr = probe_gzip(path)
        want = item.copies[src]["content_sha256"]
        if pr.status != GzipStatus.COMPLETE or pr.content_sha256 != want:
            raise RuntimeError(
                f"source {src} copy is no longer the complete copy planned"
            )
        return Path(path).read_bytes(), pr.content_sha256, False
    if item.action == Action.SALVAGE.value:
        s = salvage(path)
        planned = (item.salvage or {}).get("content_sha256")
        if s is None or planned is None or s.content_sha256 != planned:
            raise RuntimeError(
                "salvage did not reproduce the planned content hash — refusing"
            )
        return s.data, s.content_sha256, True
    raise ValueError(item.action)


def _refetch_window(item: Item) -> tuple[str, str]:
    d = date.fromisoformat(item.file_date)
    if item.file_hour is None:
        start = datetime(d.year, d.month, d.day)
        end = start + timedelta(days=1)
    else:
        start = datetime(d.year, d.month, d.day, item.file_hour)
        end = start + timedelta(hours=1)
    return start.strftime("%Y%m%d-%H%M"), end.strftime("%Y%m%d-%H%M")


def default_downloader(item: Item) -> str:
    """ONE download attempt through the normal driver. Returns its output.

    The driver pings first and gives up on an unreachable station without
    logging in; nothing here loops around it.
    """
    s, e = _refetch_window(item)
    cmd = [
        "receivers", "download", item.station, "--session", item.session_type,
        "-s", s, "-e", e, "--sync", "--archive",
    ]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    return (proc.stdout or "") + (proc.stderr or "")


def classify_download_output(text: str) -> str:
    low = text.lower()
    if "unreachable" in low or "ping failed" in low:
        return "unreachable"
    if "not found on server" in low:
        return "not_on_receiver"
    return "failed"


def _local_quarantine_copy(path: str, cfg: ApplyConfig, rel: str) -> None:
    q = os.path.join(cfg.local_quarantine, cfg.run_id, rel)
    os.makedirs(os.path.dirname(q), exist_ok=True)
    shutil.copy2(path, q)


def _write_local(path: str, data: bytes, cfg: ApplyConfig, rel: str) -> None:
    """Quarantine the old local copy (if any), then rename the new one in."""
    if os.path.isfile(path):
        _local_quarantine_copy(path, cfg, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".trunc-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _refetch(
    item: Item, plan: Plan, cfg: ApplyConfig, downloader: Callable[[Item], str]
) -> tuple[Optional[bytes], str, str]:
    """Move the local copy aside, fetch once, verify. Returns (bytes|None, outcome, detail)."""
    local = os.path.join(plan.local_root, item.rel)
    q = os.path.join(cfg.local_quarantine, cfg.run_id, item.rel)
    had_local = os.path.isfile(local)
    if had_local:
        os.makedirs(os.path.dirname(q), exist_ok=True)
        shutil.move(local, q)  # the downloader must see the period as missing
    try:
        out = downloader(item)
    except Exception as exc:  # noqa: BLE001
        out = f"download raised: {exc}"
    pr = probe_gzip(local) if os.path.isfile(local) else None
    if pr is not None and pr.status == GzipStatus.COMPLETE:
        return Path(local).read_bytes(), "fetched", ""
    # Not repaired: put the old copy back exactly as it was.
    if pr is not None:
        os.unlink(local)
    if had_local:
        shutil.move(q, local)
    outcome = classify_download_output(out)
    tail = " | ".join(out.strip().splitlines()[-3:])[:300]
    return None, outcome, tail


# ---------------------------------------------------------------------------
# Archive swap (batched)
# ---------------------------------------------------------------------------


@dataclass
class _Swap:
    item: Item
    old_csha: Optional[str]
    new_csha: str
    staged: str


def push_and_swap(swaps: list[_Swap], cfg: ApplyConfig, work_dir: str) -> dict:
    """One rsync + one ssh for the batch. Returns ``{rel: (status, detail)}``."""
    if not swaps:
        return {}
    listing = os.path.join(work_dir, "files-from.txt")
    with open(listing, "w") as fh:
        for s in swaps:
            fh.write(s.item.rel + TMP_SUFFIX + "\n")
    dest = cfg.dest_root.rstrip("/") + "/"
    rsync = [
        "rsync", "-a", "--chmod=D755,F644", "--files-from", listing,
        work_dir + "/", f"{cfg.ssh_target}:{dest}",
    ]  # fmt: skip
    proc = subprocess.run(
        rsync, capture_output=True, text=True, timeout=cfg.ssh_timeout
    )
    if proc.returncode != 0:
        err = clean_ssh_stderr(proc.stderr)
        return {
            s.item.rel: ("FAIL", f"rsync rc={proc.returncode}: {err}") for s in swaps
        }

    args: list[str] = []
    for s in swaps:
        args += [s.item.rel, s.old_csha or "-", s.new_csha]
    qroot = f"{cfg.archive_quarantine.rstrip('/')}/{cfg.run_id}"
    cmd = ["ssh", "-o", "BatchMode=yes", cfg.ssh_target, "bash", "-s", "--",
           cfg.dest_root, qroot, *args]  # fmt: skip
    proc = subprocess.run(
        cmd, input=_REMOTE_SWAP, capture_output=True, text=True, timeout=cfg.ssh_timeout
    )
    out: dict = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) == 3:
            out[parts[1]] = (parts[0], parts[2])
    for s in swaps:
        if s.item.rel not in out:
            out[s.item.rel] = (
                "FAIL",
                f"no answer from gateway: {clean_ssh_stderr(proc.stderr)}",
            )
    return out


def read_back(path: str, want_csha: str, cfg: ApplyConfig) -> bool:
    """The archive copy, read through the read-only mount, is the new file.

    Polls a few times: the NFS attribute cache can serve the old file briefly.
    """
    for i in range(cfg.readback_tries):
        if os.path.isfile(path):
            pr = probe_gzip(path)
            if pr.status == GzipStatus.COMPLETE and pr.compressed_sha256 == want_csha:
                return True
        if i + 1 < cfg.readback_tries:
            time.sleep(cfg.readback_wait_s)
    return False


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

_LEDGER_SQL = """INSERT INTO truncation_repair
  (run_id, rel_path, station, session_type, file_date, file_hour, action, outcome,
   is_partial, source, readable_bytes, kept_bytes, blocks,
   orig_archive_size, orig_archive_csha, orig_local_size, orig_local_csha,
   new_content_sha256, new_size, detail)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""


def ledger_row(
    item: Item, res: ItemResult, run_id: str, salvaged: frozenset = frozenset()
) -> tuple:
    """One ledger row. ``is_partial`` is a property of the CONTENT written:
    true for a salvage, and for any later write of bytes a salvage produced
    (a failed swap leaves a salvaged local copy that the next run REPLACEs
    the archive from — it is still partial)."""
    a = item.copies.get("archive") or {}
    loc = item.copies.get("local") or {}
    # Salvage figures belong to a salvage row only: a refetch/replace item also
    # carries the plan-time salvage estimate, which is not what was written.
    s = (item.salvage or {}) if item.action == Action.SALVAGE.value else {}
    partial = res.outcome == "repaired" and (
        item.action == Action.SALVAGE.value or res.new_content_sha256 in salvaged
    )
    return (
        run_id, item.rel, item.station, item.session_type, item.file_date,
        item.file_hour, item.action, res.outcome, partial, item.decision.get("source"),
        s.get("source_readable_bytes"), s.get("kept_bytes"), s.get("blocks"),
        a.get("size"), a.get("compressed_sha256"), loc.get("size"),
        loc.get("compressed_sha256"), res.new_content_sha256, res.new_size,
        res.detail[:1000],
    )  # fmt: skip


def write_ledger(hosts: list, rows: list[tuple]) -> dict:
    """Write the ledger rows to every catalog host. Returns {host: error|None}."""
    from ..db.connection import get_connection

    status: dict = {}
    for host in hosts:
        label = host or "localhost"
        conn = None
        try:
            conn = get_connection(host_override=host, single_host=True)
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(_LEDGER_SQL, r)
            conn.commit()
            status[label] = None
        except Exception as exc:  # noqa: BLE001
            status[label] = str(exc)
            logger.error("truncation ledger write to %s failed: %s", label, exc)
        finally:
            if conn is not None:
                conn.close()
    return status


def ledger_missing_on(hosts: list) -> list:
    """Catalog hosts on which the truncation_repair table is NOT usable.

    ``apply --yes`` refuses unless this is empty: a salvage whose ledger row
    cannot be written would land in the archive with no partial marker.
    """
    from ..db.connection import get_connection

    missing = []
    for host in hosts:
        conn = None
        try:
            conn = get_connection(host_override=host, single_host=True)
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM truncation_repair LIMIT 1")
        except Exception:  # noqa: BLE001
            missing.append(host or "localhost")
        finally:
            if conn is not None:
                conn.close()
    return missing


def salvaged_hashes(hosts: list) -> frozenset:
    """Content hashes any salvage has produced (attempted or completed)."""
    from ..db.connection import get_connection

    if not hosts:
        return frozenset()
    conn = None
    try:
        conn = get_connection(host_override=hosts[0], single_host=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT new_content_sha256 FROM truncation_repair "
                "WHERE action = 'salvage' AND new_content_sha256 IS NOT NULL"
            )
            return frozenset(r[0].strip() for r in cur.fetchall())
    finally:
        if conn is not None:
            conn.close()


def recorded_rels(hosts: list) -> set:
    """(rel, action) pairs already in the ledger, so record-only items log once."""
    from ..db.connection import get_connection

    if not hosts:
        return set()
    conn = None
    try:
        conn = get_connection(host_override=hosts[0], single_host=True)
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT rel_path, action FROM truncation_repair")
            return {(r[0], r[1]) for r in cur.fetchall()}
    except Exception:  # noqa: BLE001
        return set()
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def _default_reindex(cfg: ApplyConfig, files: list[str], root: str) -> dict:
    from .reindex import reindex_files_multi

    return reindex_files_multi(
        cfg.catalog_hosts,
        files,
        root=root,
        storage_location=cfg.storage_location,
        dest_prefix=cfg.dest_root,
    )


def default_local_quarantine() -> str:
    """Per-user, XDG cache: always writable by whoever runs apply.

    The original default (/mnt/data/gpsdata_quarantine) does not exist on
    rek-d01 and gpsops cannot create directories directly under /mnt/data, so
    the first production run failed on every item (safely, before any write).
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "gps_receivers", "truncation_quarantine")


def preflight_local_quarantine(path: str) -> Optional[str]:
    """None if ``path`` can hold quarantine copies, else why not."""
    try:
        os.makedirs(path, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".probe-", dir=path)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        return f"local quarantine {path} is not writable: {exc}"
    return None


def _default_track(item: Item, size: int) -> bool:
    """Record the repaired local file through the seam every download uses:
    file_tracking -> 'archived' with the new size (retracting a stale
    'missing'/'suspect'), and the local_raw catalog row refreshed."""
    from ..health.file_tracker import FileTracker

    ft = FileTracker()
    try:
        return bool(
            ft.mark_file_archived(
                item.station,
                item.session_type,
                date.fromisoformat(item.file_date),
                item.file_hour,
                item.rel.rsplit("/", 1)[-1],
                size,
            )
        )
    finally:
        close = getattr(ft, "close", None)
        if callable(close):
            close()


def _default_rinex(item: Item) -> None:
    from ..scheduling.backfill import _run_backfill_rinex

    _run_backfill_rinex(
        item.station, item.session_type, date.fromisoformat(item.file_date)
    )


def apply_plan(
    plan: Plan,
    cfg: ApplyConfig,
    *,
    execute: bool = False,
    downloader: Callable[[Item], str] = default_downloader,
    swapper: Callable[[list, ApplyConfig, str], dict] = push_and_swap,
    reindexer: Callable[[ApplyConfig, list, str], dict] = _default_reindex,
    rinexer: Callable[[Item], None] = _default_rinex,
    ledger: Callable[[list, list], dict] = write_ledger,
    already_recorded: Optional[set] = None,
    salvaged: Optional[frozenset] = None,
    ledger_check: Callable[[list], list] = ledger_missing_on,
    tracker: Callable[[Item, int], bool] = _default_track,
) -> ApplyResult:
    """Apply ``plan`` (dry-run unless ``execute``). See the module docstring."""
    result = ApplyResult()
    actionable = [i for i in plan.items if i.action in ACTIONABLE][: cfg.max_items]
    record_only = [i for i in plan.items if i.action in RECORD_ONLY]

    if not execute:
        why_q = preflight_local_quarantine(cfg.local_quarantine)
        for it in actionable:
            why = recheck(it, plan)
            result.results.append(
                ItemResult(
                    it.rel,
                    it.action,
                    "refused" if (why or why_q) else "would_apply",
                    why or why_q or "",
                )
            )
        return result

    if not cfg.catalog_hosts:
        raise RuntimeError("no catalog hosts: refusing to write without a ledger")
    why_q = preflight_local_quarantine(cfg.local_quarantine)
    if why_q:
        raise RuntimeError(why_q + " — refusing before touching any file")
    missing = ledger_check(cfg.catalog_hosts)
    if missing:
        raise RuntimeError(
            "truncation_repair (migration 076) is missing on "
            f"{', '.join(missing)} — refusing: a salvage would land unmarked"
        )
    if already_recorded is None:
        already_recorded = recorded_rels(cfg.catalog_hosts)
    if salvaged is None:
        salvaged = salvaged_hashes(cfg.catalog_hosts)
    # Hashes salvaged IN this run count too (local written, swap fails, ...).
    salvaged = frozenset(salvaged) | frozenset(
        (i.salvage or {}).get("content_sha256")
        for i in actionable
        if i.action == Action.SALVAGE.value and i.salvage
    )
    ledger_rows: list[tuple] = []
    for it in record_only:
        if (it.rel, it.action) in already_recorded:
            continue
        r = ItemResult(it.rel, it.action, "recorded", it.decision["reason"])
        result.results.append(r)
        ledger_rows.append(ledger_row(it, r, cfg.run_id, salvaged))

    own_tmp = cfg.work_dir is None
    work_dir = cfg.work_dir or tempfile.mkdtemp(prefix="truncation-")
    swaps: list[_Swap] = []
    pending: dict[str, ItemResult] = {}
    # Stations already contacted without success this run. ANY non-success
    # (unreachable, not found, refused login, error) ends contact with that
    # station for the run: repeated logins trip the receiver's global ~9 h
    # lockout, so nothing here may loop around a login.
    contacted_failed: set[str] = set()
    try:
        for it in actionable:
            why = recheck(it, plan)
            if why:
                r = ItemResult(it.rel, it.action, "refused", why)
                result.results.append(r)
                ledger_rows.append(ledger_row(it, r, cfg.run_id, salvaged))
                continue
            if not validate_archive_relpath(it.rel):
                r = ItemResult(it.rel, it.action, "refused", "unsafe archive path")
                result.results.append(r)
                continue
            try:
                if it.action == Action.REFETCH.value:
                    if it.station in contacted_failed:
                        r = ItemResult(
                            it.rel,
                            it.action,
                            "deferred",
                            "an earlier contact with this station failed this run",
                        )
                        result.results.append(r)
                        continue
                    contacted_failed.add(it.station)  # cleared only on success
                    data, outcome, detail = _refetch(it, plan, cfg, downloader)
                    if data is None:
                        r = ItemResult(it.rel, it.action, outcome, detail)
                        result.results.append(r)
                        ledger_rows.append(ledger_row(it, r, cfg.run_id, salvaged))
                        continue
                    contacted_failed.discard(it.station)
                    pr_local = probe_gzip(os.path.join(plan.local_root, it.rel))
                    content_sha, partial = pr_local.content_sha256, False
                else:
                    data, content_sha, partial = _replacement_bytes(it, plan)
                    if "local" in it.decision["targets"]:
                        _write_local(
                            os.path.join(plan.local_root, it.rel), data, cfg, it.rel
                        )
            except Exception as exc:  # noqa: BLE001
                r = ItemResult(it.rel, it.action, "failed", str(exc))
                result.results.append(r)
                ledger_rows.append(ledger_row(it, r, cfg.run_id, salvaged))
                continue

            r = ItemResult(it.rel, it.action, "pending", "", content_sha, len(data))
            pending[it.rel] = r
            if "archive" in it.decision["targets"] or it.action == Action.REFETCH.value:
                staged = os.path.join(work_dir, it.rel + TMP_SUFFIX)
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                Path(staged).write_bytes(data)
                arch = it.copies.get("archive")
                swaps.append(
                    _Swap(it, arch["compressed_sha256"] if arch else None,
                          hashlib.sha256(data).hexdigest(), staged)
                )  # fmt: skip
            else:
                r.outcome = "repaired"

        swap_status = swapper(swaps, cfg, work_dir) if swaps else {}
        verified: list[_Swap] = []
        for s in swaps:
            r = pending[s.item.rel]
            st, detail = swap_status.get(s.item.rel, ("FAIL", "no status"))
            if st != "REPLACED":
                r.outcome = "refused" if st == "REFUSED" else "failed"
                r.detail = f"archive: {detail}"
                continue
            if not read_back(
                os.path.join(plan.archive_root, s.item.rel), s.new_csha, cfg
            ):
                r.outcome, r.detail = (
                    "failed",
                    "archive read-back did not show the new file",
                )
                continue
            verified.append(s)

        if verified:
            # Reindex from the staged mirror under its final names.
            for s in verified:
                final = s.staged[: -len(TMP_SUFFIX)]
                os.replace(s.staged, final)
            reidx = reindexer(
                cfg, [s.staged[: -len(TMP_SUFFIX)] for s in verified], work_dir
            )
            bad_hosts = [
                h
                for h, st in reidx.items()
                if st is None or getattr(st, "errors", None)
            ]
            for s in verified:
                r = pending[s.item.rel]
                r.outcome = "repaired"
                if bad_hosts:
                    r.detail = f"catalog reindex incomplete on {', '.join(bad_hosts)}"

        for rel, r in pending.items():
            if r.outcome == "pending":
                r.outcome = "failed"
            result.results.append(r)
            it = next(i for i in actionable if i.rel == rel)
            ledger_rows.append(ledger_row(it, r, cfg.run_id, salvaged))
            if r.outcome == "repaired":
                audit.info(
                    "archive-truncation %s %s (%s)", it.action, rel, r.detail or "ok"
                )
                try:
                    tracked = tracker(it, r.new_size or 0)
                except Exception:  # noqa: BLE001
                    tracked = False
                if not tracked:
                    r.detail = (r.detail + "; file_tracking not updated").lstrip("; ")
                if cfg.rinex:
                    try:
                        rinexer(it)
                    except Exception as exc:  # noqa: BLE001
                        r.detail = (r.detail + f"; RINEX failed: {exc}").lstrip("; ")
    finally:
        if own_tmp:
            shutil.rmtree(work_dir, ignore_errors=True)

    if ledger_rows:
        ledger(cfg.catalog_hosts, ledger_rows)
    return result
