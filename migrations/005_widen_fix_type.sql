-- Migration 005: Widen fix_type columns for Trimble qualifier strings
--
-- Trimble receivers return qualifier strings like "WGS84,3D,Autonomous" (19 chars)
-- which exceeds the original VARCHAR(15) designed for PolaRX5 values (Fixed, Float, etc.)
--
-- Drops all dependent views with CASCADE, widens columns, then recreates views.

BEGIN;

-- Drop all views that depend on block_pvt_geodetic.fix_type
DROP VIEW IF EXISTS checkcomm_legacy CASCADE;
DROP VIEW IF EXISTS checkcomm CASCADE;
DROP VIEW IF EXISTS station_latest_metrics CASCADE;
DROP VIEW IF EXISTS station_health_history CASCADE;

-- Widen the columns
ALTER TABLE block_pvt_geodetic
    ALTER COLUMN fix_type TYPE VARCHAR(50);

ALTER TABLE block_pos_covariance
    ALTER COLUMN fix_type TYPE VARCHAR(50);

-- ============================================================================
-- Recreate checkcomm view (from migration 004)
-- ============================================================================
CREATE OR REPLACE VIEW checkcomm AS
SELECT
    COALESCE(p.sid, r.sid, d.sid) AS sid,
    COALESCE(p.ts, r.ts, d.ts) AS timestamp,
    r.temperature AS recv_temp,
    p.voltage AS recv_volt,
    jsonb_build_object(
        'power', CASE WHEN p.voltage IS NOT NULL
            THEN jsonb_build_object('voltage', p.voltage, 'unit', 'V')
            ELSE '{}'::jsonb END,
        'temperature', CASE WHEN r.temperature IS NOT NULL
            THEN jsonb_build_object('value', r.temperature, 'unit', 'C')
            ELSE '{}'::jsonb END,
        'cpu_load', CASE WHEN r.cpu_load IS NOT NULL
            THEN jsonb_build_object('percent', r.cpu_load)
            ELSE '{}'::jsonb END,
        'disk', CASE WHEN d.usage_percent IS NOT NULL
            THEN jsonb_build_object('usage_percent', d.usage_percent, 'used_mb', d.used_mb, 'total_mb', d.total_mb)
            ELSE '{}'::jsonb END,
        'satellites', CASE WHEN sat.total IS NOT NULL
            THEN jsonb_build_object(
                'total', sat.total,
                'by_constellation', jsonb_build_object(
                    'GPS', sat.gps,
                    'GLONASS', sat.glonass,
                    'Galileo', sat.galileo,
                    'BeiDou', sat.beidou,
                    'SBAS', sat.sbas
                )
            )
            WHEN pvt.nr_sv IS NOT NULL
            THEN jsonb_build_object('total', pvt.nr_sv)
            ELSE '{}'::jsonb END,
        'position', CASE WHEN pvt.latitude IS NOT NULL
            THEN jsonb_build_object(
                'latitude', pvt.latitude,
                'longitude', pvt.longitude,
                'height', pvt.height,
                'h_accuracy_m', pvt.h_accuracy,
                'v_accuracy_m', pvt.v_accuracy,
                'fix_mode', pvt.fix_type
            )
            ELSE '{}'::jsonb END
    ) AS recv_metrics,
    CASE
        WHEN hs.overall_status IS NOT NULL THEN hs.overall_status::text
        WHEN p.voltage IS NULL THEN 'unknown'
        WHEN p.voltage < 11.0 OR p.voltage > 16.0 THEN 'critical'
        WHEN p.voltage < 11.8 OR p.voltage > 15.0 THEN 'warning'
        ELSE 'healthy'
    END AS overall_status,
    NULL::jsonb AS rout_stat,
    NULL::jsonb AS recv_stat,
    NULL::jsonb AS data_quality
