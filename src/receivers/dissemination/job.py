"""APScheduler entry point for the EPOS dissemination sweep (T8).

Module-level callable (APScheduler persists jobs by import path, like the
archive-sync / reconciler jobs). For every EPOS-eligible station it disseminates
a short trailing window of daily files to the active dissemination target and
records each pushed file in ``archive_catalog`` (``storage_location='epos_portal'``).

**Double-gated, inert by default:** the scheduler only registers this when
``epos_disseminate.enabled`` is true in scheduler.yaml AND a dissemination target
is ``active`` in sync.yaml. Never raises out (a sweep failure must not crash the
scheduler). The reactive TOS-fingerprint diff + retroactive header re-push (T6)
layer on top of this trailing-window sweep.
"""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("receivers.dissemination.job")


class _SeqProgress:
    """tqdm progress bar for the SEQUENTIAL range run.

    The interactive single-bar analogue of the parallel :class:`ProgressBoard`
    (which reports for N workers). Duck-types the ``ChunkProgress`` surface that
    :func:`_run_station_dates` calls — ``set_total`` / ``advance`` — but the grand
    total is fixed at construction (one bar over ALL dates, across every station/
    year chunk), so the per-chunk ``set_total`` is ignored. Auto-disables when
    stderr is not a TTY (piped / cron / ``--json`` capture) and degrades to a
    no-op when tqdm is unavailable.

    While the bar is live it also routes every ``receivers.*`` log record through
    ``tqdm.write`` (``logging_redirect_tqdm``) so a mid-run WARNING prints cleanly
    ABOVE the bar instead of being concatenated onto the bar's carriage-return line
    (both share stderr — the "newlines are off" garble).
    """

    def __init__(self, total: int, desc: str) -> None:
        self._bar = None
        self._stack = None
        try:
            import logging
            import sys
            from contextlib import ExitStack

            from tqdm import tqdm
            from tqdm.contrib.logging import logging_redirect_tqdm

            self._bar = tqdm(
                total=total,
                desc=desc,
                unit="date",
                ncols=100,
                disable=not sys.stderr.isatty(),
            )
            # The console handler lives on the "receivers" logger (propagate=False),
            # so redirect that logger's stream handlers — not the root's.
            self._stack = ExitStack()
            self._stack.enter_context(
                logging_redirect_tqdm(loggers=[logging.getLogger("receivers")])
            )
        except ImportError:
            pass

    def set_total(self, n: int) -> None:  # grand total fixed in __init__
        pass

    def advance(self, seconds: Optional[float] = None) -> None:
        if self._bar is not None:
            self._bar.update(1)

    def set_postfix(self, **counts: int) -> None:
        if self._bar is not None:
            self._bar.set_postfix(counts)

    def close(self) -> None:
        # Restore normal logging BEFORE removing the bar line.
        if self._stack is not None:
            self._stack.close()
            self._stack = None
        if self._bar is not None:
            self._bar.close()


# Catalog write at push: we upsert
# an ``archive_catalog(storage_location='epos_portal')`` row in gps_health, so the
# one sha256 index carries the portal tier (with BOTH sha256s AND both EPOS md5s).
# The connection is opened lazily and CLOSED at sweep end (``_close_catalog_conn``)
# so no gps_health connection is held between scheduler sweeps. Shared across
# parallel chunks, so guarded by its own lock; commit-per-file means it never
# holds a transaction open (unlike the FormatResolver leak).
_CATALOG_LOCK = threading.Lock()
_catalog_conn: Any = None


def _get_catalog_conn() -> Any:
    """Lazily open + cache the sweep's gps_health connection for the epos_portal
    dual-write. On rek-d01 this is localhost + the best-effort pgdev mirror, so a
    row lands on both DBs. Injectable for tests via ``_catalog_conn_factory``."""
    global _catalog_conn
    if _catalog_conn is None:
        if _catalog_conn_factory is not None:
            _catalog_conn = _catalog_conn_factory()
        else:
            from ..health.database_factory import DatabaseConnectionFactory

            _catalog_conn = DatabaseConnectionFactory.get_connection(
                database="gps_health"
            )
    return _catalog_conn


