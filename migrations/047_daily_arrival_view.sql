-- Migration 047: per-station per-day arrival timing for 15s_24hr
--
-- Driving question (2026-05-18): "Why are 43 perfectly good stations not
-- arriving on the live midnight fire — they only land in backfill?"
-- Investigation showed 33 of those 43 hit `stall_timeout` on the live
-- attempt, then completed in backfill. PR #60 should fix that. We need
-- a recurring view to grade the network arrival distribution daily and
-- watch the slow tail compress (or not) over the coming days.
--
-- This view returns one row per (day_utc, sid) for 15s_24hr, with the
-- time-to-complete and an arrival bucket. Grafana panels aggregate from
-- there (histogram by bucket, trend by day, slow-tail table).
--
-- Bucket boundaries match the analysis-time query in
-- ~/.claude/plans/ok-can-we-take-toasty-simon.md so the panel reproduces
-- what we already saw in the conversation.
--
-- Schema: no new tables. Pure view, safe to drop/recreate.

BEGIN;

DROP VIEW IF EXISTS daily_arrival_15s_24hr CASCADE;

CREATE VIEW daily_arrival_15s_24hr AS
WITH per_station_day AS (
    SELECT
        sid,
        DATE(ts AT TIME ZONE 'UTC')                                                AS day_utc,
        MIN(ts)                                                                    AS first_attempt,
        MAX(CASE WHEN outcome = 'completed' THEN ts END)                           AS completed_at,
        SUM(CASE WHEN outcome IN ('failed', 'stall_timeout', 'unreachable')
                 THEN 1 ELSE 0 END)                                                AS n_failed_attempts,
        -- The DOMINANT failure cause on the live (00:01-00:25) window — useful
        -- for triage. If the station's live attempts all stall_timeout, that's
        -- the watchdog story; if they're 'unreachable', it's network.
        (
            SELECT CASE
                WHEN dl2.message ILIKE '%550%'
                  OR dl2.message ILIKE '%not found%'
                  OR dl2.message ILIKE '%no such%'           THEN 'file_not_ready'
                WHEN dl2.message ILIKE '%timed out%'
                  OR dl2.message ILIKE '%watchdog%'
                  OR dl2.outcome = 'stall_timeout'           THEN 'stall_timeout'
                WHEN dl2.message ILIKE '%connection refused%'
                  OR dl2.outcome = 'unreachable'             THEN 'unreachable'
                WHEN dl2.message ILIKE '%size mismatch%'     THEN 'size_mismatch'
                ELSE                                              'other'
            END
            FROM download_log dl2
            WHERE dl2.sid = download_log.sid
              AND dl2.session_type = '15s_24hr'
              AND dl2.outcome IN ('failed', 'stall_timeout', 'unreachable')
              AND DATE(dl2.ts AT TIME ZONE 'UTC') = DATE(download_log.ts AT TIME ZONE 'UTC')
              AND dl2.ts < DATE(download_log.ts AT TIME ZONE 'UTC') + INTERVAL '25 minutes'
            ORDER BY dl2.ts ASC
            LIMIT 1
        )                                                                          AS live_failure_kind
    FROM download_log
    WHERE session_type = '15s_24hr'
      AND ts >= NOW() - INTERVAL '30 days'
    GROUP BY sid, DATE(ts AT TIME ZONE 'UTC')
)
SELECT
    day_utc,
    sid,
    first_attempt,
    completed_at,
    n_failed_attempts,
    live_failure_kind,
    CASE
        WHEN completed_at IS NULL THEN NULL
        ELSE ROUND((EXTRACT(EPOCH FROM (completed_at - first_attempt))/60)::numeric, 1)
    END                                                                            AS time_to_complete_min,
    CASE
        WHEN completed_at IS NULL                                              THEN '99-never_completed'
        WHEN EXTRACT(EPOCH FROM (completed_at - first_attempt))/60 < 1         THEN '00-under_1min'
        WHEN EXTRACT(EPOCH FROM (completed_at - first_attempt))/60 < 5         THEN '01-1-5min'
        WHEN EXTRACT(EPOCH FROM (completed_at - first_attempt))/60 < 15        THEN '02-5-15min'
        WHEN EXTRACT(EPOCH FROM (completed_at - first_attempt))/60 < 30        THEN '03-15-30min'
        WHEN EXTRACT(EPOCH FROM (completed_at - first_attempt))/60 < 60        THEN '04-30-60min'
        WHEN EXTRACT(EPOCH FROM (completed_at - first_attempt))/60 < 180       THEN '05-1-3hr'
        ELSE                                                                        '06-over_3hr'
    END                                                                            AS arrival_bucket
FROM per_station_day;

COMMENT ON VIEW daily_arrival_15s_24hr IS
    '15s_24hr daily arrival distribution: one row per (day_utc, sid). '
    'arrival_bucket buckets time-to-complete from first attempt; '
    'live_failure_kind labels the FIRST observed failure in the live window '
    '(00:00-00:25 UTC) which is most useful for triage. '
    'Driving question: PR #60 watchdog fix should reduce stall_timeout '
    'failures in the live window — track the 33-station tail moving '
    'from 30-60min bucket to <5min bucket over coming days.';

-- Sanity check after applying: counts should match the conversation-time
-- query from 2026-05-18:
--   SELECT arrival_bucket, COUNT(*) FROM daily_arrival_15s_24hr
--   WHERE day_utc = '2026-05-18' GROUP BY 1 ORDER BY 1;
-- Expected: 53 / 20 / 32 / 7 / 43 / 2 / 1 / 17 across the 8 buckets.

COMMIT;
