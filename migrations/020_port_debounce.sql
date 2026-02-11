-- Migration: 020_port_debounce.sql
-- Description: Port status debounce — require 3 consecutive failures before reporting closed
-- Date: 2026-02-11
--
-- On lossy 3G/4G links, individual port checks can return "refused" or "timeout"
-- spuriously. This causes false warnings in the dashboard.
--
-- Changes:
--   1. station_port_status: upgrade from 2-check to 3-check debounce,
--      treat "refused" same as timeout (no longer definitive)
--   2. station_dashboard_data: debounce control/ftp/http port booleans
--      from block_health_summary over last 3 checks. Use status from
--      the most recent "good" check to keep overall_status consistent.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/020_port_debounce.sql

BEGIN;

-- ============================================================================
-- 1. UPDATE station_port_status — 3-check debounce, refused not definitive
-- ============================================================================

CREATE OR REPLACE VIEW station_port_status AS
WITH latest_three AS (
    SELECT sid, ts, download_port, download_status, download_response_ms,
           health_port, health_status, health_response_ms,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_port_status
),
latest AS (
    SELECT * FROM latest_three WHERE rn = 1
),
debounced AS (
    -- Port is "open" if ANY of the last 3 checks showed it open
    SELECT sid,
        BOOL_OR(download_status IN ('open', 'ok')) FILTER (WHERE rn <= 3) AS download_any_open,
        BOOL_OR(health_status IN ('open', 'ok')) FILTER (WHERE rn <= 3) AS health_any_open
    FROM latest_three
    WHERE rn <= 3
    GROUP BY sid
),
effective AS (
    SELECT
        l.sid,
        l.ts AS last_check,
        l.download_port,
        CASE
            WHEN d.download_any_open THEN 'open'
            ELSE COALESCE(l.download_status, 'unknown')
        END AS download_status,
        l.download_response_ms,
        l.health_port,
        CASE
            WHEN d.health_any_open THEN 'open'
            ELSE COALESCE(l.health_status, 'unknown')
        END AS health_status,
        l.health_response_ms
    FROM latest l
    LEFT JOIN debounced d ON l.sid = d.sid
)
SELECT
    sid,
    last_check,
    download_port,
    download_status,
    download_response_ms,
    health_port,
    health_status,
    health_response_ms,
    CASE
        WHEN download_status IN ('open', 'ok') AND (health_status IN ('open', 'ok') OR health_status IS NULL) THEN 'active'
        WHEN download_status IN ('refused', 'timeout', 'error', 'critical') THEN download_status
        WHEN health_status IN ('refused', 'timeout', 'error', 'critical') THEN health_status
        WHEN download_status = 'warning' OR health_status = 'warning' THEN 'warning'
        ELSE 'unknown'
    END AS overall_port_status
FROM effective;

COMMENT ON VIEW station_port_status IS 'Latest port status per station (requires 3 consecutive failures before reporting down)';


-- ============================================================================
-- 2. DROP dependent view, recreate station_dashboard_data with debounce
-- ============================================================================

-- station_data_flow_status depends on station_dashboard_data
DROP VIEW IF EXISTS station_data_flow_status;

-- Now safe to drop and recreate
DROP VIEW IF EXISTS station_dashboard_data;

