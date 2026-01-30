-- Migration: 004_health_summary_ports.sql
-- Description: Add block_health_summary table for composite overall status and port checks
-- Date: 2026-01-30
--
-- Stores the composite overall_status (computed from all metrics) and port check results.
-- Previously overall_status in checkcomm view was voltage-only; this persists the real
-- composite health status from the health parser.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/004_health_summary_ports.sql

BEGIN;

-- ============================================================================
-- HEALTH SUMMARY TABLE - Composite status and port checks
-- ============================================================================

CREATE TABLE IF NOT EXISTS block_health_summary (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    overall_status VARCHAR(20),     -- healthy, warning, critical, unknown
    ftp_open BOOLEAN,
    http_open BOOLEAN,
    control_open BOOLEAN,
    ftp_port INTEGER,
    http_port INTEGER,
    control_port INTEGER,
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_health_summary IS 'Composite health status and port check results from health parser';

-- Index for time-based queries
CREATE INDEX IF NOT EXISTS idx_health_summary_ts ON block_health_summary(ts DESC);
CREATE INDEX IF NOT EXISTS idx_health_summary_sid_ts ON block_health_summary(sid, ts DESC);

-- ============================================================================
-- UPDATE CHECKCOMM VIEW - Prefer block_health_summary overall_status
-- ============================================================================

-- Update the checkcomm view to use block_health_summary.overall_status when available,
-- falling back to voltage-only logic when no health summary exists.
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

COMMIT;
