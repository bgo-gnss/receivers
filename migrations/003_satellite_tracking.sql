-- Migration: 003_satellite_tracking.sql
-- Description: Add block_satellite_tracking table for constellation breakdown
-- Date: 2026-01-19
--
-- Stores aggregated satellite counts by constellation from ChannelStatus block.
-- This complements block_pvt_geodetic (which stores total nr_sv) with constellation details.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/003_satellite_tracking.sql

BEGIN;

-- ============================================================================
-- SATELLITE TRACKING TABLE - Aggregated constellation counts
-- ============================================================================

-- ChannelStatus block (4013) - Aggregated satellite tracking per epoch
CREATE TABLE IF NOT EXISTS block_satellite_tracking (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,           -- Time of Week [s]
    wnc INTEGER,                    -- GPS week number
    total SMALLINT,                 -- Total satellites tracked
    gps SMALLINT,                   -- GPS satellites (SVID 1-37)
    glonass SMALLINT,               -- GLONASS satellites (SVID 38-61)
    galileo SMALLINT,               -- Galileo satellites (SVID 71-102)
    beidou SMALLINT,                -- BeiDou satellites (SVID 141-180)
    sbas SMALLINT,                  -- SBAS satellites (SVID 120-140)
    qzss SMALLINT,                  -- QZSS satellites (SVID 181-187)
    irnss SMALLINT,                 -- IRNSS/NavIC satellites (SVID 191-197)
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_satellite_tracking IS 'SBF Block 4013 - ChannelStatus (aggregated by constellation)';

-- Index for time-based queries
CREATE INDEX IF NOT EXISTS idx_satellite_tracking_ts ON block_satellite_tracking(ts DESC);
CREATE INDEX IF NOT EXISTS idx_satellite_tracking_sid_ts ON block_satellite_tracking(sid, ts DESC);

-- ============================================================================
-- UPDATE CHECKCOMM VIEW - Add satellite constellation info
-- ============================================================================

-- Update the checkcomm backward compatibility view to include satellite details
-- Preserves existing column structure while adding satellite constellation data
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
WHERE COALESCE(p.sid, r.sid, d.sid) IS NOT NULL;

COMMENT ON VIEW checkcomm IS 'Backward compatibility view with satellite constellation breakdown';

COMMIT;
