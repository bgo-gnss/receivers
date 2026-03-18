-- Rollback migration 032: Revert station_data_flow_status to 031 version
--
-- Removes download_status, disk_status columns and reverts combined_status logic.

BEGIN;

DROP VIEW IF EXISTS station_data_flow_status;

-- Recreate from migration 031 (reachability_source) — no download_status/disk_status
CREATE VIEW station_data_flow_status AS
WITH latest_raw_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking
    WHERE session_type = '15s_24hr'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_raw_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour::integer, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking
    WHERE session_type = '1Hz_1hr'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
latest_rinex_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking
    WHERE session_type = '15s_24hr_rinex'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_rinex_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour::integer, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking
    WHERE session_type = '1Hz_1hr_rinex'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
health_streak AS (
    SELECT recent.sid,
           COALESCE(MIN(recent.rn) FILTER (WHERE recent.overall_status <> 'critical'), 7) - 1
               AS consecutive_critical
    FROM (
        SELECT sid, overall_status,
               ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
        FROM block_health_summary
        WHERE ts > NOW() - INTERVAL '1 day'
    ) recent
    WHERE recent.rn <= 6
    GROUP BY recent.sid
),
ever_checked AS (
    SELECT s.sid
    FROM stations s
    WHERE EXISTS (
        SELECT 1 FROM block_health_summary bhs
        WHERE bhs.sid = s.sid LIMIT 1
    )
),
flow_health AS (
    SELECT s.sid,
           h.overall_status, h.status_details,
           sc.is_online, sc.connection_state,
           s.station_status, s.health_check, s.receiver_type
    FROM stations s
    LEFT JOIN LATERAL (
        SELECT overall_status, status_details
        FROM block_health_summary
        WHERE sid = s.sid AND ts > NOW() - INTERVAL '1 day'
        ORDER BY ts DESC LIMIT 1
    ) h ON true
    LEFT JOIN station_connectivity sc ON sc.sid = s.sid
),
base AS (
    SELECT fh.sid,
        CASE
            WHEN fh.station_status IS NOT NULL OR fh.health_check IS NOT NULL THEN -2
            WHEN fh.is_online = false THEN -1
            WHEN fh.overall_status = 'healthy'  THEN 0
            WHEN fh.overall_status = 'warning'  THEN 1
            WHEN fh.overall_status = 'critical'
                 AND fh.connection_state = 'online'
                 AND (fh.status_details IS NULL
                      OR fh.status_details !~* '(Voltage|Temperature|Disk|Satellite)') THEN 1
            WHEN fh.overall_status = 'critical'
                 AND COALESCE(hs.consecutive_critical, 1) >= 2 THEN 2
            WHEN fh.overall_status = 'critical' THEN 1
            ELSE -1
        END AS health_status,
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN fh.receiver_type IS NULL
                 AND NOT COALESCE(l.session_15s_24hr, false)
                 AND r24.file_date IS NULL THEN -2
            WHEN ec.sid IS NULL AND r24.file_date IS NULL THEN -1
            WHEN r24.file_date IS NULL OR r24.file_date < CURRENT_DATE - 1 THEN 2
            WHEN x24.file_date IS NULL OR x24.file_date < r24.file_date   THEN 1
            ELSE 0
        END AS status_24h,
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN NOT COALESCE(l.session_1hz_1hr, false)
                 AND r1h.latest_ts IS NULL THEN -2
            WHEN ec.sid IS NULL AND r1h.latest_ts IS NULL THEN -1
            WHEN r1h.latest_ts >= NOW() - INTERVAL '1.5 hours' THEN 0
            WHEN r1h.latest_ts >= NOW() - INTERVAL '6 hours'   THEN 1
            WHEN r1h.latest_ts IS NOT NULL                      THEN 2
            ELSE 2
        END AS status_1hz,
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN fh.receiver_type IS NULL
                 AND NOT COALESCE(l.session_15s_24hr, false)
                 AND r24.file_date IS NULL THEN -2
            WHEN ec.sid IS NULL AND x24.file_date IS NULL AND r24.file_date IS NULL THEN -1
            WHEN x24.file_date >= CURRENT_DATE - 1 THEN 0
            WHEN x24.file_date IS NULL AND r24.file_date IS NOT NULL THEN 2
            WHEN x24.file_date IS NULL THEN -1
            WHEN EXTRACT(HOUR FROM NOW()) >= 12 THEN 2
            WHEN EXTRACT(HOUR FROM NOW()) >= 2  THEN 1
            ELSE 0
        END AS rinex_24h_status,
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN NOT COALESCE(l.session_1hz_1hr, false)
                 AND r1h.latest_ts IS NULL THEN -2
            WHEN ec.sid IS NULL AND x1h.latest_ts IS NULL AND r1h.latest_ts IS NULL THEN -1
            WHEN x1h.latest_ts >= NOW() - INTERVAL '1.5 hours' THEN 0
            WHEN x1h.latest_ts IS NULL THEN -1
            WHEN x1h.latest_ts >= NOW() - INTERVAL '6 hours'   THEN 1
            ELSE 2
        END AS rinex_1hz_status,
        r24.file_date AS raw_24h_date,
        r1h.latest_ts AS raw_1hz_ts,
        x24.file_date AS rinex_24h_date,
        x1h.latest_ts AS rinex_1hz_ts,
        (fh.receiver_type IS NOT NULL
         OR COALESCE(l.session_15s_24hr, false)
         OR r24.file_date IS NOT NULL) AS logging_15s,
        (COALESCE(l.session_1hz_1hr, false)
         OR r1h.latest_ts IS NOT NULL) AS logging_1hz
    FROM flow_health fh
    LEFT JOIN station_logging_status l  ON l.sid = fh.sid
    LEFT JOIN health_streak hs          ON hs.sid = fh.sid
    LEFT JOIN ever_checked ec           ON ec.sid = fh.sid
    LEFT JOIN latest_raw_24h r24        ON r24.sid = fh.sid
    LEFT JOIN latest_raw_1hz r1h        ON r1h.sid = fh.sid
    LEFT JOIN latest_rinex_24h x24      ON x24.sid = fh.sid
    LEFT JOIN latest_rinex_1hz x1h      ON x1h.sid = fh.sid
)
SELECT sid,
    health_status, status_24h, status_1hz,
    rinex_24h_status, rinex_1hz_status,
    raw_24h_date, raw_1hz_ts,
    rinex_24h_date, rinex_1hz_ts,
    logging_15s, logging_1hz,
    CASE
        WHEN health_status < 0 AND status_24h < 0 THEN -1
        WHEN health_status = 2 AND status_24h = 2 THEN 2
        WHEN GREATEST(
            CASE WHEN health_status < 0 THEN 0 ELSE health_status END,
            CASE WHEN status_24h   < 0 THEN 0 ELSE status_24h   END
        ) = 2 THEN 1
        WHEN GREATEST(
            CASE WHEN health_status < 0 THEN 0 ELSE health_status END,
            CASE WHEN status_24h   < 0 THEN 0 ELSE status_24h   END
        ) = 1 THEN 1
        ELSE 0
    END AS combined_status
FROM base;

DELETE FROM schema_migrations WHERE migration_name = '032_download_warnings';

COMMIT;
