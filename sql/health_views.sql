-- Health data views for Icinga/Grafana
-- These provide compact summaries of the block tables

-- Drop existing views if they exist
DROP VIEW IF EXISTS station_latest_metrics CASCADE;
DROP VIEW IF EXISTS station_health_history CASCADE;
DROP VIEW IF EXISTS station_status_summary CASCADE;

-- =============================================================================
-- View: station_latest_metrics
-- Purpose: Get the most recent metrics for each station (single row per station)
-- Use case: Icinga checks, Grafana single-stat panels
-- =============================================================================
CREATE VIEW station_latest_metrics AS
WITH latest_power AS (
    SELECT DISTINCT ON (sid)
        sid,
        ts as power_ts,
        voltage,
        power_source
    FROM block_power_status
    ORDER BY sid, ts DESC
),
latest_receiver AS (
    SELECT DISTINCT ON (sid)
        sid,
        ts as receiver_ts,
        cpu_load,
        temperature,
        uptime_seconds,
        rx_status,
        rx_error
    FROM block_receiver_status
    ORDER BY sid, ts DESC
),
latest_position AS (
    SELECT DISTINCT ON (sid)
        sid,
        ts as position_ts,
        fix_type,
        latitude,
        longitude,
        height,
        nr_sv as satellites_used,
        h_accuracy,
        v_accuracy
    FROM block_pvt_geodetic
    ORDER BY sid, ts DESC
),
latest_satellites AS (
    SELECT DISTINCT ON (sid)
        sid,
        ts as sat_ts,
        COUNT(*) as satellites_tracked
    FROM block_satellite_tracking
    GROUP BY sid, ts
    ORDER BY sid, ts DESC
),
latest_disk AS (
    SELECT DISTINCT ON (sid)
        sid,
        ts as disk_ts,
        usage_percent as disk_usage_pct,
        (total_mb - used_mb) as free_space_mb
    FROM block_disk_status
    ORDER BY sid, ts DESC
)
SELECT
    s.sid as station_id,
    COALESCE(s.marker_name, s.sid) as station_name,
    -- Power metrics
    lp.voltage,
    lp.power_source,
    lp.power_ts,
    -- Receiver metrics
    lr.cpu_load,
    lr.temperature,
    lr.uptime_seconds,
    lr.rx_status,
    lr.rx_error,
    lr.receiver_ts,
    -- Position metrics
    lpos.fix_type,
    lpos.latitude,
    lpos.longitude,
    lpos.height,
    lpos.satellites_used,
    lpos.h_accuracy,
    lpos.v_accuracy,
    lpos.position_ts,
    -- Satellite tracking
    ls.satellites_tracked,
    ls.sat_ts,
    -- Disk status
    ld.disk_usage_pct,
    ld.free_space_mb,
    ld.disk_ts,
    -- Computed fields
    EXTRACT(EPOCH FROM (NOW() - GREATEST(
        lp.power_ts, lr.receiver_ts, lpos.position_ts
    )))::int as seconds_since_update,
    GREATEST(lp.power_ts, lr.receiver_ts, lpos.position_ts) as last_update
FROM stations s
LEFT JOIN latest_power lp ON s.sid = lp.sid
LEFT JOIN latest_receiver lr ON s.sid = lr.sid
LEFT JOIN latest_position lpos ON s.sid = lpos.sid
LEFT JOIN latest_satellites ls ON s.sid = ls.sid
LEFT JOIN latest_disk ld ON s.sid = ld.sid;

COMMENT ON VIEW station_latest_metrics IS 'Latest metrics for each station - single row per station for Icinga checks';

-- =============================================================================
-- View: station_health_history
-- Purpose: Time series of key health metrics (for Grafana graphs)
-- Use case: Grafana time series panels
-- =============================================================================
CREATE VIEW station_health_history AS
SELECT
    r.sid as station_id,
    r.ts as timestamp,
    -- Receiver metrics
    r.cpu_load,
    r.temperature,
    r.uptime_seconds,
    -- Power metrics (joined)
    p.voltage,
    -- Position metrics (joined)
    pos.fix_type,
    pos.nr_sv as satellites_used,
    pos.h_accuracy,
    pos.v_accuracy
FROM block_receiver_status r
LEFT JOIN block_power_status p ON r.sid = p.sid AND r.ts = p.ts
LEFT JOIN block_pvt_geodetic pos ON r.sid = pos.sid AND r.ts = pos.ts
ORDER BY r.ts DESC;

COMMENT ON VIEW station_health_history IS 'Time series of health metrics for Grafana graphs';

