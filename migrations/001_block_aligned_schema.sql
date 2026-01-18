-- Migration: 001_block_aligned_schema.sql
-- Description: Create block-aligned database schema for GPS health data
-- Date: 2026-01-18
--
-- This schema mirrors the Septentrio SBF block structure for:
--   - Easy extensibility (new block = new table)
--   - Clear data lineage (table name = block name)
--   - Alignment with bin2asc extraction output
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/001_block_aligned_schema.sql

BEGIN;

-- ============================================================================
-- CORE REFERENCE TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS stations (
    sid VARCHAR(4) PRIMARY KEY,
    receiver_type VARCHAR(20) NOT NULL,
    marker_name VARCHAR(60),
    marker_number VARCHAR(20),
    observer VARCHAR(60),
    agency VARCHAR(60),
    ip_address INET,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    height DOUBLE PRECISION,
    antenna_type VARCHAR(60),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE stations IS 'Station metadata, populated from ReceiverSetup1 block';

-- ============================================================================
-- BLOCK TABLES - Each maps to a Septentrio SBF block
-- ============================================================================

-- PowerStatus block (4101) - Power supply information
CREATE TABLE IF NOT EXISTS block_power_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,           -- Time of Week [s]
    wnc INTEGER,                    -- GPS week number
    power_source VARCHAR(10),       -- Vin, Vbat, etc.
    voltage REAL,                   -- Input voltage [V]
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_power_status IS 'SBF Block 4101 - PowerStatus';

-- ReceiverStatus2 block (4014) - Receiver status and health
CREATE TABLE IF NOT EXISTS block_receiver_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    cpu_load REAL,                  -- CPU load [%]
    temperature REAL,               -- Internal temperature [°C]
    uptime_seconds INTEGER,         -- Receiver uptime [s]
    rx_status INTEGER,              -- Receiver status flags
    rx_error INTEGER,               -- Receiver error flags
    ext_error INTEGER,              -- External error flags
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_receiver_status IS 'SBF Block 4014 - ReceiverStatus2';

-- DiskStatus block (4105) - Internal storage status
CREATE TABLE IF NOT EXISTS block_disk_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    used_mb INTEGER,                -- Used disk space [MB]
    total_mb INTEGER,               -- Total disk space [MB]
    usage_percent REAL,             -- Calculated usage [%]
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_disk_status IS 'SBF Block 4105 - DiskStatus';

-- PVTGeodetic2 block (4007) - Position, Velocity, Time solution
CREATE TABLE IF NOT EXISTS block_pvt_geodetic (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    fix_type VARCHAR(15),           -- Fixed, Float, RTKFixed, etc.
    flag_2d VARCHAR(5),             -- 2D or 3D
    error VARCHAR(30),              -- Error status
    latitude DOUBLE PRECISION,      -- Latitude [rad]
    longitude DOUBLE PRECISION,     -- Longitude [rad]
    height REAL,                    -- Ellipsoidal height [m]
    undulation REAL,                -- Geoid undulation [m]
    vn REAL,                        -- North velocity [m/s]
    ve REAL,                        -- East velocity [m/s]
    vu REAL,                        -- Up velocity [m/s]
    cog REAL,                       -- Course over ground [deg]
    rx_clk_bias DOUBLE PRECISION,   -- Receiver clock bias [ms]
    rx_clk_drift REAL,              -- Receiver clock drift [ppm]
    time_system VARCHAR(10),        -- GPS, Galileo, etc.
    datum VARCHAR(20),              -- WGS84/ITRS, etc.
    nr_sv SMALLINT,                 -- Number of satellites used
    h_accuracy REAL,                -- Horizontal accuracy [m]
    v_accuracy REAL,                -- Vertical accuracy [m]
    latency REAL,                   -- Solution latency [s]
    raim_status VARCHAR(40),        -- RAIM integrity status
    diff_corr_type VARCHAR(20),     -- Differential correction type
    mean_corr_age REAL,             -- Mean correction age [s]
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_pvt_geodetic IS 'SBF Block 4007 - PVTGeodetic2';

-- PosCovGeodetic1 block (5905) - Position covariance matrix
CREATE TABLE IF NOT EXISTS block_pos_covariance (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    fix_type VARCHAR(15),
    cov_latlat REAL,                -- Latitude variance [m²]
    cov_lonlon REAL,                -- Longitude variance [m²]
    cov_hgthgt REAL,                -- Height variance [m²]
    cov_latlon REAL,                -- Lat-Lon covariance [m²]
    cov_lathgt REAL,                -- Lat-Height covariance [m²]
    cov_lonhgt REAL,                -- Lon-Height covariance [m²]
    h_accuracy REAL,                -- Horizontal accuracy (2D RMS) [m]
    v_accuracy REAL,                -- Vertical accuracy (1D RMS) [m]
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_pos_covariance IS 'SBF Block 5905 - PosCovGeodetic1';

-- SatVisibility1 block (4012) - Satellite visibility per epoch
-- Note: High volume table - one row per satellite per epoch
CREATE TABLE IF NOT EXISTS block_sat_visibility (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    svid VARCHAR(10) NOT NULL,      -- Satellite ID (G01, R05, E12, C01, etc.)
    freq_nr SMALLINT,               -- GLONASS frequency number
    azimuth REAL,                   -- Azimuth [deg]
    elevation REAL,                 -- Elevation [deg]
    rise_set VARCHAR(10),           -- Rising/Setting indicator
    sat_info INTEGER,               -- Satellite info flags
    PRIMARY KEY (sid, ts, svid)
);

COMMENT ON TABLE block_sat_visibility IS 'SBF Block 4012 - SatVisibility1 (high volume)';

-- NTRIPServerStatus block (4043) - NTRIP caster connection status
CREATE TABLE IF NOT EXISTS block_ntrip_server (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    cd_index VARCHAR(10),           -- Connection descriptor (NTR1, NTR2, etc.)
    status VARCHAR(20),             -- Running, Stopped, Error
    error_code VARCHAR(40),         -- Error description
    info INTEGER,                   -- Additional info flags
    tls SMALLINT,                   -- TLS enabled flag
    PRIMARY KEY (sid, ts, cd_index)
);

COMMENT ON TABLE block_ntrip_server IS 'SBF Block 4043 - NTRIPServerStatus';

-- NTRIPClientStatus block (4053) - NTRIP client connection status
CREATE TABLE IF NOT EXISTS block_ntrip_client (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    cd_index VARCHAR(10),
    status VARCHAR(20),
    error_code VARCHAR(40),
    PRIMARY KEY (sid, ts, cd_index)
);

COMMENT ON TABLE block_ntrip_client IS 'SBF Block 4053 - NTRIPClientStatus';

-- WiFiAPStatus block (4051) - WiFi access point status
CREATE TABLE IF NOT EXISTS block_wifi_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    connected_clients SMALLINT,     -- Number of connected WiFi clients
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_wifi_status IS 'SBF Block 4051 - WiFiAPStatus';

-- ReceiverTime block (5914) - Receiver time information
CREATE TABLE IF NOT EXISTS block_receiver_time (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    utc_year SMALLINT,
    utc_month SMALLINT,
    utc_day SMALLINT,
    utc_hour SMALLINT,
    utc_minute SMALLINT,
    utc_second REAL,
    delta_ls INTEGER,               -- Leap seconds
    sync_level VARCHAR(20),         -- Time synchronization level
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_receiver_time IS 'SBF Block 5914 - ReceiverTime';

-- ReceiverSetup1 block (5902) - Receiver configuration (low frequency)
CREATE TABLE IF NOT EXISTS block_receiver_setup (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    marker_name VARCHAR(60),
    marker_number VARCHAR(20),
    observer VARCHAR(60),
    agency VARCHAR(60),
    rx_serial_number VARCHAR(20),
    rx_name VARCHAR(30),
    rx_version VARCHAR(30),
    ant_serial_number VARCHAR(20),
    ant_type VARCHAR(60),
    delta_h REAL,                   -- Antenna height offset [m]
    delta_e REAL,                   -- Antenna east offset [m]
    delta_n REAL,                   -- Antenna north offset [m]
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_receiver_setup IS 'SBF Block 5902 - ReceiverSetup1 (configuration snapshots)';

-- ============================================================================
-- AGGREGATION TABLES - Computed from block tables
-- ============================================================================

-- Hourly aggregates
CREATE TABLE IF NOT EXISTS agg_hourly (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    hour TIMESTAMPTZ NOT NULL,      -- Truncated to hour
    -- Power
    voltage_mean REAL,
    voltage_min REAL,
    voltage_max REAL,
    -- CPU/Thermal
    cpu_mean REAL,
    cpu_max REAL,
    temp_mean REAL,
    temp_min REAL,
    temp_max REAL,
    -- Disk
    disk_mean REAL,
    disk_max REAL,
    -- Satellites
    sat_mean REAL,
    sat_min SMALLINT,
    sat_max SMALLINT,
    -- Position quality
    h_accuracy_mean REAL,
    h_accuracy_max REAL,
    -- Metadata
    sample_count SMALLINT,
    expected_samples SMALLINT DEFAULT 60,
    overall_status VARCHAR(20),
    PRIMARY KEY (sid, hour)
);

COMMENT ON TABLE agg_hourly IS 'Hourly aggregated health metrics';

-- Daily aggregates
CREATE TABLE IF NOT EXISTS agg_daily (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    date DATE NOT NULL,
    -- Power
    voltage_mean REAL,
    voltage_std REAL,
    voltage_min REAL,
    voltage_max REAL,
    -- CPU/Thermal
    cpu_mean REAL,
    cpu_std REAL,
    cpu_min REAL,
    cpu_max REAL,
    temp_mean REAL,
    temp_std REAL,
    temp_min REAL,
    temp_max REAL,
    -- Disk
    disk_mean REAL,
    disk_std REAL,
    disk_min REAL,
    disk_max REAL,
    -- Satellites
    sat_mean REAL,
    sat_std REAL,
    sat_min SMALLINT,
    sat_max SMALLINT,
    -- Position quality
    h_accuracy_mean REAL,
    h_accuracy_std REAL,
    h_accuracy_max REAL,
    -- Metadata
    sample_count INTEGER,
    expected_samples INTEGER DEFAULT 1440,
    uptime_percent REAL,            -- sample_count / expected_samples * 100
    overall_status VARCHAR(20),
    PRIMARY KEY (sid, date)
);

COMMENT ON TABLE agg_daily IS 'Daily aggregated health metrics';

-- ============================================================================
-- INDEXES - Optimized for common query patterns
-- ============================================================================

-- Time-based queries (most common)
CREATE INDEX IF NOT EXISTS idx_power_status_ts ON block_power_status(ts DESC);
CREATE INDEX IF NOT EXISTS idx_receiver_status_ts ON block_receiver_status(ts DESC);
CREATE INDEX IF NOT EXISTS idx_pvt_geodetic_ts ON block_pvt_geodetic(ts DESC);
CREATE INDEX IF NOT EXISTS idx_sat_visibility_ts ON block_sat_visibility(ts DESC);

-- Station + time range queries
CREATE INDEX IF NOT EXISTS idx_power_status_sid_ts ON block_power_status(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_receiver_status_sid_ts ON block_receiver_status(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pvt_geodetic_sid_ts ON block_pvt_geodetic(sid, ts DESC);

-- Aggregation table indexes
CREATE INDEX IF NOT EXISTS idx_agg_hourly_ts ON agg_hourly(hour DESC);
CREATE INDEX IF NOT EXISTS idx_agg_daily_date ON agg_daily(date DESC);

-- Status queries
CREATE INDEX IF NOT EXISTS idx_agg_daily_status ON agg_daily(overall_status) WHERE overall_status != 'healthy';

-- ============================================================================
-- BACKWARD COMPATIBILITY VIEW
-- ============================================================================

-- checkcomm view - maintains compatibility with existing code
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

COMMENT ON VIEW checkcomm IS 'Backward compatibility view for legacy code';

-- ============================================================================
-- HELPER FUNCTIONS
-- ============================================================================

-- Function to compute hourly aggregates for a station/hour
CREATE OR REPLACE FUNCTION compute_hourly_aggregate(
    p_sid VARCHAR(4),
    p_hour TIMESTAMPTZ
) RETURNS VOID AS $$
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

COMMENT ON FUNCTION compute_hourly_aggregate IS 'Compute or update hourly aggregate for a station';

COMMIT;

-- ============================================================================
-- POST-MIGRATION: Drop old table if exists (run manually after verification)
-- ============================================================================
--
-- After verifying the new schema works:
--   DROP TABLE IF EXISTS checkcomm_old;
--   ALTER TABLE checkcomm RENAME TO checkcomm_old;  -- backup
--   -- The view 'checkcomm' now provides backward compatibility
