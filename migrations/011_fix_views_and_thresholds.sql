-- Migration: 011_fix_views_and_thresholds.sql
-- Description: Fix satellites_tracked bug, add missing index, align thresholds, improve NULL handling
-- Date: 2026-02-08
--
-- Fixes:
--   1. station_latest_metrics: satellites_tracked always returned 1 (COUNT(*) bug)
--   2. block_disk_status: missing (sid, ts DESC) index for DISTINCT ON optimization
--   3. compute_hourly_aggregate: voltage/temperature thresholds misaligned with metrics.py
--   4. station_status_summary: missing upper voltage critical, NULL seconds_since_update
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/011_fix_views_and_thresholds.sql

BEGIN;

-- Drop dependent views first (they reference station_latest_metrics)
DROP VIEW IF EXISTS station_status_summary;
DROP VIEW IF EXISTS icinga_check_data;

-- ============================================================================
-- 1. FIX: station_latest_metrics - satellites_tracked always 1
-- ============================================================================
-- The latest_satellites CTE used COUNT(*) with GROUP BY (sid, ts) on a table
-- where (sid, ts) is the primary key, so count was always 1. The table has
-- a 'total' column with the actual satellite count.

DROP VIEW IF EXISTS station_latest_metrics;
CREATE VIEW station_latest_metrics AS
WITH latest_power AS (
    SELECT DISTINCT ON (sid) sid,
        ts AS power_ts,
        voltage,
        power_source
    FROM block_power_status
    ORDER BY sid, ts DESC
), latest_receiver AS (
    SELECT DISTINCT ON (sid) sid,
        ts AS receiver_ts,
        cpu_load,
        temperature,
        uptime_seconds,
        rx_status,
        rx_error
    FROM block_receiver_status
    ORDER BY sid, ts DESC
), latest_position AS (
    SELECT DISTINCT ON (sid) sid,
        ts AS position_ts,
        fix_type,
        latitude,
        longitude,
        height,
        nr_sv AS satellites_used,
        h_accuracy,
        v_accuracy
    FROM block_pvt_geodetic
    ORDER BY sid, ts DESC
), latest_satellites AS (
    SELECT DISTINCT ON (sid) sid,
        ts AS sat_ts,
        total::bigint AS satellites_tracked
    FROM block_satellite_tracking
    ORDER BY sid, ts DESC
), latest_disk AS (
    SELECT DISTINCT ON (sid) sid,
        ts AS disk_ts,
        usage_percent AS disk_usage_pct,
        (total_mb - used_mb) AS free_space_mb
    FROM block_disk_status
    ORDER BY sid, ts DESC
)
SELECT s.sid AS station_id,
    COALESCE(s.marker_name, s.sid) AS station_name,
    lp.voltage,
    lp.power_source,
    lp.power_ts,
    lr.cpu_load,
    lr.temperature,
    lr.uptime_seconds,
    lr.rx_status,
    lr.rx_error,
    lr.receiver_ts,
    lpos.fix_type,
    lpos.latitude,
    lpos.longitude,
    lpos.height,
    lpos.satellites_used,
    lpos.h_accuracy,
    lpos.v_accuracy,
    lpos.position_ts,
    ls.satellites_tracked,
    ls.sat_ts,
    ld.disk_usage_pct,
    ld.free_space_mb,
    ld.disk_ts,
    (EXTRACT(epoch FROM (now() - GREATEST(lp.power_ts, lr.receiver_ts, lpos.position_ts))))::integer AS seconds_since_update,
    GREATEST(lp.power_ts, lr.receiver_ts, lpos.position_ts) AS last_update
FROM stations s
    LEFT JOIN latest_power lp ON s.sid = lp.sid
    LEFT JOIN latest_receiver lr ON s.sid = lr.sid
    LEFT JOIN latest_position lpos ON s.sid = lpos.sid
    LEFT JOIN latest_satellites ls ON s.sid = ls.sid
    LEFT JOIN latest_disk ld ON s.sid = ld.sid;

