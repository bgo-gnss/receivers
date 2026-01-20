-- Rollback: 003_satellite_tracking_rollback.sql
-- Description: Remove block_satellite_tracking table and revert checkcomm view
-- Date: 2026-01-19

BEGIN;

-- Restore original checkcomm view (without satellite tracking join)
CREATE OR REPLACE VIEW checkcomm AS
SELECT
    row_number() OVER (ORDER BY p.sid, p.ts) AS id,
    p.sid,
    p.ts AS timestamp,
    r.temperature AS recv_temp,
    p.voltage AS recv_volt,
    jsonb_build_object(
        'status', CASE WHEN p.voltage IS NOT NULL THEN 'ok' ELSE 'unknown' END
    ) AS rout_stat,
    jsonb_build_object(
        'cpu_load', r.cpu_load,
        'temperature', r.temperature,
        'uptime_seconds', r.uptime_seconds
    ) AS recv_stat,
    jsonb_build_object(
        'voltage', p.voltage,
        'power_source', p.power_source
    ) AS recv_metrics,
    jsonb_build_object(
        'fix_type', pvt.fix_type,
        'nr_sv', pvt.nr_sv,
        'h_accuracy', pvt.h_accuracy,
        'v_accuracy', pvt.v_accuracy
    ) AS data_quality,
    CASE
        WHEN p.voltage IS NULL OR r.temperature IS NULL THEN 'unknown'
        WHEN p.voltage < 11.5 OR r.temperature > 70 THEN 'critical'
        WHEN p.voltage < 12.0 OR r.temperature > 60 THEN 'warning'
        ELSE 'healthy'
    END AS overall_status
FROM block_power_status p
LEFT JOIN block_receiver_status r ON p.sid = r.sid AND p.ts = r.ts
LEFT JOIN block_pvt_geodetic pvt ON p.sid = pvt.sid AND p.ts = pvt.ts;

-- Drop indexes
DROP INDEX IF EXISTS idx_satellite_tracking_ts;
DROP INDEX IF EXISTS idx_satellite_tracking_sid_ts;

-- Drop table
DROP TABLE IF EXISTS block_satellite_tracking;

COMMIT;
