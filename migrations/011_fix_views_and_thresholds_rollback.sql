-- Rollback: 011_fix_views_and_thresholds_rollback.sql
-- Reverts: satellites_tracked fix, disk index, threshold alignment, status summary fixes
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/011_fix_views_and_thresholds_rollback.sql

BEGIN;

-- Revert station_status_summary (remove NULL handling, remove upper voltage critical)
CREATE OR REPLACE VIEW station_status_summary AS
SELECT station_id,
    station_name,
    CASE
        WHEN seconds_since_update > 3600 THEN 'offline'
        WHEN seconds_since_update > 300 THEN 'stale'
        ELSE 'online'
    END AS connection_status,
    CASE
        WHEN voltage IS NULL THEN 'unknown'
        WHEN voltage < 11.0 THEN 'critical'
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

-- Revert compute_hourly_aggregate (old thresholds: 11.5/12.0, 60/70)
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
            WHEN MIN(p.voltage) < 11.5 OR MAX(r.temperature) > 70 THEN 'critical'
            WHEN MIN(p.voltage) < 12.0 OR MAX(r.temperature) > 60 THEN 'warning'
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

-- Drop the disk status index
DROP INDEX IF EXISTS idx_disk_status_sid_ts;

-- Revert station_latest_metrics (broken COUNT(*) version)
CREATE OR REPLACE VIEW station_latest_metrics AS
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
        count(*) AS satellites_tracked
    FROM block_satellite_tracking
    GROUP BY sid, ts
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

COMMIT;
