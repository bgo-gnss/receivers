# Morning Recovery Job — Design

**Status**: Proposal (not yet implemented)
**Author**: bgo + claude — 2026-05-08
**Driving incident**: 2026-05-08 — 17 stations missed the 00:01 window for yesterday's 15s_24hr file. Manual `receivers download` at 12:47 recovered 6 of 7 PolaRX5 stations cleanly. Single station (AFST) had genuine network issues. Conclusion: **most "missed" stations are recoverable hours later, not minutes later.**

## Problem statement

The current scheduler timeline for `15s_24hr` (yesterday's file):

| Time (UTC) | Job | Behaviour |
|---|---|---|
| 00:01–00:06 | Primary distribution window | All 173 stations download yesterday's file |
| 00:36 | Second-chance retry | Up to 8 parallel workers retry stations whose primary attempt failed |
| 00:46 | Second-chance done | ~10 min total |
| 02:00, 04:00, ... | Gap detection | Periodic scan, no aggressive retry |

**The gap**: Septentrio receivers occasionally need 30–60+ minutes after midnight UTC before the daily 24h SBF file is reliably served via FTP. The 00:36 second-chance retry catches some, but not all. Empirical data from 2026-05-08:

- 6 PolaRX5 stations (ENTC, FAGD, GOLA, HUSM, SVIE, THOB) failed at 00:01 and at 00:36 — recovered cleanly at 12:47 manual run.
- 1 (AFST) failed at all hours — genuine network/router issue.
- 1 Trimble (DYNG) was offline at 00:01, online by 13:57.

## Goal

Add a daily **morning recovery** pass that catches stations the primary + second-chance windows missed, before the operations team's working day starts (~09:00 UTC). The aim is **>95 % of yesterday's data archived by 09:30 UTC** without manual intervention.

## Non-goals

- Re-fetching files older than yesterday (that's `gap_detection`'s job).
- Recovering stations that are persistently unreachable (e.g. AFST / chronic ping fails) — the operator should investigate the router/receiver instead.
- Replacing the 00:36 second-chance retry. Both jobs should coexist:
  - 00:36 retry: catches transient-at-midnight failures (passive→active mode flip, brief receiver glitches).
  - 09:00 recovery: catches receivers that needed an hour or more.

## Design

### Schedule

Single daily run at **01:30 UTC** (configurable). Rationale:

- **Hard deadline**: GAMIT processing starts at 03:00–04:00 UTC. Recovery must complete before then or the day's missing files don't make it into the daily processing run.
- 01:30 gives Septentrio receivers ~90 min after midnight to finish whatever rotation/lock task they do — empirically (2026-05-08) some stations recovered cleanly via manual run by 12:47 UTC, but most recover within 30–60 min of midnight. 01:30 is the conservative early choice.
- 01:30 avoids the 00:01–00:11 primary window, the 00:36 second-chance retry, the 00:30 archive_reconciler, and the 00:15 status_1hr — the previous half-hour is busy.
- Job target completion: by 02:00 UTC. Hard ceiling 02:30 for the worst case (4 workers, 8 min per station × 8 stations / 4 workers = 16 min nominal).

### What it processes

For session = `15s_24hr` (initially; design extensible to `status_1hr` later):

1. Query the database for stations where:
   - `station_data_flow_status.status_24h = 2` (yesterday's file missing)
   - `station_data_flow_status.health_status >= 0` (not passive)
   - **AND** `file_tracking` has either no row for yesterday's file, or `status != 'missing'` (so we don't waste effort on receivers that genuinely don't have the file — but see "Configurable bypass" below)
2. Order by station_id (deterministic, predictable progress logs).
3. For each station, call the same `_download_station_data_job(sid, '15s_24hr', ...)` that the primary job uses.
4. After all stations attempted, log a summary line:
   ```
   🌅 Morning recovery 15s_24hr: 7/9 recovered (THOB, ENTC, FAGD, GOLA, HUSM, SVIE, OFEL) | still failing: AFST, HVSK
   ```

### YAML configuration

Add a new top-level section to `scheduler.yaml`:

```yaml
morning_recovery:
  enabled: true                # Disable to skip; default false in package defaults
  schedule: "01:30"            # Daily at 01:30 UTC. Single time only.
  sessions:
    - 15s_24hr                 # Sessions to recover. Initially just daily files.
  days_back: 1                 # How many days back to consider (default 1 = yesterday only)
  max_workers: 4               # Parallel station processing. 4 is enough; we're not racing.
  station_timeout_minutes: 8   # Hard ceiling per station so a stuck FTP doesn't block the whole job
  bypass_known_missing: false  # If true, retry even stations marked status='missing' in file_tracking
                               # (useful when the missing-state TTL is wrong; see follow-ups below)
```

### Implementation sketch

In `bulk_scheduler.py`:

```python
def _morning_recovery_job(sessions: List[str], days_back: int,
                          max_workers: int, station_timeout_minutes: int,
                          bypass_known_missing: bool) -> None:
    log = logging.getLogger("receivers.scheduler.morning_recovery")
    target_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).date()

    for session in sessions:
        sids = _query_stations_missing_yesterday(session, target_date,
                                                 bypass_known_missing)
        if not sids:
            log.info(f"🌅 Morning recovery {session}: no stations to retry")
            continue

        log.info(f"🌅 Morning recovery {session}: {len(sids)} stations queued — "
                 f"{', '.join(sids[:10])}{'...' if len(sids) > 10 else ''}")

        recovered, still_failing = [], []
        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="morn_rec") as pool:
            futures = {pool.submit(_download_station_data_job, sid, session,
                                   production_mode=True,
                                   timeout_minutes=station_timeout_minutes,
                                   run_rinex=True): sid for sid in sids}
            for fut in as_completed(futures):
                sid = futures[fut]
                # Confirm via file_tracking — _download_station_data_job logs result
                with DatabaseConnectionFactory.connection() as conn, conn.cursor() as cur:
                    cur.execute("""
                      SELECT 1 FROM file_tracking
                      WHERE sid = %s AND session_type = %s
                        AND file_date = %s
                        AND status IN ('downloaded', 'archived')
                      LIMIT 1
                    """, (sid, session, target_date))
                    (recovered if cur.fetchone() else still_failing).append(sid)

        recovered.sort(); still_failing.sort()
        log.info(f"🌅 Morning recovery {session} complete: "
                 f"{len(recovered)}/{len(sids)} recovered "
                 + (f"({', '.join(recovered)}) " if recovered else "")
                 + (f"| still failing: {', '.join(still_failing)}" if still_failing else ""))


def _schedule_morning_recovery(self) -> None:
    cfg = self.yaml_config.get("morning_recovery", {})
    if not cfg.get("enabled", False):
        return
    base = parse_schedule(cfg.get("schedule", "01:30"))
    self.scheduler.add_job(
        func=_morning_recovery_job,
        trigger=base.trigger_type,
        kwargs={
            "sessions": cfg.get("sessions", ["15s_24hr"]),
            "days_back": cfg.get("days_back", 1),
            "max_workers": cfg.get("max_workers", 4),
            "station_timeout_minutes": cfg.get("station_timeout_minutes", 8),
            "bypass_known_missing": cfg.get("bypass_known_missing", False),
        },
        id="morning_recovery",
        replace_existing=True,
        executor="backfill",  # Same executor pool as second-chance retry
        max_instances=1,
        misfire_grace_time=900,  # 15 min — if scheduler restarted, allow late fire
        **base.trigger_kwargs,
    )
    self.logger.info(f"🌅 Scheduled morning recovery ({base.description}, "
                     f"sessions={cfg.get('sessions')})")
```

Call from `_setup_jobs()` alongside `_schedule_gap_detection()` etc.

### Database query helper

```python
def _query_stations_missing_yesterday(session: str, target_date: date,
                                      bypass_known_missing: bool) -> List[str]:
    """Return SIDs whose 'session' file for target_date is missing.

    Excludes passive stations and (unless bypass) those locked as known-missing.
    """
    where_known_missing = "" if bypass_known_missing else """
        AND NOT EXISTS (
          SELECT 1 FROM file_tracking ft
          WHERE ft.sid = sids.sid
            AND ft.session_type = %(sess)s
            AND ft.file_date = %(date)s
            AND ft.status = 'missing'
        )
    """
    sql = f"""
      WITH sids AS (
        SELECT sid FROM station_data_flow_status
         WHERE health_status >= 0
           AND status_24h = 2          -- "missing yesterday"
      )
      SELECT s.sid FROM sids s
      LEFT JOIN file_tracking ft
             ON ft.sid = s.sid
            AND ft.session_type = %(sess)s
            AND ft.file_date = %(date)s
            AND ft.status IN ('downloaded', 'archived')
      WHERE ft.sid IS NULL
        {where_known_missing}
      ORDER BY s.sid
    """
    with DatabaseConnectionFactory.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, {"sess": session, "date": target_date})
        return [row[0] for row in cur.fetchall()]
```

## Open questions / follow-ups

1. **Should `bypass_known_missing` default true or false?**
   - **False** (default): trust file_tracking. Saves bandwidth but can lock out stations that were transiently 404 (HVSK case from 2026-05-08).
   - **True**: ignore the lock, always retry. Wastes 1 attempt per genuinely-missing station per day.
   - Recommendation: ship as `false` and reduce the `is_file_missing` 7-day TTL to 24h in a separate fix (linked task).

2. **Per-station worker limit vs sequential**.
   - 4 parallel workers gives 4× throughput on the recovery pass (~2 min for 8 stations vs ~8 min sequential).
   - But may exhaust the same network/router if multiple stations share infrastructure.
   - Recommendation: start with 4, monitor for any new failure modes.

3. **Should it run for `status_1hr` too?**
   - status_1hr files are hourly, so "yesterday's missing" is 24 separate files per station.
   - Initial scope: 15s_24hr only. Extend to status_1hr if midnight-rollover misses become a problem there.

4. **Failure escalation**.
   - If a station fails two morning-recovery passes in a row (e.g., AFST), should we open an Icinga alert / pager?
   - Currently the dashboard shows status_24h=2 indefinitely. An alert after N consecutive misses would be more actionable.
   - Out of scope for this design; tracked separately.

5. **Interaction with `gap_detection`**.
   - `gap_detection` runs every 2h with `days_back=7`. It already retries missing files — but on a much larger window.
   - Morning recovery is a focused subset (1 day) at a specific time.
   - No conflict: morning recovery hits :30 of an hour where `gap_detection` is also expected to run; both use the `backfill` executor with `max_instances=1`, so APScheduler will queue them — fine.

## Linked work

- Depends on: nothing — current code paths are sufficient.
- Closely-related but separate fixes:
  - Reduce `is_file_missing` TTL from 7d to 24h (or graduate by failure age).
  - Investigate progress-bar truncation on AFST (Task 15 — not blocking).
  - Add Icinga alert for "missing for N consecutive days".

## Implementation effort estimate

- Small — ~150 lines in `bulk_scheduler.py`, ~40 lines in tests, ~15 lines in `scheduler.yaml` defaults.
- Disabled by default in package defaults, enabled via gps-config-data deployment.
- Reuses existing `_download_station_data_job` end-to-end so no new download paths to test.

## Decision

[ ] Approve — implement as PR
[ ] Modify — adjust before implementing
[ ] Reject — different approach preferred
