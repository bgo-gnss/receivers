-- Migration: Replace checkcomm table with view over block tables
-- This unifies live and historical health data into a single path

-- Step 1: Rename existing checkcomm table (keep as backup)
ALTER TABLE IF EXISTS checkcomm RENAME TO checkcomm_legacy;

-- Step 2: Create the checkcomm view that joins block tables
-- This provides backward compatibility for Grafana queries

CREATE OR REPLACE VIEW checkcomm AS
SELECT
    -- Use power status as the primary source (most common data point)
    COALESCE(p.sid, r.sid, d.sid) AS sid,
    COALESCE(p.ts, r.ts, d.ts) AS timestamp,

    -- Legacy columns
    r.temperature AS recv_temp,
    p.voltage AS recv_volt,

    -- Build recv_metrics JSONB from block tables
    jsonb_build_object(
        'power', CASE WHEN p.voltage IS NOT NULL THEN
            jsonb_build_object('voltage', p.voltage, 'unit', 'V')
        ELSE '{}'::jsonb END,
        'temperature', CASE WHEN r.temperature IS NOT NULL THEN
            jsonb_build_object('value', r.temperature, 'unit', 'C')
        ELSE '{}'::jsonb END,
        'cpu_load', CASE WHEN r.cpu_load IS NOT NULL THEN
            jsonb_build_object('percent', r.cpu_load)
        ELSE '{}'::jsonb END,
        'disk', CASE WHEN d.usage_percent IS NOT NULL THEN
            jsonb_build_object(
                'usage_percent', d.usage_percent,
                'used_mb', d.used_mb,
                'total_mb', d.total_mb
            )
        ELSE '{}'::jsonb END,
        'satellites', CASE WHEN pvt.nr_sv IS NOT NULL THEN
            jsonb_build_object('total', pvt.nr_sv)
        ELSE '{}'::jsonb END
    ) AS recv_metrics,

    -- Compute overall_status from voltage
    CASE
        WHEN p.voltage IS NULL THEN 'unknown'
        WHEN p.voltage < 11.0 OR p.voltage > 16.0 THEN 'critical'
        WHEN p.voltage < 11.8 OR p.voltage > 15.0 THEN 'warning'
        ELSE 'healthy'
    END AS overall_status,

    -- Additional fields for compatibility
    NULL::jsonb AS rout_stat,
    NULL::jsonb AS recv_stat,
    NULL::jsonb AS data_quality

FROM block_power_status p
FULL OUTER JOIN block_receiver_status r ON p.sid = r.sid AND p.ts = r.ts
FULL OUTER JOIN block_disk_status d ON COALESCE(p.sid, r.sid) = d.sid AND COALESCE(p.ts, r.ts) = d.ts
LEFT JOIN block_pvt_geodetic pvt ON COALESCE(p.sid, r.sid, d.sid) = pvt.sid AND COALESCE(p.ts, r.ts, d.ts) = pvt.ts
WHERE COALESCE(p.sid, r.sid, d.sid) IS NOT NULL;

-- Step 3: Create index on the most commonly queried block table
-- (block_power_status is typically the primary data source)
CREATE INDEX IF NOT EXISTS idx_block_power_status_sid_ts
ON block_power_status(sid, ts DESC);

CREATE INDEX IF NOT EXISTS idx_block_receiver_status_sid_ts
ON block_receiver_status(sid, ts DESC);

CREATE INDEX IF NOT EXISTS idx_block_disk_status_sid_ts
ON block_disk_status(sid, ts DESC);

-- Add comment explaining the view
COMMENT ON VIEW checkcomm IS
'Backward-compatible view over block tables. All health data should be written to block tables, this view provides the legacy checkcomm interface for Grafana.';