-- =============================================================================
-- View: station_status_summary
-- Purpose: Summary of current status across all stations
-- Use case: Grafana table panels, Icinga overview
-- =============================================================================
CREATE VIEW station_status_summary AS
SELECT
    m.station_id,
    m.station_name,
    -- Status indicators
    CASE
        WHEN m.seconds_since_update > 3600 THEN 'offline'
        WHEN m.seconds_since_update > 300 THEN 'stale'
        ELSE 'online'
    END as connection_status,
    CASE
        WHEN m.voltage IS NULL THEN 'unknown'
        WHEN m.voltage < 11.0 THEN 'critical'
        WHEN m.voltage < 11.8 OR m.voltage > 15.0 THEN 'warning'
        ELSE 'ok'
    END as voltage_status,
    CASE
        WHEN m.temperature IS NULL THEN 'unknown'
        WHEN m.temperature > 60 THEN 'critical'
        WHEN m.temperature > 50 THEN 'warning'
        ELSE 'ok'
    END as temperature_status,
    CASE
        WHEN m.cpu_load IS NULL THEN 'unknown'
        WHEN m.cpu_load > 90 THEN 'critical'
        WHEN m.cpu_load > 75 THEN 'warning'
        ELSE 'ok'
    END as cpu_status,
    CASE
        WHEN m.satellites_used IS NULL THEN 'unknown'
        WHEN m.satellites_used < 4 THEN 'critical'
        WHEN m.satellites_used < 8 THEN 'warning'
        ELSE 'ok'
    END as satellite_status,
    CASE
        WHEN m.fix_type IS NULL THEN 'unknown'
        WHEN m.fix_type IN ('fixed', 'rtk_fixed', '3d', 'standalone') THEN 'ok'
        WHEN m.fix_type IN ('float', 'rtk_float', 'single', 'dgps') THEN 'warning'
        ELSE 'critical'
    END as position_status,
    -- Values
    m.voltage,
    m.temperature,
    m.cpu_load,
    m.satellites_used,
    m.fix_type,
    m.uptime_seconds,
    m.seconds_since_update,
    m.last_update
FROM station_latest_metrics m;

COMMENT ON VIEW station_status_summary IS 'Summary of status for all stations with status indicators';

-- =============================================================================
-- View: icinga_check_data
-- Purpose: Pre-formatted data for Icinga checks
-- Use case: Direct querying for passive checks
-- =============================================================================
CREATE VIEW icinga_check_data AS
SELECT
    m.station_id,
    -- GPS Ping
    CASE WHEN m.seconds_since_update <= 300 THEN 0 ELSE 2 END as ping_exit_code,
    CASE
        WHEN m.seconds_since_update <= 300 THEN format('OK - %s responding', m.station_id)
        ELSE format('CRITICAL - %s not responding for %s seconds', m.station_id, m.seconds_since_update)
    END as ping_output,
    -- Temperature
    CASE
        WHEN m.temperature IS NULL THEN 3
        WHEN m.temperature > 60 THEN 2
        WHEN m.temperature > 50 THEN 1
        ELSE 0
    END as temp_exit_code,
    format('Temperature: %s°C', COALESCE(m.temperature::text, 'unknown')) as temp_output,
    format('temp=%sC;50;60', COALESCE(m.temperature::text, '')) as temp_perfdata,
    -- Voltage
    CASE
        WHEN m.voltage IS NULL THEN 3
        WHEN m.voltage < 11.0 OR m.voltage > 16.0 THEN 2
        WHEN m.voltage < 11.8 OR m.voltage > 15.0 THEN 1
        ELSE 0
    END as volt_exit_code,
    format('Voltage: %sV', COALESCE(m.voltage::text, 'unknown')) as volt_output,
    format('voltage=%sV;11.8:15.0;11.0:16.0', COALESCE(m.voltage::text, '')) as volt_perfdata,
    -- CPU
    CASE
        WHEN m.cpu_load IS NULL THEN 3
        WHEN m.cpu_load > 90 THEN 2
        WHEN m.cpu_load > 75 THEN 1
        ELSE 0
    END as cpu_exit_code,
    format('CPU Load: %s%%', COALESCE(m.cpu_load::text, 'unknown')) as cpu_output,
    format('cpu=%s%%;75;90', COALESCE(m.cpu_load::text, '')) as cpu_perfdata,
    -- Satellites
    CASE
        WHEN m.satellites_used IS NULL THEN 3
        WHEN m.satellites_used < 4 THEN 2
        WHEN m.satellites_used < 8 THEN 1
        ELSE 0
    END as sat_exit_code,
    format('Satellites: %s used', COALESCE(m.satellites_used::text, 'unknown')) as sat_output,
    format('satellites=%s;8:;4:', COALESCE(m.satellites_used::text, '')) as sat_perfdata,
    -- Last update timestamp
    m.last_update
FROM station_latest_metrics m;

COMMENT ON VIEW icinga_check_data IS 'Pre-formatted check data for Icinga passive checks';

-- Grant permissions (adjust as needed for your setup)
-- GRANT SELECT ON station_latest_metrics TO grafana;
-- GRANT SELECT ON station_health_history TO grafana;
-- GRANT SELECT ON station_status_summary TO grafana;
-- GRANT SELECT ON icinga_check_data TO icinga;
