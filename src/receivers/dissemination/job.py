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

            markers = epos_markers()
        except Exception:
            logger.exception("epos-disseminate: EPOS station lookup failed")
            return summary

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
