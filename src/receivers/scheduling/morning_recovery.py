"""Morning recovery — daily catch-up download for stations missing yesterday's file.

Driving incident (2026-05-08): the primary 00:01 window and the 00:36
second-chance retry both miss a class of PolaRX5 stations whose receivers
need 30–90 min after midnight UTC before reliably serving the daily 24h
file via FTP. This job reruns the same `_download_station_data_job` for
those stations at 01:30 UTC — late enough that receivers have settled,
early enough to land before GAMIT processing starts at 03:00–04:00 UTC.

Coexists with the 00:36 second-chance retry. They serve different cases:

* 00:36 retry: catches transient-at-midnight failures (passive→active
  mode flip, brief receiver glitches).
* 01:30 morning recovery: catches receivers that needed an hour or more.

Reuses `_download_station_data_job` end-to-end. Confirms each station's
success via `file_tracking` (status IN ('downloaded', 'archived')).

Configured via `morning_recovery:` section in `scheduler.yaml`. Disabled
by default in package; enabled in the operational deployment via
gps-config-data.

See `docs/design/morning-recovery.md` for the full design rationale.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import List, Tuple

logger = logging.getLogger("receivers.scheduler.morning_recovery")


def _query_stations_missing_yesterday(
    session: str,
    target_date: date,
    bypass_known_missing: bool,
) -> List[str]:
    """Return station IDs whose `session` file for `target_date` is missing.

    Excludes:
      - Passive stations (`station_data_flow_status.health_status < 0`)
      - Stations that already have the file in `file_tracking` (downloaded/archived)
      - Stations marked `status='missing'` in `file_tracking` for this
        (sid, session, date) tuple — UNLESS `bypass_known_missing=True`.

    Args:
        session: Session type (e.g. '15s_24hr').
        target_date: Date to check (typically yesterday).
        bypass_known_missing: If True, retry even stations marked 'missing'.

    Returns:
        List of station IDs to retry, sorted alphabetically.
    """
    from ..health.database_factory import DatabaseConnectionFactory

    where_known_missing = (
        ""
        if bypass_known_missing
        else (
            "AND NOT EXISTS ("
            "  SELECT 1 FROM file_tracking ft2"
            "  WHERE ft2.sid = s.sid"
            "    AND ft2.session_type = %(sess)s"
            "    AND ft2.file_date = %(date)s"
            "    AND ft2.status = 'missing'"
            ")"
        )
    )

    sql = f"""
      SELECT s.sid
      FROM station_data_flow_status s
      LEFT JOIN file_tracking ft
             ON ft.sid = s.sid
            AND ft.session_type = %(sess)s
            AND ft.file_date = %(date)s
            AND ft.status IN ('downloaded', 'archived')
      WHERE s.health_status >= 0
        AND s.status_24h = 2
        AND ft.sid IS NULL
        {where_known_missing}
      ORDER BY s.sid
    """
    with DatabaseConnectionFactory.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"sess": session, "date": target_date})
            return [row[0] for row in cur.fetchall()]


def _confirm_recovered(sid: str, session: str, target_date: date) -> bool:
    """True if `file_tracking` shows the file as downloaded/archived after retry."""
    from ..health.database_factory import DatabaseConnectionFactory

    with DatabaseConnectionFactory.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM file_tracking
                WHERE sid = %s AND session_type = %s
                  AND file_date = %s
                  AND status IN ('downloaded', 'archived')
                LIMIT 1
                """,
                (sid, session, target_date),
            )
            return cur.fetchone() is not None


def _retry_station(
    sid: str,
    session: str,
    target_date: date,
    timeout_minutes: int,
    run_rinex: bool,
    production_mode: bool,
) -> Tuple[str, bool]:
    """Run a single-station download attempt, return (sid, recovered)."""
    try:
        # Late import to avoid circulars during module load
        from .bulk_scheduler import _download_station_data_job

        _download_station_data_job(
            sid,
            session,
            production_mode=production_mode,
            timeout_minutes=timeout_minutes,
            run_rinex=run_rinex,
        )
        return sid, _confirm_recovered(sid, session, target_date)
    except Exception as exc:
        logger.warning(f"Morning recovery {session} {sid}: {exc}")
        return sid, False


def _run_morning_recovery_job(
    sessions: List[str],
    days_back: int = 1,
    max_workers: int = 4,
    station_timeout_minutes: int = 8,
    bypass_known_missing: bool = False,
) -> None:
    """APScheduler job: re-download yesterday's missing files for missing stations.

    Args:
        sessions: Session types to recover (initially just ['15s_24hr']).
        days_back: How many days back to target. 1 = yesterday.
        max_workers: Concurrent station downloads (default 4).
        station_timeout_minutes: Hard ceiling per station so a stuck FTP
            doesn't block the whole job.
        bypass_known_missing: If True, retry even stations that file_tracking
            has marked status='missing'.
    """
    target_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).date()

    # Resolve scheduler context to mirror the regular daily job. Falls back to
    # safe defaults when this is invoked outside a running scheduler (e.g. tests).
    production_mode = True
    try:
        from .bulk_scheduler import _scheduler_instance

        if _scheduler_instance is not None:
            production_mode = _scheduler_instance.production_mode
    except Exception:
        pass

    for session in sessions:
        sids = _query_stations_missing_yesterday(
            session, target_date, bypass_known_missing
        )
        if not sids:
            logger.info(
                f"🌅 Morning recovery {session} ({target_date}): "
                "nothing to retry — all stations have yesterday's file"
            )
            continue

        # Resolve run_rinex from the session config when available
        run_rinex = session == "15s_24hr"  # default for the typical case
        try:
            from .bulk_scheduler import _scheduler_instance

            if _scheduler_instance is not None:
                cfg = _scheduler_instance.schedule_configs.get(session)
                if cfg is not None:
                    run_rinex = cfg.rinex
        except Exception:
            pass

        sid_preview = ", ".join(sids[:10]) + (
            f" [+{len(sids) - 10} more]" if len(sids) > 10 else ""
        )
        logger.info(
            f"🌅 Morning recovery {session} ({target_date}): "
            f"{len(sids)} stations queued — {sid_preview}"
        )

        recovered: List[str] = []
        still_failing: List[str] = []
        workers = min(max_workers, len(sids))

        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="morn_rec"
        ) as pool:
            futures = {
                pool.submit(
                    _retry_station,
                    sid,
                    session,
                    target_date,
                    station_timeout_minutes,
                    run_rinex,
                    production_mode,
                ): sid
                for sid in sids
            }
            for fut in as_completed(futures):
                sid, success = fut.result()
                (recovered if success else still_failing).append(sid)

        recovered.sort()
        still_failing.sort()
        recovered_preview = ", ".join(recovered[:15]) + (
            f" [+{len(recovered) - 15} more]" if len(recovered) > 15 else ""
        )
        failing_preview = ", ".join(still_failing[:15]) + (
            f" [+{len(still_failing) - 15} more]" if len(still_failing) > 15 else ""
        )
        logger.info(
            f"🌅 Morning recovery {session} complete: "
            f"{len(recovered)}/{len(sids)} recovered"
            + (f" — {recovered_preview}" if recovered else "")
            + (f" | still failing: {failing_preview}" if still_failing else "")
        )