FROM block_power_status p
FULL JOIN block_receiver_status r ON p.sid = r.sid AND p.ts = r.ts
FULL JOIN block_disk_status d ON COALESCE(p.sid, r.sid) = d.sid AND COALESCE(p.ts, r.ts) = d.ts
LEFT JOIN block_pvt_geodetic pvt ON COALESCE(p.sid, r.sid, d.sid) = pvt.sid AND COALESCE(p.ts, r.ts, d.ts) = pvt.ts
LEFT JOIN block_satellite_tracking sat ON COALESCE(p.sid, r.sid, d.sid) = sat.sid AND COALESCE(p.ts, r.ts, d.ts) = sat.ts
LEFT JOIN block_health_summary hs ON COALESCE(p.sid, r.sid, d.sid) = hs.sid AND COALESCE(p.ts, r.ts, d.ts) = hs.ts
WHERE COALESCE(p.sid, r.sid, d.sid) IS NOT NULL;

COMMENT ON VIEW checkcomm IS 'Backward compatibility view with composite health status and satellite data';

-- ============================================================================
-- Recreate health views (from sql/health_views.sql)
-- ============================================================================

CREATE VIEW station_latest_metrics AS
WITH latest_power AS (
    SELECT DISTINCT ON (sid) sid, ts as power_ts, voltage, power_source
    FROM block_power_status ORDER BY sid, ts DESC
),
latest_receiver AS (
    SELECT DISTINCT ON (sid) sid, ts as receiver_ts, cpu_load, temperature,
        uptime_seconds, rx_status, rx_error
    FROM block_receiver_status ORDER BY sid, ts DESC
),
latest_position AS (
    SELECT DISTINCT ON (sid) sid, ts as position_ts, fix_type, latitude, longitude,
        height, nr_sv as satellites_used, h_accuracy, v_accuracy
    FROM block_pvt_geodetic ORDER BY sid, ts DESC
),
latest_satellites AS (
    SELECT DISTINCT ON (sid) sid, ts as sat_ts, COUNT(*) as satellites_tracked
    FROM block_satellite_tracking GROUP BY sid, ts ORDER BY sid, ts DESC
),
latest_disk AS (
    SELECT DISTINCT ON (sid) sid, ts as disk_ts, usage_percent as disk_usage_pct,
        (total_mb - used_mb) as free_space_mb
    FROM block_disk_status ORDER BY sid, ts DESC
)
SELECT
    s.sid as station_id, COALESCE(s.marker_name, s.sid) as station_name,
    lp.voltage, lp.power_source, lp.power_ts,
    lr.cpu_load, lr.temperature, lr.uptime_seconds, lr.rx_status, lr.rx_error, lr.receiver_ts,
    lpos.fix_type, lpos.latitude, lpos.longitude, lpos.height,
    lpos.satellites_used, lpos.h_accuracy, lpos.v_accuracy, lpos.position_ts,
    ls.satellites_tracked, ls.sat_ts,
    ld.disk_usage_pct, ld.free_space_mb, ld.disk_ts,
    EXTRACT(EPOCH FROM (NOW() - GREATEST(lp.power_ts, lr.receiver_ts, lpos.position_ts)))::int as seconds_since_update,
    GREATEST(lp.power_ts, lr.receiver_ts, lpos.position_ts) as last_update
FROM stations s
LEFT JOIN latest_power lp ON s.sid = lp.sid
LEFT JOIN latest_receiver lr ON s.sid = lr.sid
LEFT JOIN latest_position lpos ON s.sid = lpos.sid
LEFT JOIN latest_satellites ls ON s.sid = ls.sid
LEFT JOIN latest_disk ld ON s.sid = ld.sid;

COMMENT ON VIEW station_latest_metrics IS 'Latest metrics for each station - single row per station for Icinga checks';

CREATE VIEW station_health_history AS
SELECT
    r.sid as station_id, r.ts as timestamp,
    r.cpu_load, r.temperature, r.uptime_seconds,
    p.voltage,
    pos.fix_type, pos.nr_sv as satellites_used, pos.h_accuracy, pos.v_accuracy
FROM block_receiver_status r
LEFT JOIN block_power_status p ON r.sid = p.sid AND r.ts = p.ts
LEFT JOIN block_pvt_geodetic pos ON r.sid = pos.sid AND r.ts = pos.ts
ORDER BY r.ts DESC;

COMMENT ON VIEW station_health_history IS 'Time series of health metrics for Grafana graphs';