CREATE VIEW station_dashboard_data AS
WITH health_ranked AS (
    SELECT sid, ts,
           overall_status, status_details,
           ftp_open, http_open, control_open,
           ftp_port, http_port, control_port,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_health_summary
),
health_debounced_ports AS (
    -- Port is "open" if ANY of last 3 checks showed it open
    SELECT sid,
        BOOL_OR(ftp_open) FILTER (WHERE rn <= 3) AS ftp_open_db,
        BOOL_OR(http_open) FILTER (WHERE rn <= 3) AS http_open_db,
        BOOL_OR(control_open) FILTER (WHERE rn <= 3) AS control_open_db
    FROM health_ranked
    WHERE rn <= 3
    GROUP BY sid
),
latest_health AS (
    -- Debounced port booleans. For overall_status and status_details,
    -- prefer the most recent record where ports matched the debounced state
    -- (avoids showing "WARNING: CONTROL refused" when debounced says open).
    SELECT DISTINCT ON (r.sid)
        r.sid,
        COALESCE(good.overall_status, r.overall_status) AS overall_status,
        COALESCE(good.status_details, r.status_details) AS status_details,
        dp.ftp_open_db AS ftp_open,
        dp.http_open_db AS http_open,
        dp.control_open_db AS control_open,
        COALESCE(r.ftp_port, good.ftp_port) AS ftp_port,
        COALESCE(r.http_port, good.http_port) AS http_port,
        COALESCE(r.control_port, good.control_port) AS control_port,
        r.ts AS health_ts
    FROM health_ranked r
    JOIN health_debounced_ports dp ON dp.sid = r.sid
    LEFT JOIN LATERAL (
        -- Most recent record in last 3 where ports match debounced state
        SELECT overall_status, status_details, ftp_port, http_port, control_port
        FROM health_ranked g
        WHERE g.sid = r.sid AND g.rn <= 3
          AND (NOT dp.control_open_db OR g.control_open)
          AND (NOT dp.ftp_open_db OR g.ftp_open)
          AND (NOT dp.http_open_db OR g.http_open)
        ORDER BY g.ts DESC
        LIMIT 1
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
        gps AS gps_sats,
        glonass AS glonass_sats,
        galileo AS galileo_sats,
        beidou AS beidou_sats
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

    m.voltage,
    m.power_source,
    m.power_ts,

    m.cpu_load,
    m.temperature,
    m.uptime_seconds,
    m.rx_status,
    m.rx_error,
    m.receiver_ts,

    m.latitude AS metrics_latitude,
    m.longitude AS metrics_longitude,
    m.height AS metrics_height,
    m.satellites_used,
    m.h_accuracy,
    m.v_accuracy,
    m.position_ts,

    COALESCE(s.latitude, m.latitude) AS latitude,
    COALESCE(s.longitude, m.longitude) AS longitude,

    m.satellites_tracked,
    m.sat_ts,
    lsb.gps_sats,
    lsb.glonass_sats,
    lsb.galileo_sats,
    lsb.beidou_sats,

    m.disk_usage_pct,
    m.free_space_mb,
    m.disk_ts,

    COALESCE(m.seconds_since_update, EXTRACT(EPOCH FROM (NOW() - sc.last_check))::integer) AS seconds_since_update,
    COALESCE(m.last_update, sc.last_check) AS last_update,

    sc.is_online,
    sc.last_check,
    sc.state_since,
    sc.state_duration,
    sc.response_time_ms AS ping_response_ms,

    -- Health summary (debounced ports + consistent overall_status)
    lh.overall_status,
    lh.status_details,
    lh.ftp_open,
    lh.http_open,
    lh.control_open,
    lh.ftp_port,
    lh.http_port AS health_http_port,
    lh.control_port,

    ln.ntrip_status,

    sp.download_status,
    sp.health_status AS port_health_status,

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

    -- Station lifecycle (from migration 015)
    s.station_status,
    s.health_check

FROM station_latest_metrics m
JOIN stations s ON s.sid = m.station_id
LEFT JOIN latest_health lh ON lh.sid = m.station_id
LEFT JOIN latest_ntrip ln ON ln.sid = m.station_id
LEFT JOIN latest_sat_breakdown lsb ON lsb.sid = m.station_id
LEFT JOIN station_connectivity sc ON sc.sid = m.station_id
LEFT JOIN station_port_status sp ON sp.sid = m.station_id;

COMMENT ON VIEW station_dashboard_data IS 'Unified dashboard data with 3-check port debounce — requires 3 consecutive failures before reporting port closed';


-- ============================================================================
-- 3. RECREATE station_data_flow_status (depends on station_dashboard_data)
-- ============================================================================

CREATE VIEW station_data_flow_status AS
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
    CASE
      WHEN l.sid IS NULL THEN -1
      WHEN NOT COALESCE(l.session_15s_24hr, false) THEN 2
      WHEN r24.file_date >= CURRENT_DATE - 1
           AND COALESCE(x24.file_date >= CURRENT_DATE - 1, true) THEN 0
      WHEN EXTRACT(HOUR FROM NOW()) >= 12 THEN 2
      WHEN EXTRACT(HOUR FROM NOW()) >= 2 THEN 1
      ELSE 0
    END AS status_24h,
    CASE
      WHEN l.sid IS NULL THEN -1
      WHEN l.session_1hz_1hr IS NULL OR NOT l.session_1hz_1hr THEN -2
      WHEN r1h.latest_ts >= NOW() - INTERVAL '90 minutes' THEN 0
      WHEN r1h.latest_ts >= NOW() - INTERVAL '6 hours' THEN 1
      WHEN r1h.latest_ts IS NOT NULL THEN 2
      ELSE 2
    END AS status_1hz,
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
    r24.file_date AS raw_24h_date,
    r1h.latest_ts AS raw_1hz_ts,
    x24.file_date AS rinex_24h_date,
    x1h.latest_ts AS rinex_1hz_ts,
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
