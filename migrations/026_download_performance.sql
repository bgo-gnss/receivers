-- Migration: 026_download_performance.sql
-- Description: Per-station stall timeout override + download performance logging
-- Date: 2026-02-21
--
-- Adds:
--   1. stations.stall_timeout_override — per-station stall timeout (NULL = receiver-type default)
--   2. download_log table — one row per download attempt (success or failure)
--   3. station_download_summary view — aggregates over rolling 5-day window
--   4. Updates station_dashboard_data view to include download performance columns
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/026_download_performance.sql

BEGIN;

-- ============================================================================
-- 1. PER-STATION STALL TIMEOUT OVERRIDE
-- ============================================================================

ALTER TABLE stations ADD COLUMN IF NOT EXISTS stall_timeout_override INTEGER;
COMMENT ON COLUMN stations.stall_timeout_override IS
  'Per-station stall timeout in seconds. NULL = use receiver-type default from receivers.cfg';

-- ============================================================================
-- 2. DOWNLOAD LOG TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS download_log (
    id SERIAL PRIMARY KEY,
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    session_type VARCHAR(20) NOT NULL,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    outcome VARCHAR(20) NOT NULL,        -- 'completed', 'stall_timeout', 'failed', 'unreachable'
    file_date DATE,
    filename VARCHAR(100),
    duration_seconds REAL,               -- wall-clock time for this attempt
    bytes_downloaded BIGINT,             -- actual bytes received
    file_size BIGINT,                    -- expected/total file size
    avg_speed_bps REAL,                  -- bytes/second average
    stall_timeout_used INTEGER,          -- effective timeout value in seconds
    attempt INTEGER DEFAULT 1,           -- which retry attempt (1-based)
    message TEXT                          -- error message or context
);