CREATE VIEW station_status_summary AS
SELECT
    m.station_id, m.station_name,
    CASE WHEN m.seconds_since_update > 3600 THEN 'offline'
         WHEN m.seconds_since_update > 300 THEN 'stale' ELSE 'online' END as connection_status,
    CASE WHEN m.voltage IS NULL THEN 'unknown'
         WHEN m.voltage < 11.0 THEN 'critical'
         WHEN m.voltage < 11.8 OR m.voltage > 15.0 THEN 'warning' ELSE 'ok' END as voltage_status,
    CASE WHEN m.temperature IS NULL THEN 'unknown'
         WHEN m.temperature > 60 THEN 'critical'
         WHEN m.temperature > 50 THEN 'warning' ELSE 'ok' END as temperature_status,
    CASE WHEN m.cpu_load IS NULL THEN 'unknown'
         WHEN m.cpu_load > 90 THEN 'critical'
         WHEN m.cpu_load > 75 THEN 'warning' ELSE 'ok' END as cpu_status,
    CASE WHEN m.satellites_used IS NULL THEN 'unknown'
         WHEN m.satellites_used < 4 THEN 'critical'
         WHEN m.satellites_used < 8 THEN 'warning' ELSE 'ok' END as satellite_status,
    CASE WHEN m.fix_type IS NULL THEN 'unknown'
         WHEN m.fix_type IN ('fixed', 'rtk_fixed', '3d', 'standalone') THEN 'ok'
         WHEN m.fix_type IN ('float', 'rtk_float', 'single', 'dgps') THEN 'warning'
         ELSE 'critical' END as position_status,
    m.voltage, m.temperature, m.cpu_load, m.satellites_used, m.fix_type,
    m.uptime_seconds, m.seconds_since_update, m.last_update
FROM station_latest_metrics m;

COMMENT ON VIEW station_status_summary IS 'Summary of status for all stations with status indicators';

CREATE VIEW icinga_check_data AS
SELECT
    m.station_id,
    CASE WHEN m.seconds_since_update <= 300 THEN 0 ELSE 2 END as ping_exit_code,
    CASE WHEN m.seconds_since_update <= 300 THEN format('OK - %s responding', m.station_id)
         ELSE format('CRITICAL - %s not responding for %s seconds', m.station_id, m.seconds_since_update) END as ping_output,
    CASE WHEN m.temperature IS NULL THEN 3 WHEN m.temperature > 60 THEN 2
         WHEN m.temperature > 50 THEN 1 ELSE 0 END as temp_exit_code,
    format('Temperature: %s°C', COALESCE(m.temperature::text, 'unknown')) as temp_output,
    format('temp=%sC;50;60', COALESCE(m.temperature::text, '')) as temp_perfdata,
    CASE WHEN m.voltage IS NULL THEN 3 WHEN m.voltage < 11.0 OR m.voltage > 16.0 THEN 2
         WHEN m.voltage < 11.8 OR m.voltage > 15.0 THEN 1 ELSE 0 END as volt_exit_code,
    format('Voltage: %sV', COALESCE(m.voltage::text, 'unknown')) as volt_output,
    format('voltage=%sV;11.8:15.0;11.0:16.0', COALESCE(m.voltage::text, '')) as volt_perfdata,
    CASE WHEN m.cpu_load IS NULL THEN 3 WHEN m.cpu_load > 90 THEN 2
         WHEN m.cpu_load > 75 THEN 1 ELSE 0 END as cpu_exit_code,
    format('CPU Load: %s%%', COALESCE(m.cpu_load::text, 'unknown')) as cpu_output,
    format('cpu=%s%%;75;90', COALESCE(m.cpu_load::text, '')) as cpu_perfdata,
    CASE WHEN m.satellites_used IS NULL THEN 3 WHEN m.satellites_used < 4 THEN 2
         WHEN m.satellites_used < 8 THEN 1 ELSE 0 END as sat_exit_code,
    format('Satellites: %s used', COALESCE(m.satellites_used::text, 'unknown')) as sat_output,
    format('satellites=%s;8:;4:', COALESCE(m.satellites_used::text, '')) as sat_perfdata,
    m.last_update
FROM station_latest_metrics m;

COMMENT ON VIEW icinga_check_data IS 'Pre-formatted check data for Icinga passive checks';

COMMIT;
