-- Migration 029: Dashboard view performance optimization
--
-- Problem: station_data_flow_status takes ~6.4 seconds per query.
-- The view cascade (station_data_flow_status → station_dashboard_data →
-- station_latest_metrics + station_connectivity + station_port_status) does
-- unbounded full-table scans on block_* tables with 300k-450k rows each.
--
-- Optimizations:
--   1. station_latest_metrics: Replace 5 DISTINCT ON full-table scans with
--      LATERAL JOIN pattern (196 index seeks instead of 1.4M row scans)
--   2. station_connectivity: Add 7-day time bound to ping window functions
--      (448k rows → ~100k rows)
--   3. station_dashboard_data: Add 1-day time bound to health_ranked and
--      latest_sat_breakdown CTEs
--   4. station_port_status: Add 1-hour time bound to ROW_NUMBER CTE
--   5. station_data_flow_status: Add time bounds to health_streak and
--      ever_checked CTEs
--
-- Also incorporates migration 028 changes (power-type-aware voltage_status)
-- which was not yet applied to the database.
--
-- Expected improvement: ~6.4s → <500ms per query
--
-- Dependency chain (drop order → recreate in reverse):
--   station_data_flow_status → station_dashboard_data →
--     station_latest_metrics, station_connectivity, station_port_status

BEGIN;

-- ============================================================================
-- 0. Record migration 027 (already applied manually but not in schema_migrations)
-- ============================================================================
INSERT INTO schema_migrations (migration_name)
VALUES ('027_staleness_guards')
ON CONFLICT DO NOTHING;

INSERT INTO schema_migrations (migration_name)
VALUES ('028_dcdc24_voltage')
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 1. Drop dependent views (forward dependency order)
-- ============================================================================
DROP VIEW IF EXISTS station_data_flow_status;
DROP VIEW IF EXISTS station_dashboard_data;
DROP VIEW IF EXISTS icinga_check_data;
DROP VIEW IF EXISTS station_status_summary;
DROP VIEW IF EXISTS station_latest_metrics;
DROP VIEW IF EXISTS station_connectivity;
DROP VIEW IF EXISTS station_port_status;

-- ============================================================================
-- 2. Recreate station_latest_metrics — LATERAL JOIN pattern
--    Old: 5 DISTINCT ON full-table scans (~505ms)
--    New: 5 LATERAL index seeks per station (~196 stations × 5 = 980 seeks)
-- ============================================================================
CREATE VIEW station_latest_metrics AS
SELECT
    s.sid AS station_id,
    COALESCE(s.marker_name, s.sid) AS station_name,

    lp.voltage,
    lp.power_source,
    lp.ts AS power_ts,

    lr.cpu_load,
    lr.temperature,
    lr.uptime_seconds,
    lr.rx_status,
    lr.rx_error,
    lr.ts AS receiver_ts,

    lpos.fix_type,
    lpos.latitude,
    lpos.longitude,
    lpos.height,
    lpos.nr_sv AS satellites_used,
    lpos.h_accuracy,
    lpos.v_accuracy,
    lpos.ts AS position_ts,

    ls.total::bigint AS satellites_tracked,
    ls.ts AS sat_ts,

    ld.usage_percent AS disk_usage_pct,
    (ld.total_mb - ld.used_mb) AS free_space_mb,
    ld.ts AS disk_ts,

    EXTRACT(EPOCH FROM NOW() - GREATEST(lp.ts, lr.ts, lpos.ts))::integer
        AS seconds_since_update,
    GREATEST(lp.ts, lr.ts, lpos.ts) AS last_update

FROM stations s

LEFT JOIN LATERAL (
    SELECT ts, voltage, power_source
    FROM block_power_status
    WHERE sid = s.sid
    ORDER BY ts DESC LIMIT 1
) lp ON true

LEFT JOIN LATERAL (
    SELECT ts, cpu_load, temperature, uptime_seconds, rx_status, rx_error
    FROM block_receiver_status
    WHERE sid = s.sid
    ORDER BY ts DESC LIMIT 1
) lr ON true

LEFT JOIN LATERAL (
    SELECT ts, fix_type, latitude, longitude, height, nr_sv, h_accuracy, v_accuracy
    FROM block_pvt_geodetic
    WHERE sid = s.sid
    ORDER BY ts DESC LIMIT 1
) lpos ON true

LEFT JOIN LATERAL (
    SELECT ts, total
    FROM block_satellite_tracking
    WHERE sid = s.sid
    ORDER BY ts DESC LIMIT 1
) ls ON true