COMMENT ON VIEW station_latest_metrics IS 'Latest metrics per station - fixed satellites_tracked to use total column';

-- ============================================================================
-- 2. FIX: block_disk_status missing index
-- ============================================================================
-- All other block tables have idx_*_sid_ts (sid, ts DESC) for DISTINCT ON.
-- block_disk_status only had the primary key, forcing full table sorts.

CREATE INDEX IF NOT EXISTS idx_disk_status_sid_ts ON block_disk_status (sid, ts DESC);

-- ============================================================================
-- 3. FIX: compute_hourly_aggregate threshold alignment
-- ============================================================================
-- Old: voltage 11.5/12.0, temperature 60/70
-- New: voltage 11.0/11.8, temperature 50/60 (matches metrics.py ThresholdConfig)

CREATE OR REPLACE FUNCTION compute_hourly_aggregate(p_sid VARCHAR, p_hour TIMESTAMPTZ)
RETURNS VOID AS $$
BEGIN
    INSERT INTO agg_hourly (
        sid, hour,
        voltage_mean, voltage_min, voltage_max,
        cpu_mean, cpu_max,
        temp_mean, temp_min, temp_max,
        sample_count, overall_status
    )
    SELECT
        p.sid,
        date_trunc('hour', p.ts),
        AVG(p.voltage), MIN(p.voltage), MAX(p.voltage),
        AVG(r.cpu_load), MAX(r.cpu_load),
        AVG(r.temperature), MIN(r.temperature), MAX(r.temperature),
        COUNT(*)::SMALLINT,
        CASE
            WHEN MIN(p.voltage) < 11.0 OR MAX(r.temperature) > 60 THEN 'critical'
            WHEN MIN(p.voltage) < 11.8 OR MAX(r.temperature) > 50 THEN 'warning'
            ELSE 'healthy'
        END
    FROM block_power_status p
    LEFT JOIN block_receiver_status r ON p.sid = r.sid AND p.ts = r.ts
    WHERE p.sid = p_sid
      AND p.ts >= p_hour
      AND p.ts < p_hour + INTERVAL '1 hour'
    GROUP BY p.sid, date_trunc('hour', p.ts)
    ON CONFLICT (sid, hour) DO UPDATE SET
        voltage_mean = EXCLUDED.voltage_mean,
        voltage_min = EXCLUDED.voltage_min,
        voltage_max = EXCLUDED.voltage_max,
        cpu_mean = EXCLUDED.cpu_mean,
        cpu_max = EXCLUDED.cpu_max,
        temp_mean = EXCLUDED.temp_mean,
        temp_min = EXCLUDED.temp_min,
        temp_max = EXCLUDED.temp_max,
        sample_count = EXCLUDED.sample_count,
        overall_status = EXCLUDED.overall_status;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION compute_hourly_aggregate(VARCHAR, TIMESTAMPTZ)
    IS 'Hourly aggregation - thresholds aligned with metrics.py (voltage 11.0/11.8, temp 50/60)';

-- ============================================================================
-- 4. FIX: station_status_summary - upper voltage critical + NULL handling
-- ============================================================================
-- Old: voltage > 15.0 was warning only, no critical for high voltage
--      NULL seconds_since_update showed as 'online' instead of 'unknown'
-- New: voltage > 16.0 = critical, NULL seconds = 'unknown'

CREATE OR REPLACE VIEW station_status_summary AS
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
FROM station_latest_metrics m;

COMMENT ON VIEW station_status_summary IS 'Station status summary - with upper voltage critical (>16V) and NULL=unknown';

-- ============================================================================
-- 5. RECREATE: icinga_check_data (depends on station_latest_metrics)
-- ============================================================================
-- Recreated as-is since its thresholds were already correct.

CREATE OR REPLACE VIEW icinga_check_data AS
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
FROM station_latest_metrics m;

COMMENT ON VIEW icinga_check_data IS 'Pre-computed Icinga check results from latest metrics';

COMMIT;
