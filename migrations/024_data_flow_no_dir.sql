-- Migration: 024_data_flow_no_dir.sql
-- Description: Grey data status for stations never health-checked (no data dir)
-- Date: 2026-02-17
--
-- Stations that have receiver_type set but have never been health-checked
-- (no records in block_health_summary) and have no file_tracking records
-- now show grey (-1) in data columns instead of red (2).
-- This prevents false "missing data" alerts for stations that haven't been
-- set up for data collection yet.
--
-- Changes:
--   - Adds ever_checked CTE (SELECT DISTINCT sid FROM block_health_summary)
--   - status_24h, status_1hz, rinex_24h_status, rinex_1hz_status:
--     returns -1 (grey) instead of 2 (red) when ec.sid IS NULL + no files

BEGIN;

-- Recreate the view with the ever_checked logic
-- (full view replacement — same as 018 with the addition)

CREATE OR REPLACE VIEW station_data_flow_status AS
WITH latest_raw_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking
    WHERE session_type = '15s_24hr' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_raw_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking
    WHERE session_type = '1Hz_1hr' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
latest_rinex_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking
    WHERE session_type = '15s_24hr_rinex' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_rinex_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking
    WHERE session_type = '1Hz_1hr_rinex' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
health_streak AS (
    SELECT sid,
           COALESCE(
               MIN(rn) FILTER (WHERE overall_status != 'critical'), 7
           ) - 1 AS consecutive_critical
    FROM (
        SELECT sid, overall_status,
               ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
        FROM block_health_summary
    ) recent
    WHERE rn <= 6
    GROUP BY sid
),
ever_checked AS (
    SELECT DISTINCT sid FROM block_health_summary
),
base AS (
    SELECT
        d.station_id AS sid,

        CASE
          WHEN d.station_status IS NOT NULL OR d.health_check IS NOT NULL THEN -2
          WHEN d.is_online = false THEN -1
          WHEN d.overall_status = 'healthy' THEN 0
          WHEN d.overall_status = 'warning' THEN 1
          WHEN d.overall_status = 'critical'
               AND d.connection_state = 'online'
               AND (d.status_details IS NULL
                    OR d.status_details !~* '(Voltage|Temperature|Disk|Satellite)')
               THEN 1
          WHEN d.overall_status = 'critical'
               AND COALESCE(hs.consecutive_critical, 1) >= 2
               THEN 2
          WHEN d.overall_status = 'critical' THEN 1
          ELSE -1
        END AS health_status,

        CASE
          WHEN d.station_status IS NOT NULL THEN -2
          WHEN d.receiver_type IS NULL
               AND NOT COALESCE(l.session_15s_24hr, false)
               AND r24.file_date IS NULL THEN -2
          WHEN ec.sid IS NULL AND r24.file_date IS NULL THEN -1
          WHEN r24.file_date IS NULL OR r24.file_date < CURRENT_DATE - 1
               THEN 2
          WHEN x24.file_date IS NULL OR x24.file_date < r24.file_date
               THEN 1
          ELSE 0
        END AS status_24h,

        CASE
          WHEN d.station_status IS NOT NULL THEN -2
          WHEN NOT COALESCE(l.session_1hz_1hr, false)
               AND r1h.latest_ts IS NULL THEN -2
          WHEN ec.sid IS NULL AND r1h.latest_ts IS NULL THEN -1
          WHEN r1h.latest_ts >= NOW() - INTERVAL '90 minutes' THEN 0
          WHEN r1h.latest_ts >= NOW() - INTERVAL '6 hours' THEN 1
          WHEN r1h.latest_ts IS NOT NULL THEN 2
          ELSE 2
        END AS status_1hz,

        CASE
          WHEN d.station_status IS NOT NULL THEN -2
          WHEN d.receiver_type IS NULL
               AND NOT COALESCE(l.session_15s_24hr, false)
               AND r24.file_date IS NULL THEN -2
          WHEN ec.sid IS NULL AND x24.file_date IS NULL AND r24.file_date IS NULL THEN -1
          WHEN x24.file_date >= CURRENT_DATE - 1 THEN 0
          WHEN x24.file_date IS NULL AND r24.file_date IS NOT NULL THEN 2
          WHEN x24.file_date IS NULL THEN -1
          WHEN EXTRACT(HOUR FROM NOW()) >= 12 THEN 2
          WHEN EXTRACT(HOUR FROM NOW()) >= 2 THEN 1
          ELSE 0
        END AS rinex_24h_status,

        CASE
          WHEN d.station_status IS NOT NULL THEN -2
          WHEN NOT COALESCE(l.session_1hz_1hr, false)
               AND r1h.latest_ts IS NULL THEN -2
          WHEN ec.sid IS NULL AND x1h.latest_ts IS NULL AND r1h.latest_ts IS NULL THEN -1
          WHEN x1h.latest_ts >= NOW() - INTERVAL '90 minutes' THEN 0
          WHEN x1h.latest_ts IS NULL THEN -1
          WHEN x1h.latest_ts >= NOW() - INTERVAL '6 hours' THEN 1
          ELSE 2
        END AS rinex_1hz_status,

        r24.file_date AS raw_24h_date,
        r1h.latest_ts AS raw_1hz_ts,
        x24.file_date AS rinex_24h_date,
        x1h.latest_ts AS rinex_1hz_ts,

        d.receiver_type IS NOT NULL OR COALESCE(l.session_15s_24hr, false) OR r24.file_date IS NOT NULL AS logging_15s,
        COALESCE(l.session_1hz_1hr, false) OR r1h.latest_ts IS NOT NULL AS logging_1hz

    FROM station_dashboard_data d
    LEFT JOIN station_logging_status l ON l.sid = d.station_id
    LEFT JOIN health_streak hs ON hs.sid = d.station_id
    LEFT JOIN ever_checked ec ON ec.sid = d.station_id
    LEFT JOIN latest_raw_24h r24 ON r24.sid = d.station_id
    LEFT JOIN latest_raw_1hz r1h ON r1h.sid = d.station_id
    LEFT JOIN latest_rinex_24h x24 ON x24.sid = d.station_id
    LEFT JOIN latest_rinex_1hz x1h ON x1h.sid = d.station_id
)
SELECT base.*,
    CASE
      WHEN base.health_status < 0 AND base.status_24h < 0 THEN -1
      WHEN base.health_status = 2 AND base.status_24h = 2 THEN 2
      WHEN GREATEST(
             CASE WHEN base.health_status < 0 THEN 0 ELSE base.health_status END,
             CASE WHEN base.status_24h < 0 THEN 0 ELSE base.status_24h END
           ) = 2 THEN 1
      WHEN GREATEST(
             CASE WHEN base.health_status < 0 THEN 0 ELSE base.health_status END,
             CASE WHEN base.status_24h < 0 THEN 0 ELSE base.status_24h END
           ) = 1 THEN 1
      ELSE 0
    END AS combined_status
FROM base;

COMMENT ON VIEW station_data_flow_status IS 'Station health, data flow, and combined status codes for dashboards and maps';

COMMIT;