LEFT JOIN LATERAL (
    SELECT ts, usage_percent, total_mb, used_mb
    FROM block_disk_status
    WHERE sid = s.sid
    ORDER BY ts DESC LIMIT 1
) ld ON true;


-- ============================================================================
-- 2b. Recreate station_status_summary (depends on station_latest_metrics)
-- ============================================================================
CREATE VIEW station_status_summary AS
SELECT station_id,
    station_name,
    CASE
        WHEN seconds_since_update IS NULL THEN 'unknown'
        WHEN seconds_since_update > 3600 THEN 'offline'
        WHEN seconds_since_update > 300 THEN 'stale'
        ELSE 'online'
    END AS connection_status,
    CASE
        WHEN voltage IS NULL THEN 'unknown'
        WHEN voltage < 11.0 OR voltage > 16.0 THEN 'critical'
        WHEN voltage < 11.8 OR voltage > 15.0 THEN 'warning'
        ELSE 'ok'
    END AS voltage_status,
    CASE
        WHEN temperature IS NULL THEN 'unknown'
        WHEN temperature > 60 THEN 'critical'
        WHEN temperature > 50 THEN 'warning'
        ELSE 'ok'
    END AS temperature_status,
    CASE
        WHEN cpu_load IS NULL THEN 'unknown'
        WHEN cpu_load > 90 THEN 'critical'
        WHEN cpu_load > 75 THEN 'warning'
        ELSE 'ok'
    END AS cpu_status,
    CASE
        WHEN satellites_used IS NULL THEN 'unknown'
        WHEN satellites_used < 4 THEN 'critical'
        WHEN satellites_used < 8 THEN 'warning'
        ELSE 'ok'
    END AS satellite_status,
    CASE
        WHEN fix_type IS NULL THEN 'unknown'
        WHEN fix_type IN ('fixed', 'rtk_fixed', '3d', 'standalone') THEN 'ok'
        WHEN fix_type IN ('float', 'rtk_float', 'single', 'dgps') THEN 'warning'
        ELSE 'critical'
    END AS position_status,
    voltage,
    temperature,
    cpu_load,
    satellites_used,
    fix_type,
    uptime_seconds,
    seconds_since_update,
    last_update
FROM station_latest_metrics;


-- ============================================================================
-- 2c. Recreate icinga_check_data (depends on station_latest_metrics)
-- ============================================================================
CREATE VIEW icinga_check_data AS
SELECT station_id,
    CASE
        WHEN seconds_since_update <= 300 THEN 0
        ELSE 2
    END AS ping_exit_code,
    CASE
        WHEN seconds_since_update <= 300 THEN format('OK - %s responding', station_id)
        ELSE format('CRITICAL - %s not responding for %s seconds', station_id, seconds_since_update)
    END AS ping_output,
    CASE
        WHEN temperature IS NULL THEN 3
        WHEN temperature > 60 THEN 2
        WHEN temperature > 50 THEN 1
        ELSE 0
    END AS temp_exit_code,
    format('Temperature: %s°C', COALESCE(temperature::text, 'unknown')) AS temp_output,
    format('temp=%sC;50;60', COALESCE(temperature::text, '')) AS temp_perfdata,
    CASE
        WHEN voltage IS NULL THEN 3
        WHEN voltage < 11.0 OR voltage > 16.0 THEN 2
        WHEN voltage < 11.8 OR voltage > 15.0 THEN 1
        ELSE 0
    END AS volt_exit_code,
    format('Voltage: %sV', COALESCE(voltage::text, 'unknown')) AS volt_output,
    format('voltage=%sV;11.8:15.0;11.0:16.0', COALESCE(voltage::text, '')) AS volt_perfdata,
    CASE
        WHEN cpu_load IS NULL THEN 3
        WHEN cpu_load > 90 THEN 2
        WHEN cpu_load > 75 THEN 1
        ELSE 0
    END AS cpu_exit_code,
    format('CPU Load: %s%%', COALESCE(cpu_load::text, 'unknown')) AS cpu_output,
    format('cpu=%s%%;75;90', COALESCE(cpu_load::text, '')) AS cpu_perfdata,
    CASE
        WHEN satellites_used IS NULL THEN 3
        WHEN satellites_used < 4 THEN 2
        WHEN satellites_used < 8 THEN 1
        ELSE 0
    END AS sat_exit_code,
    format('Satellites: %s used', COALESCE(satellites_used::text, 'unknown')) AS sat_output,
    format('satellites=%s;8:;4:', COALESCE(satellites_used::text, '')) AS sat_perfdata,
    last_update