CREATE INDEX IF NOT EXISTS idx_download_log_sid_ts ON download_log(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_download_log_outcome ON download_log(outcome, ts DESC);
-- Partial index on recent rows — use ts column directly for pruning (planner uses constraint exclusion)
CREATE INDEX IF NOT EXISTS idx_download_log_ts ON download_log(ts DESC);

COMMENT ON TABLE download_log IS 'One row per download attempt (success or failure) for performance monitoring';

-- ============================================================================
-- 3. STATION DOWNLOAD SUMMARY VIEW (rolling 5-day window)
-- ============================================================================

CREATE OR REPLACE VIEW station_download_summary AS
SELECT
    sid,
    COUNT(*) AS total_attempts,
    COUNT(*) FILTER (WHERE outcome = 'completed') AS completions,
    COUNT(*) FILTER (WHERE outcome = 'stall_timeout') AS stalls,
    COUNT(*) FILTER (WHERE outcome IN ('failed', 'unreachable')) AS failures,
    ROUND(AVG(avg_speed_bps) FILTER (WHERE outcome = 'completed'))::BIGINT AS avg_speed_bps,
    ROUND(AVG(duration_seconds) FILTER (WHERE outcome = 'completed')::NUMERIC, 1) AS avg_duration_s,
    ROUND(AVG(duration_seconds) FILTER (WHERE outcome = 'stall_timeout')::NUMERIC, 1) AS avg_stall_duration_s,
    MAX(ts) AS last_download_at,
    MAX(ts) FILTER (WHERE outcome = 'stall_timeout') AS last_stall_at
FROM download_log
WHERE ts > NOW() - INTERVAL '5 days'
GROUP BY sid;

COMMENT ON VIEW station_download_summary IS 'Per-station download performance summary over rolling 5-day window';

-- ============================================================================
-- 4. UPDATE STATION DASHBOARD DATA VIEW
-- ============================================================================
-- Drop and recreate: new columns are added before the computed CASE columns,
-- which changes column order (CREATE OR REPLACE VIEW can only add at the end).
-- CASCADE drops station_data_flow_status which is recreated below.

DROP VIEW IF EXISTS station_dashboard_data CASCADE;

CREATE VIEW station_dashboard_data AS
WITH health_ranked AS (
    SELECT sid, ts, overall_status, status_details,
           ftp_open, http_open, control_open,
           ftp_port, http_port, control_port,
           row_number() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_health_summary
),
health_debounced_ports AS (
    SELECT sid,
        bool_or(ftp_open)     FILTER (WHERE rn <= 3) AS ftp_open_db,
        bool_or(http_open)    FILTER (WHERE rn <= 3) AS http_open_db,
        bool_or(control_open) FILTER (WHERE rn <= 3) AS control_open_db
    FROM health_ranked
    WHERE rn <= 3
    GROUP BY sid
),
latest_health AS (
    SELECT DISTINCT ON (r.sid) r.sid,
        COALESCE(good.overall_status, r.overall_status) AS overall_status,
        COALESCE(good.status_details, r.status_details) AS status_details,
        dp.ftp_open_db     AS ftp_open,
        dp.http_open_db    AS http_open,
        dp.control_open_db AS control_open,
        COALESCE(r.ftp_port,     good.ftp_port)     AS ftp_port,
        COALESCE(r.http_port,    good.http_port)    AS http_port,
        COALESCE(r.control_port, good.control_port) AS control_port,
        r.ts AS health_ts
    FROM health_ranked r
    JOIN health_debounced_ports dp ON dp.sid = r.sid
    LEFT JOIN LATERAL (
        SELECT g.overall_status, g.status_details,
               g.ftp_port, g.http_port, g.control_port
        FROM health_ranked g
        WHERE g.sid = r.sid AND g.rn <= 3
          AND (NOT dp.control_open_db OR g.control_open)
          AND (NOT dp.ftp_open_db     OR g.ftp_open)
          AND (NOT dp.http_open_db    OR g.http_open)
        ORDER BY g.ts DESC LIMIT 1
    ) good ON true
    WHERE r.rn = 1
    ORDER BY r.sid
),
latest_ntrip AS (
    SELECT DISTINCT ON (sid) sid, status AS ntrip_status
    FROM (
        SELECT sid, ts, status FROM block_ntrip_server
        UNION ALL
        SELECT sid, ts, status FROM block_ntrip_client
    ) ntrip_all
    ORDER BY sid, ts DESC
),
latest_sat_breakdown AS (
    SELECT DISTINCT ON (sid) sid,
        gps AS gps_sats, glonass AS glonass_sats,
        galileo AS galileo_sats, beidou AS beidou_sats
    FROM block_satellite_tracking
    ORDER BY sid, ts DESC
)
SELECT
    m.station_id,
    m.station_name,
    s.receiver_type,
    s.antenna_type,
    s.ip_address,
    s.power_type,
    s.http_port AS station_http_port,

    m.voltage, m.power_source, m.power_ts,
    m.cpu_load, m.temperature, m.uptime_seconds,
    m.rx_status, m.rx_error, m.receiver_ts,

    m.latitude AS metrics_latitude,
    m.longitude AS metrics_longitude,
    m.height AS metrics_height,
    m.satellites_used, m.h_accuracy, m.v_accuracy, m.position_ts,

    COALESCE(s.latitude, m.latitude) AS latitude,
    COALESCE(s.longitude, m.longitude) AS longitude,

    m.satellites_tracked, m.sat_ts,
    lsb.gps_sats, lsb.glonass_sats, lsb.galileo_sats, lsb.beidou_sats,

    m.disk_usage_pct, m.free_space_mb, m.disk_ts,

    COALESCE(m.seconds_since_update, EXTRACT(EPOCH FROM (NOW() - sc.last_check))::integer) AS seconds_since_update,
    COALESCE(m.last_update, sc.last_check) AS last_update,

    sc.is_online,
    sc.connection_state,
    sc.last_check,
    sc.state_since,
    sc.state_duration,
    sc.response_time_ms AS ping_response_ms,
    sc.packet_loss,

    lh.overall_status, lh.status_details,
    lh.ftp_open, lh.http_open, lh.control_open,
    lh.ftp_port, lh.http_port AS health_http_port, lh.control_port,

    ln.ntrip_status,

    sp.download_status,
    sp.health_status AS port_health_status,

    -- Download performance (from station_download_summary)
    ds.avg_speed_bps,
    ds.completions,
    ds.stalls,
    ds.failures AS download_failures,
    ds.avg_stall_duration_s,
    ds.last_download_at,
    ds.last_stall_at,
    s.stall_timeout_override,

    CASE
        WHEN m.seconds_since_update IS NULL THEN 'unknown'
        WHEN m.seconds_since_update > 3600 THEN 'offline'
        WHEN m.seconds_since_update > 300 THEN 'stale'
        ELSE 'online'
    END AS connection_status,

    CASE
        WHEN m.voltage IS NULL THEN 'unknown'
        WHEN m.voltage < 11.0 OR m.voltage > 16.0 THEN 'critical'
        WHEN m.voltage < 11.8 OR m.voltage > 15.0 THEN 'warning'
        ELSE 'ok'
    END AS voltage_status,

    CASE
        WHEN m.temperature IS NULL THEN 'unknown'
        WHEN m.temperature > 60 THEN 'critical'
        WHEN m.temperature > 50 THEN 'warning'
        ELSE 'ok'
    END AS temperature_status,

    CASE
        WHEN m.cpu_load IS NULL THEN 'unknown'
        WHEN m.cpu_load > 90 THEN 'critical'
        WHEN m.cpu_load > 75 THEN 'warning'
        ELSE 'ok'
    END AS cpu_status,

    CASE
        WHEN m.satellites_used IS NULL THEN 'unknown'
        WHEN m.satellites_used < 4 THEN 'critical'
        WHEN m.satellites_used < 8 THEN 'warning'
        ELSE 'ok'
    END AS satellite_status,

    s.station_status,
    s.health_check

FROM station_latest_metrics m
JOIN stations s ON s.sid = m.station_id
LEFT JOIN latest_health lh ON lh.sid = m.station_id
LEFT JOIN latest_ntrip ln ON ln.sid = m.station_id
LEFT JOIN latest_sat_breakdown lsb ON lsb.sid = m.station_id
LEFT JOIN station_connectivity sc ON sc.sid = m.station_id
LEFT JOIN station_port_status sp ON sp.sid = m.station_id
LEFT JOIN station_download_summary ds ON ds.sid = m.station_id;

COMMENT ON VIEW station_dashboard_data IS 'Unified dashboard data for all Grafana dashboards - one row per station with all metrics, status, connectivity, and download performance';

-- ============================================================================
-- 5. RECREATE DEPENDENT VIEW: station_data_flow_status
-- ============================================================================
-- This view was dropped by CASCADE above. Recreate from migration 024.

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
