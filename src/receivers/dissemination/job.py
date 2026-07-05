"""APScheduler entry point for the EPOS dissemination sweep (T8).

Module-level callable (APScheduler persists jobs by import path, like the
archive-sync / reconciler jobs). For every EPOS-eligible station it disseminates
a short trailing window of daily files to the active dissemination target and
indexes each pushed file in the EPOS ``rinex_file`` table.

**Double-gated, inert by default:** the scheduler only registers this when
``epos_disseminate.enabled`` is true in scheduler.yaml AND a dissemination target
is ``active`` in sync.yaml. Never raises out (a sweep failure must not crash the
scheduler). The reactive TOS-fingerprint diff + retroactive header re-push (T6)
layer on top of this trailing-window sweep.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("receivers.dissemination.job")


def _index_pushed(epos_conn: Any, target: Any, result: Any) -> None:
    """Best-effort index of a freshly pushed file in the EPOS rinex_file table."""
    if epos_conn is None or not result.artifact_path or not result.relative_path:
        return
    try:
        from .rinex_index import index_rinex_file

        d = result.file_date
        index_rinex_file(
            epos_conn,
            Path(result.artifact_path),
            result.station,
            datetime(d.year, d.month, d.day),
            relative_path=f"/files/{result.relative_path}",
            session=(target.sessions[0] if target.sessions else "15s_24hr"),
            rinex_version=result.rinex_version or 3,
        )
    except Exception as exc:  # noqa: BLE001 - index must never fail the sweep
        logger.warning(
            "index failed for %s %s: %s", result.station, result.file_date, exc
        )


def run_epos_disseminate_job(
    config_path: Optional[str] = None,
    days_back: int = 3,
    target_name: Optional[str] = None,
    no_qc: bool = False,
    *,
    today: Optional[date] = None,
    markers: Optional[list[str]] = None,
    engine_factory: Any = None,
    epos_conn_factory: Any = None,
) -> dict[str, int]:
    """Disseminate the last ``days_back`` days for every EPOS station. Never raises.

    Returns a summary ``{stations, pushed, cached, skipped, failed}``. The injectable
    ``today`` / ``markers`` / ``engine_factory`` / ``epos_conn_factory`` keep the
    sweep testable offline; production uses the real defaults.
    """
    from .config import load_dissemination_config

    summary = {"stations": 0, "pushed": 0, "cached": 0, "skipped": 0, "failed": 0}
    try:
        targets = load_dissemination_config(Path(config_path) if config_path else None)
    except Exception:
        logger.exception("epos-disseminate: failed to load sync.yaml")
        return summary

    if target_name:
        targets = [t for t in targets if t.name == target_name]
    active = [t for t in targets if t.active]
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

    engine = engine_factory(target)

    end = today or date.today()
    dates = [end - timedelta(days=n) for n in range(days_back)]

    epos_conn = None
    if epos_conn_factory is not None:
        try:
            epos_conn = epos_conn_factory()
        except Exception:  # noqa: BLE001 - indexing is best-effort
            epos_conn = None

    try:
        for station in markers:
            summary["stations"] += 1
            for d in dates:
                try:
                    result = engine.run_one(station, d)
                except Exception:
                    logger.exception("epos-disseminate %s %s: run failed", station, d)
                    summary["failed"] += 1
                    continue
                if not result.ok:
                    summary["skipped"] += 1
                    continue
                if result.cached:
                    summary["cached"] += 1
                else:
                    summary["pushed"] += 1
                _index_pushed(epos_conn, target, result)
    finally:
        if epos_conn is not None:
            try:
                epos_conn.close()
            except Exception:  # noqa: BLE001
                pass

    logger.info(
        "epos-disseminate sweep: %d stations, pushed=%d cached=%d skipped=%d failed=%d",
        summary["stations"],
        summary["pushed"],
        summary["cached"],
        summary["skipped"],
        summary["failed"],
    )
    return summary


# --------------------------------------------------------------------------- #
# Reactive sweep (T6) — TOS-fingerprint diff → re-ETL / re-disseminate / stop  #
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
    epos_conn_factory: Any = None,
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

    - NEW / ACTIVATED / CHANGED → re-ETL metadata, re-disseminate the backfill
      window, regenerate + commit the site log.
    - DEACTIVATED → stop-only (keep EPOS rows; just stop pushing).

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

    # EPOS DB connection (for metadata ETL + file indexing); best-effort.
    epos_conn = None
    if epos_conn_factory is not None:
        try:
            epos_conn = epos_conn_factory()
        except Exception:  # noqa: BLE001
            epos_conn = None
    elif actions is None:
        epos_conn = _open_epos_conn()

    try:
        if actions is None:
            actions = _build_reactive_actions(
                target,
                session_provider=session_provider,
                epos_conn=epos_conn,
                engine_factory=engine_factory,
                sitelogs_dir=sitelogs_dir,
                backfill_days=backfill_days,
                today=end,
                publish_m3g=publish_m3g,
            )
        return run_reactive_sync(scan_markers, fingerprint_fn, store, actions)
    finally:
        if epos_conn is not None:
            try:
                epos_conn.close()
            except Exception:  # noqa: BLE001
                pass


def _open_epos_conn() -> Any:
    """Open the EPOS DB connection ([epos_db] / EPOS_DB_* env), or None."""
    try:
        from . import epos_db

        return epos_db.connect()
    except Exception as exc:  # noqa: BLE001 - DB optional; metadata/index steps skip
        logger.warning("epos-reactive: no EPOS DB connection (%s)", exc)
        return None


def _build_reactive_actions(
    target: Any,
    *,
    session_provider: Any,
    epos_conn: Any,
    engine_factory: Any,
    sitelogs_dir: Optional[str],
    backfill_days: int,
    today: date,
    publish_m3g: bool = False,
) -> Any:
    """Wire the production :class:`ReactiveActions` for one target.

    Closures capture the live engine / EPOS DB / site-log repo. Each callback
    raises on a hard failure so the orchestrator keeps the station unadvanced and
    retries it next sweep; best-effort steps (indexing) swallow their own errors.
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

    def refresh_metadata(station: str) -> None:
        if epos_conn is None:
            raise RuntimeError(
                "EPOS DB connection unavailable — cannot refresh metadata"
            )
        from .epos_etl import run_etl

        res = run_etl(epos_conn, markers=[station.upper()])
        if res.errors:
            raise RuntimeError(f"metadata ETL errors: {'; '.join(res.errors)}")

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
        dates = _reactive_date_range(target, today, eff_backfill, floor_from=floor_from)
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
            _index_pushed(epos_conn, target, result)
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
        # stops being disseminated. EPOS rows are intentionally NOT purged.
        logger.info("epos-reactive: %s deactivated — stop-only (rows kept)", station)

    return ReactiveActions(
        refresh_metadata=refresh_metadata,
        disseminate=disseminate,
        regenerate_sitelog=regenerate_sitelog,
        stop=stop,
    )