FROM station_latest_metrics;


-- ============================================================================
-- 3. Recreate station_connectivity — time-bounded ping window functions
--    Old: Window functions scan ALL 448k ping rows (no time bound)
--    New: 7-day window (enough for state duration, ~100k rows)
-- ============================================================================
CREATE VIEW station_connectivity AS
WITH latest_pings AS (
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_ping_status
    WHERE ts > NOW() - INTERVAL '1 hour'
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
    WHERE ts > NOW() - INTERVAL '1 hour'
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
    WHERE ts > NOW() - INTERVAL '1 hour'
    ORDER BY sid, ts DESC
),
-- Connection state duration: scan 2 days of pings to find last state change
-- (2 days ≈ 100k rows vs 7 days ≈ 300k; sufficient for monitoring display)
ping_with_debounced AS (
    SELECT sid, ts, is_online,
           bool_or(is_online) OVER (PARTITION BY sid ORDER BY ts ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS debounced_online
    FROM block_ping_status
    WHERE ts > NOW() - INTERVAL '2 days'
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
    CASE
        WHEN COALESCE(nt.ntrip_status, '') = 'connected' THEN true
        WHEN COALESCE(prd.port_any_ok, false) THEN true
        WHEN COALESCE(prd.port_all_fail, false) THEN false
        WHEN prd.port_any_ok IS NULL AND COALESCE(pd.ping_any_ok, false) THEN true
        WHEN COALESCE(pd.ping_any_ok, false) THEN true
        ELSE false
    END AS is_online,
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
-- 4. Recreate station_port_status — time-bounded
--    Old: ROW_NUMBER on ALL 381k port rows (~301ms)
--    New: 1-hour window (only recent records)
-- ============================================================================
CREATE VIEW station_port_status AS
WITH latest_three AS (
    SELECT sid, ts, download_port, download_status, download_response_ms,
           health_port, health_status, health_response_ms,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_port_status
    WHERE ts > NOW() - INTERVAL '1 hour'
),
latest AS (
    SELECT sid, ts, download_port, download_status, download_response_ms,
           health_port, health_status, health_response_ms, rn
    FROM latest_three
    WHERE rn = 1
),
debounced AS (
    SELECT sid,
        bool_or(download_status IN ('open', 'ok')) FILTER (WHERE rn <= 3) AS download_any_open,
        bool_or(health_status IN ('open', 'ok')) FILTER (WHERE rn <= 3) AS health_any_open
    FROM latest_three
    WHERE rn <= 3
    GROUP BY sid
),
effective AS (
    SELECT l.sid,
        l.ts AS last_check,
        l.download_port,
        CASE WHEN d.download_any_open THEN 'open'::varchar
             ELSE COALESCE(l.download_status, 'unknown'::varchar)
        END AS download_status,
        l.download_response_ms,
        l.health_port,
        CASE WHEN d.health_any_open THEN 'open'::varchar
             ELSE COALESCE(l.health_status, 'unknown'::varchar)
        END AS health_status,
        l.health_response_ms
    FROM latest l
    LEFT JOIN debounced d ON l.sid = d.sid
)
SELECT sid, last_check,
    download_port, download_status, download_response_ms,
    health_port, health_status, health_response_ms,
    CASE
        WHEN download_status IN ('open', 'ok') AND (health_status IN ('open', 'ok') OR health_status IS NULL)
            THEN 'active'::varchar
        WHEN download_status IN ('refused', 'timeout', 'error', 'critical')
            THEN download_status
        WHEN health_status IN ('refused', 'timeout', 'error', 'critical')
            THEN health_status
        WHEN download_status = 'warning' OR health_status = 'warning'
            THEN 'warning'::varchar
        ELSE 'unknown'::varchar
    END AS overall_port_status
FROM effective;


-- ============================================================================
-- 5. Recreate station_dashboard_data — time-bounded CTEs + 028 voltage
--    Old: health_ranked scans ALL 448k health rows, satellite ALL 307k rows
--    New: 1-day window (health checks every 5 min = ~288 rows/station/day)
-- ============================================================================
CREATE VIEW station_dashboard_data AS
WITH health_ranked AS (
    SELECT sid, ts, overall_status, status_details,
           ftp_open, http_open, control_open,
           ftp_port, http_port, control_port,
           row_number() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_health_summary
    WHERE ts > NOW() - INTERVAL '1 day'
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
    WHERE ts > NOW() - INTERVAL '1 hour'
    ORDER BY sid, ts DESC
),
latest_sat_breakdown AS (
    SELECT DISTINCT ON (sid) sid,
        gps AS gps_sats, glonass AS glonass_sats,
        galileo AS galileo_sats, beidou AS beidou_sats
    FROM block_satellite_tracking
    WHERE ts > NOW() - INTERVAL '1 day'
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

    -- Corrected overall_status: override stale voltage-critical when the
    -- power-type-aware thresholds say voltage is NOT critical.
    CASE
        WHEN lh.overall_status IS DISTINCT FROM 'critical' THEN lh.overall_status
        WHEN lh.status_details IS NULL OR lh.status_details NOT LIKE '%Voltage%' THEN lh.overall_status
        WHEN m.voltage IS NULL THEN lh.overall_status
        WHEN s.power_type = 'dcdc24'
             AND m.voltage BETWEEN 18.0 AND 30.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage BETWEEN 20.0 AND 28.0 THEN 'healthy'
                 ELSE 'warning' END
        WHEN s.power_type = 'mains'
             AND m.voltage BETWEEN 15.0 AND 30.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage BETWEEN 18.0 AND 28.0 THEN 'healthy'
                 ELSE 'warning' END
        WHEN s.power_type = 'dcdc'
             AND m.voltage BETWEEN 11.0 AND 18.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage BETWEEN 12.0 AND 16.5 THEN 'healthy'
                 ELSE 'warning' END
        WHEN COALESCE(s.power_type, 'battery') = 'battery'
             AND m.voltage BETWEEN 11.0 AND 16.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage BETWEEN 11.8 AND 15.0 THEN 'healthy'
                 ELSE 'warning' END
        ELSE lh.overall_status
    END AS overall_status,
    CASE
        WHEN lh.status_details = 'Voltage'
             AND lh.overall_status = 'critical'
             AND m.voltage IS NOT NULL
             AND (
                 (s.power_type = 'dcdc24' AND m.voltage BETWEEN 20.0 AND 28.0) OR
                 (s.power_type = 'mains' AND m.voltage BETWEEN 18.0 AND 28.0) OR
                 (s.power_type = 'dcdc' AND m.voltage BETWEEN 12.0 AND 16.5) OR
                 (COALESCE(s.power_type, 'battery') = 'battery' AND m.voltage BETWEEN 11.8 AND 15.0)
             )
        THEN NULL
        ELSE lh.status_details
    END AS status_details,
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

    -- Power-type-aware voltage thresholds (from migration 028)
    CASE
        WHEN m.voltage IS NULL THEN 'unknown'
        WHEN s.power_type = 'dcdc24' THEN
            CASE WHEN m.voltage < 18.0 OR m.voltage > 30.0 THEN 'critical'
                 WHEN m.voltage < 20.0 OR m.voltage > 28.0 THEN 'warning'
                 ELSE 'ok'
            END
        WHEN s.power_type = 'mains' THEN
            CASE WHEN m.voltage < 15.0 OR m.voltage > 30.0 THEN 'critical'
                 WHEN m.voltage < 18.0 OR m.voltage > 28.0 THEN 'warning'
                 ELSE 'ok'
            END
        WHEN s.power_type = 'dcdc' THEN
            CASE WHEN m.voltage < 11.0 OR m.voltage > 18.0 THEN 'critical'
                 WHEN m.voltage < 12.0 OR m.voltage > 16.5 THEN 'warning'
                 ELSE 'ok'
            END
        ELSE -- battery (default)
            CASE WHEN m.voltage < 11.0 OR m.voltage > 16.0 THEN 'critical'
                 WHEN m.voltage < 11.8 OR m.voltage > 15.0 THEN 'warning'
                 ELSE 'ok'
            END
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


-- ============================================================================
-- 6. Recreate station_data_flow_status — time-bounded health CTEs
--    Old: health_streak scans ALL 448k health rows; ever_checked full DISTINCT
--    New: 1-day bound on health_streak, EXISTS-based ever_checked
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
        WHERE ts > NOW() - INTERVAL '1 day'
    ) recent
    WHERE rn <= 6
    GROUP BY sid
),
ever_checked AS (
    SELECT s.sid
    FROM stations s
    WHERE EXISTS (
        SELECT 1 FROM block_health_summary bhs
        WHERE bhs.sid = s.sid
        LIMIT 1
    )
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

-- ============================================================================
-- 7. Record this migration
-- ============================================================================
INSERT INTO schema_migrations (migration_name) VALUES ('029_dashboard_performance');

COMMIT;
