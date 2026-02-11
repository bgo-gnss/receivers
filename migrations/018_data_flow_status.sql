-- Migration: 018_data_flow_status.sql
-- Description: View computing time-based data flow status codes per station
-- Date: 2026-02-10
--
-- Joins file_tracking + station_logging_status to produce:
--   status_24h:  combined raw file + RINEX freshness for 15s_24hr
--   status_1hz:  combined raw file + RINEX freshness for 1Hz_1hr
--   rinex_24h_status / rinex_1hz_status: RINEX-specific for detail panel
--
-- Status codes:
--   0  = OK (green)
--   1  = Warning (yellow)
--   2  = Critical (red)
--   -1 = Unknown (grey)
--   -2 = N/A (grey text)
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/018_data_flow_status.sql

BEGIN;

-- ============================================================================
-- STATION DATA FLOW STATUS VIEW
-- ============================================================================

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
)
SELECT
    d.station_id AS sid,
    -- 24h combined status (raw + rinex + logging)
    CASE
      WHEN l.sid IS NULL THEN -1                          -- no logging data
      WHEN NOT COALESCE(l.session_15s_24hr, false) THEN 2 -- not logging -> red
      WHEN r24.file_date >= CURRENT_DATE - 1
           AND COALESCE(x24.file_date >= CURRENT_DATE - 1, true) THEN 0  -- green
      WHEN EXTRACT(HOUR FROM NOW()) >= 12 THEN 2          -- past noon -> red
      WHEN EXTRACT(HOUR FROM NOW()) >= 2 THEN 1           -- past 2am -> yellow
      ELSE 0
    END AS status_24h,
    -- 1Hz combined status
    CASE
      WHEN l.sid IS NULL THEN -1
      WHEN l.session_1hz_1hr IS NULL OR NOT l.session_1hz_1hr THEN -2  -- N/A
      WHEN r1h.latest_ts >= NOW() - INTERVAL '90 minutes' THEN 0      -- green
      WHEN r1h.latest_ts >= NOW() - INTERVAL '6 hours' THEN 1         -- yellow
      WHEN r1h.latest_ts IS NOT NULL THEN 2                            -- red
      ELSE 2
    END AS status_1hz,
    -- RINEX-specific status for detail panel
    CASE
      WHEN x24.file_date >= CURRENT_DATE - 1 THEN 0
      WHEN x24.file_date IS NULL THEN -1
      WHEN EXTRACT(HOUR FROM NOW()) >= 12 THEN 2
      WHEN EXTRACT(HOUR FROM NOW()) >= 2 THEN 1
      ELSE 0
    END AS rinex_24h_status,
    CASE
      WHEN l.session_1hz_1hr IS NULL OR NOT COALESCE(l.session_1hz_1hr, false) THEN -2
      WHEN x1h.latest_ts >= NOW() - INTERVAL '90 minutes' THEN 0
      WHEN x1h.latest_ts IS NULL THEN -1
      WHEN x1h.latest_ts >= NOW() - INTERVAL '6 hours' THEN 1
      ELSE 2
    END AS rinex_1hz_status,
    -- Timestamps for detail panels
    r24.file_date AS raw_24h_date,
    r1h.latest_ts AS raw_1hz_ts,
    x24.file_date AS rinex_24h_date,
    x1h.latest_ts AS rinex_1hz_ts,
    -- Logging flags
    COALESCE(l.session_15s_24hr, false) AS logging_15s,
    COALESCE(l.session_1hz_1hr, false) AS logging_1hz
FROM station_dashboard_data d
LEFT JOIN station_logging_status l ON l.sid = d.station_id
LEFT JOIN latest_raw_24h r24 ON r24.sid = d.station_id
LEFT JOIN latest_raw_1hz r1h ON r1h.sid = d.station_id
LEFT JOIN latest_rinex_24h x24 ON x24.sid = d.station_id
LEFT JOIN latest_rinex_1hz x1h ON x1h.sid = d.station_id;

COMMENT ON VIEW station_data_flow_status IS 'Time-based data flow status codes per station: raw files, RINEX conversion, and logging session health';

COMMIT;