# Test seam: set to a zero-arg callable returning a fake gps_health connection.
_catalog_conn_factory: Any = None


def _close_catalog_conn() -> None:
    """Close the sweep's catalog connection (call from the job's finally) so no
    gps_health connection is left held between scheduler sweeps."""
    global _catalog_conn
    with _CATALOG_LOCK:
        if _catalog_conn is not None:
            try:
                _catalog_conn.close()
            except Exception:  # noqa: BLE001
                pass
            _catalog_conn = None


def _catalog_epos_push(target: Any, result: Any) -> None:
    """Best-effort dual-write of a pushed EPOS file into ``archive_catalog``
    (``storage_location='epos_portal'``) with both sha256s + both EPOS md5s.

    NEVER raises, so it can't affect the sweep.
    Skips a file not in hand (cached/aged-off) — those are the ``--epos`` backfill
    scope, not the forward hash-at-push path.
    """
    path = getattr(result, "artifact_path", None)
    if not path or getattr(result, "dry_run", False):
        return
    p = Path(path)
    if not p.is_file():
        return
    try:
        from ..archive.catalog import upsert_catalog_row
        from ..utils.content_hash import compressed_sha256, content_sha256
        from .rinex_md5 import rinex_md5s

        md5checksum, md5uncompressed = rinex_md5s(p)
        csha = content_sha256(p)
        zsha = compressed_sha256(p)
        with _CATALOG_LOCK:
            conn = _get_catalog_conn()
            upsert_catalog_row(
                conn,
                storage_location="epos_portal",
                station=result.station,
                session_type=(target.sessions[0] if target.sessions else "15s_24hr"),
                file_category="rinex",
                file_date=result.file_date,
                file_hour=None,
                archive_path=f"/files/{result.relative_path}",
                filename=p.name,
                file_size=p.stat().st_size,
                content_sha256=csha,
                compressed_sha256=zsha,
                md5checksum=md5checksum,
                md5uncompressed=md5uncompressed,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - dual-write must never fail the sweep
        logger.warning(
            "epos_portal catalog dual-write failed for %s %s: %s",
            getattr(result, "station", "?"),
            getattr(result, "file_date", "?"),
            exc,
        )
        _close_catalog_conn()  # drop a possibly-poisoned conn; next file retries


def _record_pushed(target: Any, result: Any) -> None:
    """Record a freshly pushed file in the file index (``archive_catalog``)."""
    _catalog_epos_push(target, result)


def _failure_reason(exc: BaseException) -> str:
    """Short, stable reason string for a run failure (first line, truncated)."""
    text = str(exc).strip()
    msg = text.splitlines()[0] if text else type(exc).__name__
    return msg[:120]


def _emit_failure_digest(summary: dict) -> None:
    """Print a post-run digest: failed/skipped dates grouped by reason.

    One-glance answer to "is this run trustworthy, and if not, which periods
    must I re-disseminate?" — instead of discovering gaps ad hoc after the fact.
    """
    from collections import defaultdict

    failures = summary.get("failures") or []
    skips = summary.get("skips") or []
    if not failures and not skips:
        print("   🟢 digest: no failures, no skips — clean")
        return

    def _group(rows: list) -> None:
        groups = defaultdict(list)
        for station, dstr, reason in rows:
            groups[reason or "unknown"].append((station, dstr))
        for reason, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            dts = sorted(d for _, d in items)
            span = f"{dts[0]}..{dts[-1]}" if len(dts) > 1 else dts[0]
            stations = ",".join(sorted({s for s, _ in items}))
            print(f"      · {reason} — {len(items)}× [{span}] ({stations})")

    print(f"\n   ❌ FAILED {len(failures)} file(s) — re-disseminate these periods:")
    _group(failures)
    print(f"   ⏭️  SKIPPED {len(skips)} file(s) (informational):")
    _group(skips)


def run_epos_disseminate_job(
    config_path: Optional[str] = None,
    days_back: int = 3,
    target_name: Optional[str] = None,
    no_qc: bool = False,
    *,
    today: Optional[date] = None,
    markers: Optional[list[str]] = None,
    dates: Optional[list[date]] = None,
    parallel: Any = None,
    force: bool = False,
    engine_factory: Any = None,
) -> dict:
    """Disseminate a date window for every EPOS station. Never raises.

    Dates: explicit ``dates`` list when given (the range/--dates driver — a
    full-history portal refresh after a re-rinex campaign is exactly this),
    else the ``days_back`` days up to yesterday (the T8 scheduler sweep).

    ``parallel`` (None | "auto" | N): chunk the work into date-disjoint
    (station, year) chunks on the load-aware thread pool; each chunk gets its
    own engine.

    Returns a summary ``{stations, pushed, cached, skipped, failed}``. The
    injectable ``today`` / ``markers`` / ``engine_factory`` keep the sweep testable
    offline.
    """
    from .config import load_dissemination_config

    summary = {
        "stations": 0,
        "pushed": 0,
        "cached": 0,
        "skipped": 0,
        "failed": 0,
        "failures": [],
        "skips": [],
    }
    try:
        targets = load_dissemination_config(Path(config_path) if config_path else None)
    except Exception:
        logger.exception("epos-disseminate: failed to load sync.yaml")
        return summary

    if target_name:
        targets = [t for t in targets if t.name == target_name]
    # force: the CLI range mode's pre-stage path (mirrors the single-date
    # --force) — the scheduler never sets it, so active:false still gates T8.
    active = [t for t in targets if t.active or force]
    if not active:
        logger.info("epos-disseminate: no active dissemination target — nothing to do")
        return summary
    target = active[0]

    # Station set (TOS EPOS filter) and the live QC session provider.
    session_provider = None
    if not no_qc:
        try:
            from .tos_access import make_session_provider

            session_provider = make_session_provider()
        except Exception:
            logger.exception("epos-disseminate: session provider init failed")

    if markers is None:
        try:
            from .tos_access import epos_markers

            discovered = epos_markers()
        except Exception:
            logger.exception("epos-disseminate: EPOS station lookup failed")
            return summary
        # Rollout allowlist (sync.yaml `stations:`): narrow the auto-discovered
        # in_epos set to the stations being onboarded. Empty/absent = all. NOTE:
        # applied to the SWEEP only; the explicit --station path passes markers in
        # and bypasses this. Reactive is intentionally NOT filtered here — its
        # DEACTIVATED detection keys off the raw marker set (TODO: gate the reactive
        # disseminate action, not the marker set, when the allowlist grows there).
        markers = target.select_markers(discovered)
        if target.stations:
            logger.info(
                "epos-disseminate allowlist: %d of %d in_epos stations selected: %s",
                len(markers),
                len(discovered),
                ", ".join(markers) or "(none — check sync.yaml stations: names)",
            )

    if engine_factory is None:
        from .engine import EposDisseminate

        def engine_factory(tgt):  # type: ignore[misc]
            return EposDisseminate(tgt, session_provider=session_provider)

    if dates is None:
        # Ends yesterday: today has no finished day to publish. A stream station
        # (HRIC) does have a partial file for it, which went out under its short
        # name and needed removing the next morning.
        end = (today or date.today()) - timedelta(days=1)
        dates = [end - timedelta(days=n) for n in range(days_back)]
    dates = sorted(set(dates))

    push_ssh = f"{target.user}@{target.host}" if getattr(target, "host", "") else None
    _PUSH_BATCH = 300  # files staged per rsync flush (both products of ~150 dates)

    # Multi-product (format.products): one published file per product per date.
    # Explicit products use the product kwarg; the default single-product shape
    # calls run_one(station, d) unchanged (keeps injected test engines working).
    _products = list(getattr(getattr(target, "format", None), "products", ()) or ())
    _active_products = [p for p in _products if getattr(p, "enabled", True)]

    def _run_station_dates(station: str, chunk_dates: list, progress=None) -> dict:
        """One worker unit: own engine + own batched pusher.

        run_one STAGES each published file into the pusher; the pusher rsyncs a
        batch of ``_PUSH_BATCH`` files at a time (one connection, not one-per-
        file) and calls ``_on_flush`` for the batch once it is DURABLE on the
        portal — that is where its catalog row is written, so the index never
        lists a file that has not landed.
        """
        from ..utils.net_push import BatchPush

        local = {
            "pushed": 0,
            "cached": 0,
            "skipped": 0,
            "failed": 0,
            "failures": [],
            "skips": [],
        }
        engine = engine_factory(target)

        def _on_flush(refs: list, stats: dict) -> None:
            # refs = result objects whose files just landed durably. Catalog each.
            # Best-effort.
            for result in refs:
                if result is None:
                    continue
                _record_pushed(target, result)
            # Drop the convert-cache entries for this batch. Safe HERE and only
            # here: BatchPush calls _on_flush once the files are DURABLE on the
            # portal, so nothing is discarded before its replacement has landed.
            #
            # Per-batch is the right granularity. Per-file would mean one rm per
            # product (the pusher deliberately batches to avoid per-file work),
            # and per-year would let a single station's intermediates reach ~120 GB
            # before anything is reclaimed — measured on the ELDC 2020-2026 run.
            # Chunks are date-disjoint and cache keys are content hashes, so two
            # concurrent chunks can never own the same entry.
            for result in refs:
                for entry in getattr(result, "cache_entries", None) or ():
                    try:
                        shutil.rmtree(entry, ignore_errors=True)
                    except Exception as exc:  # noqa: BLE001 — best-effort
                        logger.debug("cache prune %s: %s", entry, exc)

        pusher = BatchPush(
            dest_base=getattr(engine, "dest_override", None) or target.dest,
            ssh_target=push_ssh,
            flush_every=_PUSH_BATCH,
            dry_run=getattr(engine, "dry_run", False),
            on_flush=_on_flush,
            log=logger,
        )
        engine.pusher = pusher
        if progress is not None:
            progress.set_total(len(chunk_dates))
        try:
            for d in chunk_dates:
                runs = [(p,) for p in _active_products] if _active_products else [()]
                for run_args in runs:
                    try:
                        if run_args:
                            result = engine.run_one(station, d, product=run_args[0])
                        else:
                            result = engine.run_one(station, d)
                    except Exception as exc:  # noqa: BLE001 — logged + counted
                        logger.exception(
                            "epos-disseminate %s %s: run failed", station, d
                        )
                        local["failed"] += 1
                        local["failures"].append(
                            (station, d.isoformat(), _failure_reason(exc))
                        )
                        continue
                    if not result.ok:
                        local["skipped"] += 1
                        local["skips"].append(
                            (
                                station,
                                d.isoformat(),
                                str(getattr(result, "message", ""))[:120],
                            )
                        )
                        logger.info(
                            "epos-disseminate %s %s skipped: %s",
                            station,
                            d,
                            str(getattr(result, "message", ""))[:160],
                        )
                        continue
                    # run_one staged the file into the pusher (which flushes every
                    # _PUSH_BATCH → _on_flush writes the catalog rows). Count here.
                    local["cached" if result.cached else "pushed"] += 1
                if progress is not None:
                    progress.advance()
            pusher.close()  # final flush → _on_flush for the remainder
        finally:
            engine.pusher = None
        return local

    from ..utils.batch_parallel import resolve_workers

    chunks: list = []
    for station in markers:
        by_year: dict[int, list] = {}
        for d in dates:
            by_year.setdefault(d.year, []).append(d)
        for _year, chunk_dates in sorted(by_year.items()):
            chunks.append((station, chunk_dates))
    workers = resolve_workers(parallel, len(chunks), logger)

    summary["stations"] = len(markers)
    if workers > 1:
        from ..utils.batch_parallel import ProgressBoard, run_chunks

        board = ProgressBoard(interval=30)
        handles = {id(c): board.handle(f"{c[0]} {c[1][0].year}") for c in chunks}

        def _chunk_fn(chunk):
            station, chunk_dates = chunk
            h = handles[id(chunk)]
            h.start()
            try:
                res = _run_station_dates(station, chunk_dates, progress=h)
                h.finish(ok=True)
                return res
            except BaseException:
                h.finish(ok=False)
                raise

        with board:
            outcomes = run_chunks(
                chunks, _chunk_fn, workers=workers, logger=logger, load_gate=True
            )
        for oc in outcomes:
            if oc.ok and oc.value is not None:
                for k, v in oc.value.items():
                    summary[k] += v
            else:
                summary["failed"] += len(oc.chunk[1])
    else:
        # Sequential (no --parallel): one tqdm bar over ALL dates — the parallel
        # path already shows progress via ProgressBoard; this closes the gap for
        # the default single-worker range run (e.g. --start full --end <date>).
        seq_total = sum(len(c[1]) for c in chunks)
        seq_desc = markers[0] if len(markers) == 1 else "disseminate"
        seq = _SeqProgress(seq_total, seq_desc)
        try:
            for station, chunk_dates in chunks:
                local = _run_station_dates(station, chunk_dates, progress=seq)
                for k, v in local.items():
                    summary[k] += v
                seq.set_postfix(
                    pushed=summary["pushed"],
                    cached=summary["cached"],
                    skipped=summary["skipped"],
                    failed=summary["failed"],
                )
        finally:
            seq.close()

    _close_catalog_conn()  # don't hold a gps_health conn between sweeps

    logger.info(
        "epos-disseminate sweep: %d stations, pushed=%d cached=%d skipped=%d "
        "failed=%d",
        summary["stations"],
        summary["pushed"],
        summary["cached"],
        summary["skipped"],
        summary["failed"],
    )
    _emit_failure_digest(summary)
    return summary


# --------------------------------------------------------------------------- #
# Reactive sweep (T6) — TOS-fingerprint diff → re-disseminate / stop         #
# --------------------------------------------------------------------------- #


def _reactive_date_range(
    target: Any,
    today: date,
    backfill_days: int,
    *,
    floor_from: Optional[date] = None,
) -> list[date]:
    """Daily dates to (re-)disseminate for a changed/activated station.

    Floor = ``max(target.cutover, today - backfill_days, floor_from)``. The base
    window (cutover / backfill_days) caps how far back we ever go; ``floor_from``
    (from :func:`reactive.affected_floor`) tightens it to the period a CHANGED
    station's metadata change actually affects — e.g. a firmware update only needs
    re-dissemination from that firmware's install date, not the whole year. The
    convert-cache still gates which dates re-render; this just stops the sweep from
    iterating dates that cannot have changed.
    """
    floor = today - timedelta(days=max(0, backfill_days))
    cutover = getattr(target, "cutover", None)
    if cutover is not None:
        cutover_date = cutover.date() if isinstance(cutover, datetime) else cutover
        if cutover_date > floor:
            floor = cutover_date
    if floor_from is not None and floor_from > floor:
        floor = floor_from
    if floor > today:
        return []
    n = (today - floor).days
    return [today - timedelta(days=k) for k in range(n + 1)]


def run_epos_reactive_job(
    config_path: Optional[str] = None,
    target_name: Optional[str] = None,
    backfill_days: int = 365,
    no_qc: bool = False,
    *,
    today: Optional[date] = None,
    state_path: Optional[str] = None,
    markers: Optional[list[str]] = None,
    engine_factory: Any = None,
    sitelogs_dir: Optional[str] = None,
    publish_m3g: bool = False,
    fingerprint_fn: Any = None,
    actions: Any = None,
) -> dict[str, int]:
    """Reactive TOS-fingerprint sweep for EPOS dissemination. Never raises.

    ``publish_m3g`` (default off) opts a CHANGED site log into auto-publishing to
    M3G — the "automatic" path; the standalone ``epos-disseminate --publish-m3g``
    verb is the manual one.

    Scans every currently-eligible EPOS station *plus* every station already in
    the fingerprint store (so a station that dropped out of EPOS is detected as
    DEACTIVATED), classifies each, and dispatches the production actions:

    - NEW / ACTIVATED / CHANGED → re-disseminate the backfill window, regenerate +
      commit the site log (+ M3G publish when ``publish_m3g``).
    - DEACTIVATED → stop-only (portal files kept; just stop pushing).

    Mirrors :func:`run_epos_disseminate_job`: every collaborator is injectable so
    the whole sweep is offline-testable, and a station advances in the store only
    when its full action chain succeeds (transient failures retry next sweep).
    Returns the :func:`receivers.dissemination.reactive.run_reactive_sync` summary.
    """
    from .config import load_dissemination_config
    from .reactive import (
        DEFAULT_STATE_PATH,
        FingerprintStore,
        make_fingerprint_fn,
        run_reactive_sync,
    )

    empty = {
        "new": 0,
        "changed": 0,
        "activated": 0,
        "deactivated": 0,
        "unchanged": 0,
        "failed": 0,
    }

    try:
        targets = load_dissemination_config(Path(config_path) if config_path else None)
    except Exception:
        logger.exception("epos-reactive: failed to load sync.yaml")
        return empty
    if target_name:
        targets = [t for t in targets if t.name == target_name]
    active = [t for t in targets if t.active]
    if not active:
        logger.info("epos-reactive: no active dissemination target — nothing to do")
        return empty
    target = active[0]

    store = FingerprintStore(state_path or DEFAULT_STATE_PATH)
    end = today or date.today()

    # Live QC + fingerprint session provider (shared between detection + acting).
    session_provider = None
    if not no_qc:
        try:
            from .tos_access import make_session_provider

            session_provider = make_session_provider()
        except Exception:
            logger.exception("epos-reactive: session provider init failed")

    # Currently-eligible markers (TOS EPOS filter).
    if markers is None:
        try:
            from .tos_access import epos_markers

            markers = epos_markers()
        except Exception:
            logger.exception("epos-reactive: EPOS station lookup failed")
            return empty

    # Scan = current markers ∪ previously-seen stations, so a station that left
    # the EPOS set is still classified (→ DEACTIVATED) rather than silently lost.
    scan_markers = sorted(set(m.upper() for m in markers) | set(store.load().keys()))

    if fingerprint_fn is None:
        # Detection reads the whole device history (history-wide fingerprint), so a
        # retroactive correction to a closed historical session is caught; the
        # session_provider above stays for QC/acting. Separate TOS read from the
        # components reader, but the net read count per station is unchanged.
        history_fn = None
        components_fn = None
        try:
            from .tos_access import make_components_fn, make_history_fn

            history_fn = make_history_fn()
            components_fn = make_components_fn()
        except Exception:
            logger.exception("epos-reactive: reactive readers init failed")
        fingerprint_fn = make_fingerprint_fn(
            history_fn,
            set(m.upper() for m in markers),
            at=datetime(end.year, end.month, end.day),
            components_fn=components_fn,
        )

    try:
        if actions is None:
            actions = _build_reactive_actions(
                target,
                session_provider=session_provider,
                engine_factory=engine_factory,
                sitelogs_dir=sitelogs_dir,
                backfill_days=backfill_days,
                today=end,
                publish_m3g=publish_m3g,
            )
        return run_reactive_sync(scan_markers, fingerprint_fn, store, actions)
    finally:
        _close_catalog_conn()  # don't hold a gps_health conn between sweeps


def _build_reactive_actions(
    target: Any,
    *,
    session_provider: Any,
    engine_factory: Any,
    sitelogs_dir: Optional[str],
    backfill_days: int,
    today: date,
    publish_m3g: bool = False,
) -> Any:
    """Wire the production :class:`ReactiveActions` for one target.

    Closures capture the live engine / site-log repo. Each callback raises on a
    hard failure so the orchestrator keeps the station unadvanced and retries it
    next sweep; best-effort steps (the catalog write) swallow their own errors.
    ``publish_m3g`` opts the site-log action into auto-publishing a changed log to
    M3G (default off — M3G stays a manual verb until enabled).
    """
    from .engine import EposDisseminate
    from .reactive import ReactiveActions, StationChange
    from .sitelogs import (
        commit_site_log,
        generate_site_log_if_changed,
        resolve_sitelogs_repo,
    )

    if engine_factory is None:

        def engine_factory(tgt):
            return EposDisseminate(tgt, session_provider=session_provider)

    engine = engine_factory(target)
    sitelog_repo = resolve_sitelogs_repo(sitelogs_dir)

    def disseminate(change: StationChange) -> bool:
        from .reactive import CHANGED, affected_floor

        station = change.station
        floor_from = affected_floor(change)
        # A CHANGED with no bound is a historical (closed-period) correction whose
        # affected date is unknown — it could be anywhere in the disseminated
        # history. The default lookback (today − backfill_days) would miss a
        # correction older than that, so extend the window back to cutover (the
        # earliest disseminated date); the convert cache still re-renders only the
        # dates that actually differ. No cutover ⇒ fall back to backfill_days (a
        # documented interim limit — deep-historical reach then needs the full
        # per-period diff). NEW/ACTIVATED keep the plain backfill window.
        eff_backfill = backfill_days
        if change.kind == CHANGED and floor_from is None:
            cutover = getattr(target, "cutover", None)
            cutover_date = cutover.date() if isinstance(cutover, datetime) else cutover
            if cutover_date is not None:
                eff_backfill = max(backfill_days, (today - cutover_date).days)
        # up to yesterday: today's file, where one exists, is partial
        dates = _reactive_date_range(
            target, today - timedelta(days=1), eff_backfill, floor_from=floor_from
        )
        pushed = cached = skipped = 0
        for d in dates:
            result = engine.run_one(station, d)
            if not result.ok:
                skipped += 1
                continue
            if result.cached:
                cached += 1
            else:
                pushed += 1
            _record_pushed(target, result)
        logger.info(
            "epos-reactive %s (%s): %d dates — pushed=%d cached=%d skipped=%d",
            station,
            change.kind,
            len(dates),
            pushed,
            cached,
            skipped,
        )
        # Success = the window ran to completion. Individual QC-blocked / data-less
        # dates are logged and skipped (not a station-level failure) — a stuck
        # historical file must not pin the station's fingerprint forever.
        return True

    def regenerate_sitelog(station: str) -> None:
        # Change-gated: render current TOS, write+commit a new dated log ONLY when
        # the station content changed vs the latest committed one. Unchanged ⇒
        # no-op (no write, no commit, no M3G) — the reactive job already fetched
        # TOS, so this piggybacks a cheap render+hash.
        gate = generate_site_log_if_changed(
            station,
            sitelog_repo,
            country_code=target.format.country_code,
            monument_number=target.format.monument_number,
        )
        if gate is None:
            raise RuntimeError(f"site-log generation produced nothing for {station}")
        if not gate.changed:
            logger.info("epos-reactive: site log unchanged for %s — no-op", station)
            return
        if gate.path is None:
            return
        try:
            commit_site_log(
                sitelog_repo,
                gate.path,
                f"{station.upper()}: site log update (reactive)",
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 - commit is best-effort (no repo ⇒ skip)
            logger.warning(
                "epos-reactive: site-log commit skipped for %s: %s", station, exc
            )
        if publish_m3g and gate.path is not None:
            try:
                from .sitelogs import submit_to_m3g

                submit_to_m3g(
                    station,
                    site_log_path=gate.path,
                    country_code=target.format.country_code,
                    monument_number=target.format.monument_number,
                    dry_run=False,
                )
                logger.info("epos-reactive: M3G published site log for %s", station)
            except Exception as exc:  # noqa: BLE001 - M3G publish is best-effort
                logger.warning(
                    "epos-reactive: M3G publish failed for %s: %s", station, exc
                )

    def stop(station: str) -> None:
        # Stop-only: the station is simply absent from future marker sweeps, so it
        # stops being disseminated. Portal files are intentionally NOT purged.
        logger.info("epos-reactive: %s deactivated — stop-only (files kept)", station)

    return ReactiveActions(
        disseminate=disseminate,
        regenerate_sitelog=regenerate_sitelog,
        stop=stop,
    )
