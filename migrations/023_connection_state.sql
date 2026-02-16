-- Migration 023: Add connection_state to station_connectivity view
-- Adds three-state connectivity: 'online', 'degraded', 'offline'
-- 'degraded' = router reachable (ping OK) but receiver ports all down
--
-- Dependency chain: station_data_flow_status → station_dashboard_data → station_connectivity
-- Must drop in forward order, recreate in reverse order.

BEGIN;

-- ============================================================================
-- 1. Drop dependent views (forward dependency order)
-- ============================================================================
DROP VIEW IF EXISTS station_data_flow_status;
DROP VIEW IF EXISTS station_dashboard_data;
DROP VIEW IF EXISTS station_connectivity;

-- ============================================================================
-- 2. Recreate station_connectivity with connection_state column
-- ============================================================================
CREATE VIEW station_connectivity AS
WITH latest_pings AS (
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_ping_status
),
ping_debounced AS (
    SELECT sid,
           bool_or(is_online) FILTER (WHERE rn <= 3) AS ping_any_ok
    FROM latest_pings
    WHERE rn <= 3
    GROUP BY sid
),
latest_ping AS (
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message
    FROM latest_pings
    WHERE rn = 1
),
latest_ports AS (
    SELECT sid, ts, download_status,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_port_status
),
port_debounced AS (
    SELECT sid,
           bool_or(download_status IN ('open', 'ok')) FILTER (WHERE rn <= 3) AS port_any_ok,
           bool_and(download_status IN ('refused', 'timeout', 'unreachable', 'critical')) FILTER (WHERE rn <= 3) AS port_all_fail
    FROM latest_ports
    WHERE rn <= 3
    GROUP BY sid
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
ping_with_debounced AS (
    SELECT sid, ts, is_online,
           bool_or(is_online) OVER (PARTITION BY sid ORDER BY ts ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS debounced_online
    FROM block_ping_status
),
debounced_state_changes AS (
    SELECT sid, ts, debounced_online,
           LAG(debounced_online) OVER (PARTITION BY sid ORDER BY ts) AS prev_debounced
    FROM ping_with_debounced
),
debounced_state_start AS (
    SELECT DISTINCT ON (sid) sid, ts AS state_since
    FROM debounced_state_changes
    WHERE debounced_online <> prev_debounced OR prev_debounced IS NULL
    ORDER BY sid, ts DESC
)
SELECT
    lp.sid,
    lp.ts AS last_check,
    -- Existing is_online boolean (backward compatible)
    CASE
        WHEN COALESCE(nt.ntrip_status, '') = 'connected' THEN true
        WHEN COALESCE(prd.port_any_ok, false) THEN true
        WHEN COALESCE(prd.port_all_fail, false) THEN false
        WHEN prd.port_any_ok IS NULL AND COALESCE(pd.ping_any_ok, false) THEN true
        WHEN COALESCE(pd.ping_any_ok, false) THEN true
        ELSE false
    END AS is_online,
    -- New three-state: online / degraded / offline
    CASE
        WHEN COALESCE(nt.ntrip_status, '') = 'connected' THEN 'online'
        WHEN COALESCE(prd.port_any_ok, false) THEN 'online'
        WHEN COALESCE(prd.port_all_fail, false) AND COALESCE(pd.ping_any_ok, false) THEN 'degraded'
        WHEN COALESCE(prd.port_all_fail, false) THEN 'offline'
        WHEN prd.port_any_ok IS NULL AND COALESCE(pd.ping_any_ok, false) THEN 'online'
        WHEN COALESCE(pd.ping_any_ok, false) THEN 'online'
        ELSE 'offline'
    END AS connection_state,
    lp.response_time_ms,
    lp.packet_loss,
    lp.error_message,
    COALESCE(dss.state_since, lp.ts) AS state_since,
    NOW() - COALESCE(dss.state_since, lp.ts) AS state_duration
FROM latest_ping lp
LEFT JOIN ping_debounced pd ON pd.sid = lp.sid
LEFT JOIN port_debounced prd ON prd.sid = lp.sid
LEFT JOIN latest_ntrip nt ON nt.sid = lp.sid
LEFT JOIN debounced_state_start dss ON dss.sid = lp.sid;

-- ============================================================================
-- 3. Recreate station_dashboard_data with connection_state
-- ============================================================================
CREATE VIEW station_dashboard_data AS
WITH latest_health AS (
    SELECT DISTINCT ON (sid) sid, ts AS health_ts, overall_status, status_details,
           ftp_open, http_open, control_open, ftp_port, http_port, control_port
    FROM block_health_summary
    ORDER BY sid, ts DESC
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
    SELECT DISTINCT ON (sid) sid, gps AS gps_sats, glonass AS glonass_sats,
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
    m.latitude AS metrics_latitude, m.longitude AS metrics_longitude, m.height AS metrics_height,
    m.satellites_used, m.h_accuracy, m.v_accuracy, m.position_ts,
    COALESCE(s.latitude, m.latitude) AS latitude,
    COALESCE(s.longitude, m.longitude) AS longitude,
    m.satellites_tracked, m.sat_ts,
    lsb.gps_sats, lsb.glonass_sats, lsb.galileo_sats, lsb.beidou_sats,
    m.disk_usage_pct, m.free_space_mb, m.disk_ts,
    COALESCE(m.seconds_since_update, EXTRACT(EPOCH FROM NOW() - sc.last_check)::integer) AS seconds_since_update,
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
    sp.download_status, sp.health_status AS port_health_status,
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
LEFT JOIN station_port_status sp ON sp.sid = m.station_id;

-- ============================================================================
-- 4. Recreate station_data_flow_status (unchanged, just re-created due to dependency)
-- ============================================================================
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
           file_date + COALESCE(file_hour, 0) * INTERVAL '1 hour' AS latest_ts
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
           file_date + COALESCE(file_hour, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking
    WHERE session_type = '1Hz_1hr_rinex'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
-- Consecutive critical streak per station (for debounce)
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
base AS (
    SELECT
        d.station_id AS sid,
        -- Offline → unknown (separate category, shown in Offline panel)
        -- Connectivity-only critical + online → capped at Warning
        -- All critical: need 2+ consecutive checks before Critical
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
            WHEN r24.file_date IS NULL OR r24.file_date < CURRENT_DATE - 1 THEN 2
            WHEN x24.file_date IS NULL OR x24.file_date < r24.file_date THEN 1
            ELSE 0
        END AS status_24h,
        CASE
            WHEN d.station_status IS NOT NULL THEN -2
            WHEN NOT COALESCE(l.session_1hz_1hr, false) AND r1h.latest_ts IS NULL THEN -2
            WHEN r1h.latest_ts >= NOW() - INTERVAL '1.5 hours' THEN 0
            WHEN r1h.latest_ts >= NOW() - INTERVAL '6 hours' THEN 1
            WHEN r1h.latest_ts IS NOT NULL THEN 2
            ELSE 2
        END AS status_1hz,
        CASE
            WHEN d.station_status IS NOT NULL THEN -2
            WHEN d.receiver_type IS NULL
                 AND NOT COALESCE(l.session_15s_24hr, false)
                 AND r24.file_date IS NULL THEN -2
            WHEN x24.file_date >= CURRENT_DATE - 1 THEN 0
            WHEN x24.file_date IS NULL AND r24.file_date IS NOT NULL THEN 2
            WHEN x24.file_date IS NULL THEN -1
            WHEN EXTRACT(HOUR FROM NOW()) >= 12 THEN 2
            WHEN EXTRACT(HOUR FROM NOW()) >= 2 THEN 1
            ELSE 0
        END AS rinex_24h_status,
        CASE
            WHEN d.station_status IS NOT NULL THEN -2
            WHEN NOT COALESCE(l.session_1hz_1hr, false) AND r1h.latest_ts IS NULL THEN -2
            WHEN x1h.latest_ts >= NOW() - INTERVAL '1.5 hours' THEN 0
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
    LEFT JOIN latest_raw_24h r24 ON r24.sid = d.station_id
    LEFT JOIN latest_raw_1hz r1h ON r1h.sid = d.station_id
    LEFT JOIN latest_rinex_24h x24 ON x24.sid = d.station_id
    LEFT JOIN latest_rinex_1hz x1h ON x1h.sid = d.station_id
)
SELECT
    sid, health_status, status_24h, status_1hz,
    rinex_24h_status, rinex_1hz_status,
    raw_24h_date, raw_1hz_ts, rinex_24h_date, rinex_1hz_ts,
    logging_15s, logging_1hz,
    CASE
        WHEN health_status < 0 AND status_24h < 0 THEN -1
        WHEN health_status = 2 AND status_24h = 2 THEN 2
        WHEN GREATEST(
            CASE WHEN health_status < 0 THEN 0 ELSE health_status END,
            CASE WHEN status_24h < 0 THEN 0 ELSE status_24h END
        ) = 2 THEN 1
        WHEN GREATEST(
            CASE WHEN health_status < 0 THEN 0 ELSE health_status END,
            CASE WHEN status_24h < 0 THEN 0 ELSE status_24h END
        ) = 1 THEN 1
        ELSE 0
    END AS combined_status
FROM base;

COMMIT;
