-- Migration: 000_consolidated_schema.sql
-- Description: Complete from-scratch schema for GPS health database
-- Date: 2026-02-17
--
-- This file represents the final-state schema combining migrations 001-023.
-- Used for fresh installs only. On existing databases, run individual migrations.
--
-- Usage:
--   psql -h localhost -U $USER -d gps_health -f migrations/000_consolidated_schema.sql

BEGIN;

-- ============================================================================
-- SCHEMA MIGRATION TRACKING
-- ============================================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_name VARCHAR(100) PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- CORE REFERENCE TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS stations (
    sid VARCHAR(4) PRIMARY KEY,
    receiver_type VARCHAR(20),              -- nullable for external/unknown stations
    marker_name VARCHAR(60),
    marker_number VARCHAR(20),
    observer VARCHAR(60),
    agency VARCHAR(60),
    ip_address INET,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    height DOUBLE PRECISION,
    antenna_type VARCHAR(60),
    power_type VARCHAR(10),                 -- 'battery' or 'mains'
    station_status VARCHAR(20),             -- NULL=active, 'inactive', 'discontinued'
    health_check VARCHAR(20),               -- NULL=active, 'passive'
    http_port INTEGER,
    firmware_version VARCHAR(30),
    detected_model VARCHAR(60),
    serial_number VARCHAR(30),
    identity_last_checked TIMESTAMPTZ,
    station_name VARCHAR(100),              -- Full name (Icelandic place name)
    station_owner VARCHAR(60),              -- Operating organization
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE stations IS 'Station metadata — populated from config and receiver probes';

CREATE INDEX IF NOT EXISTS idx_stations_owner ON stations(station_owner);

-- ============================================================================
-- BLOCK TABLES - Each maps to a Septentrio SBF block
-- ============================================================================

-- PowerStatus block (4101)
CREATE TABLE IF NOT EXISTS block_power_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    power_source VARCHAR(10),
    voltage REAL,
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_power_status IS 'SBF Block 4101 - PowerStatus';

-- ReceiverStatus2 block (4014)
CREATE TABLE IF NOT EXISTS block_receiver_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    cpu_load REAL,
    temperature REAL,
    uptime_seconds INTEGER,
    rx_status INTEGER,
    rx_error INTEGER,
    ext_error INTEGER,
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_receiver_status IS 'SBF Block 4014 - ReceiverStatus2';

-- DiskStatus block (4105)
CREATE TABLE IF NOT EXISTS block_disk_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    used_mb INTEGER,
    total_mb INTEGER,
    usage_percent REAL,
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_disk_status IS 'SBF Block 4105 - DiskStatus';

-- PVTGeodetic2 block (4007)
CREATE TABLE IF NOT EXISTS block_pvt_geodetic (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    fix_type VARCHAR(50),
    flag_2d VARCHAR(5),
    error VARCHAR(30),
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    height REAL,
    undulation REAL,
    vn REAL,
    ve REAL,
    vu REAL,
    cog REAL,
    rx_clk_bias DOUBLE PRECISION,
    rx_clk_drift REAL,
    time_system VARCHAR(10),
    datum VARCHAR(20),
    nr_sv SMALLINT,
    h_accuracy REAL,
    v_accuracy REAL,
    latency REAL,
    raim_status VARCHAR(40),
    diff_corr_type VARCHAR(20),
    mean_corr_age REAL,
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_pvt_geodetic IS 'SBF Block 4007 - PVTGeodetic2';

-- PosCovGeodetic1 block (5905)
CREATE TABLE IF NOT EXISTS block_pos_covariance (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    fix_type VARCHAR(50),
    cov_latlat REAL,
    cov_lonlon REAL,
    cov_hgthgt REAL,
    cov_latlon REAL,
    cov_lathgt REAL,
    cov_lonhgt REAL,
    h_accuracy REAL,
    v_accuracy REAL,
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_pos_covariance IS 'SBF Block 5905 - PosCovGeodetic1';

-- SatVisibility1 block (4012) - high volume
CREATE TABLE IF NOT EXISTS block_sat_visibility (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    svid VARCHAR(10) NOT NULL,
    freq_nr SMALLINT,
    azimuth REAL,
    elevation REAL,
    rise_set VARCHAR(10),
    sat_info INTEGER,
    PRIMARY KEY (sid, ts, svid)
);
COMMENT ON TABLE block_sat_visibility IS 'SBF Block 4012 - SatVisibility1 (high volume)';

-- ChannelStatus block (4013) - aggregated satellite tracking
CREATE TABLE IF NOT EXISTS block_satellite_tracking (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    total SMALLINT,
    gps SMALLINT,
    glonass SMALLINT,
    galileo SMALLINT,
    beidou SMALLINT,
    sbas SMALLINT,
    qzss SMALLINT,
    irnss SMALLINT,
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_satellite_tracking IS 'SBF Block 4013 - ChannelStatus (aggregated by constellation)';

-- NTRIPServerStatus block (4043)
CREATE TABLE IF NOT EXISTS block_ntrip_server (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    cd_index VARCHAR(10),
    status VARCHAR(20),
    error_code VARCHAR(40),
    info INTEGER,
    tls SMALLINT,
    PRIMARY KEY (sid, ts, cd_index)
);
COMMENT ON TABLE block_ntrip_server IS 'SBF Block 4043 - NTRIPServerStatus';

-- NTRIPClientStatus block (4053)
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

-- WiFiAPStatus block (4051)
CREATE TABLE IF NOT EXISTS block_wifi_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    connected_clients SMALLINT,
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_wifi_status IS 'SBF Block 4051 - WiFiAPStatus';

-- ReceiverTime block (5914)
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
    delta_ls INTEGER,
    sync_level VARCHAR(20),
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_receiver_time IS 'SBF Block 5914 - ReceiverTime';

-- ReceiverSetup1 block (5902)
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
    delta_h REAL,
    delta_e REAL,
    delta_n REAL,
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_receiver_setup IS 'SBF Block 5902 - ReceiverSetup1 (configuration snapshots)';

-- Health summary - composite status and port checks
CREATE TABLE IF NOT EXISTS block_health_summary (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    overall_status VARCHAR(20),
    status_details TEXT,
    ftp_open BOOLEAN,
    http_open BOOLEAN,
    control_open BOOLEAN,
    ftp_port INTEGER,
    http_port INTEGER,
    control_port INTEGER,
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_health_summary IS 'Composite health status and port check results from health parser';

-- Ping status - online/offline tracking
CREATE TABLE IF NOT EXISTS block_ping_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    is_online BOOLEAN NOT NULL,
    response_time_ms REAL,
    packet_loss REAL,
    error_message TEXT,
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_ping_status IS 'Ping check results for online/offline tracking';

-- Port status - download/health port monitoring
CREATE TABLE IF NOT EXISTS block_port_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    download_port INTEGER,
    download_status VARCHAR(20),
    download_response_ms REAL,
    health_port INTEGER,
    health_status VARCHAR(20),
    health_response_ms REAL,
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_port_status IS 'Port check results for download and health monitoring';

-- Logging status - active session tracking
CREATE TABLE IF NOT EXISTS block_logging_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    active_sessions INTEGER,
    session_15s_24hr BOOLEAN,
    session_1hz_1hr BOOLEAN,
    session_status_1hr BOOLEAN,
    status VARCHAR(10),
    PRIMARY KEY (sid, ts)
);
COMMENT ON TABLE block_logging_status IS 'Active logging session status from receiver API';

-- ============================================================================
-- AGGREGATION TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS agg_hourly (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    hour TIMESTAMPTZ NOT NULL,
    voltage_mean REAL, voltage_min REAL, voltage_max REAL,
    cpu_mean REAL, cpu_max REAL,
    temp_mean REAL, temp_min REAL, temp_max REAL,
    disk_mean REAL, disk_max REAL,
    sat_mean REAL, sat_min SMALLINT, sat_max SMALLINT,
    h_accuracy_mean REAL, h_accuracy_max REAL,
    sample_count SMALLINT,
    expected_samples SMALLINT DEFAULT 60,
    overall_status VARCHAR(20),
    PRIMARY KEY (sid, hour)
);
COMMENT ON TABLE agg_hourly IS 'Hourly aggregated health metrics';

CREATE TABLE IF NOT EXISTS agg_daily (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    date DATE NOT NULL,
    voltage_mean REAL, voltage_std REAL, voltage_min REAL, voltage_max REAL,
    cpu_mean REAL, cpu_std REAL, cpu_min REAL, cpu_max REAL,
    temp_mean REAL, temp_std REAL, temp_min REAL, temp_max REAL,
    disk_mean REAL, disk_std REAL, disk_min REAL, disk_max REAL,
    sat_mean REAL, sat_std REAL, sat_min SMALLINT, sat_max SMALLINT,
    h_accuracy_mean REAL, h_accuracy_std REAL, h_accuracy_max REAL,
    sample_count INTEGER,
    expected_samples INTEGER DEFAULT 1440,
    uptime_percent REAL,
    overall_status VARCHAR(20),
    PRIMARY KEY (sid, date)
);
COMMENT ON TABLE agg_daily IS 'Daily aggregated health metrics';

-- ============================================================================
-- FILE TRACKING
-- ============================================================================

CREATE TABLE IF NOT EXISTS file_tracking (
    id SERIAL PRIMARY KEY,
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    session_type VARCHAR(20) NOT NULL,
    file_date DATE NOT NULL,
    file_hour SMALLINT,
    filename VARCHAR(100),
    file_size BIGINT,
    status VARCHAR(20) NOT NULL DEFAULT 'unknown',
    first_checked TIMESTAMPTZ,
    last_checked TIMESTAMPTZ,
    last_attempt TIMESTAMPTZ,
    download_count INTEGER DEFAULT 0,
    imported_to_db BOOLEAN DEFAULT FALSE,
    imported_at TIMESTAMPTZ,
    samples_imported INTEGER,
    import_checksum VARCHAR(64),
    json_written BOOLEAN DEFAULT FALSE,
    json_path VARCHAR(255),
    json_written_at TIMESTAMPTZ,
    last_error TEXT,
    error_count INTEGER DEFAULT 0,
    format_id VARCHAR(40),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
COMMENT ON TABLE file_tracking IS 'Track file availability and import status for downloads and health extraction';

-- ============================================================================
-- ARCHIVE FORMAT SYSTEM
-- ============================================================================

CREATE TABLE IF NOT EXISTS archive_format (
    format_id VARCHAR(40) PRIMARY KEY,
    session_type VARCHAR(20) NOT NULL,
    file_category VARCHAR(20) NOT NULL,
    receiver_type VARCHAR(20),
    frequency VARCHAR(4) NOT NULL,
    rinex_version VARCHAR(8),
    naming_convention VARCHAR(10),
    hatanaka BOOLEAN,
    compression VARCHAR(4),
    file_extension VARCHAR(20) NOT NULL,
    dir_template TEXT NOT NULL,
    filename_template TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
COMMENT ON TABLE archive_format IS 'Format definitions for archive files — drives path construction and RINEX metadata';

-- Add FK from file_tracking to archive_format
ALTER TABLE file_tracking
    ADD CONSTRAINT file_tracking_format_id_fkey
    FOREIGN KEY (format_id) REFERENCES archive_format(format_id);

-- Storage locations
CREATE TABLE IF NOT EXISTS storage_location (
    location_id VARCHAR(30) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    base_path TEXT NOT NULL,
    location_type VARCHAR(10) NOT NULL
        CHECK (location_type IN ('local', 'nfs', 'server')),
    is_primary BOOLEAN DEFAULT false,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
COMMENT ON TABLE storage_location IS 'Storage locations for archive files — environment-specific base paths';

-- File-to-location tracking (many-to-many)
CREATE TABLE IF NOT EXISTS file_locations (
    file_tracking_id INTEGER NOT NULL
        REFERENCES file_tracking(id) ON DELETE CASCADE,
    location_id VARCHAR(30) NOT NULL
        REFERENCES storage_location(location_id) ON DELETE CASCADE,
    stored_at TIMESTAMPTZ DEFAULT NOW(),
    verified_at TIMESTAMPTZ,
    file_path TEXT,
    file_size BIGINT,
    PRIMARY KEY (file_tracking_id, location_id)
);
COMMENT ON TABLE file_locations IS 'Tracks which files exist at which storage locations';

-- ============================================================================
-- STATION AREAS
-- ============================================================================

CREATE TABLE IF NOT EXISTS station_areas (
    area_id VARCHAR(30) PRIMARY KEY,
    area_name VARCHAR(100) NOT NULL,
    area_type VARCHAR(20) NOT NULL CHECK (area_type IN ('volcanic', 'regional')),
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS station_area_members (
    area_id VARCHAR(30) REFERENCES station_areas(area_id) ON DELETE CASCADE,
    sid VARCHAR(10) NOT NULL,
    PRIMARY KEY (area_id, sid)
);

-- ============================================================================
-- BACKFILL PROGRESS
-- ============================================================================

CREATE TABLE IF NOT EXISTS backfill_progress (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    session_type VARCHAR(20) NOT NULL DEFAULT 'status_1hr',
    backfill_start DATE NOT NULL,
    next_date DATE NOT NULL,
    backfill_end DATE NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    files_found INTEGER DEFAULT 0,
    files_imported INTEGER DEFAULT 0,
    files_missing INTEGER DEFAULT 0,
    files_error INTEGER DEFAULT 0,
    last_run TIMESTAMPTZ,
    last_duration_seconds REAL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (sid, session_type)
);
COMMENT ON TABLE backfill_progress IS 'Track backfill progress per station for resumable health extraction';

-- ============================================================================
-- INDEXES
-- ============================================================================

-- Block table indexes (time-based queries)
CREATE INDEX IF NOT EXISTS idx_power_status_ts ON block_power_status(ts DESC);
CREATE INDEX IF NOT EXISTS idx_power_status_sid_ts ON block_power_status(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_receiver_status_ts ON block_receiver_status(ts DESC);
CREATE INDEX IF NOT EXISTS idx_receiver_status_sid_ts ON block_receiver_status(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pvt_geodetic_ts ON block_pvt_geodetic(ts DESC);
CREATE INDEX IF NOT EXISTS idx_pvt_geodetic_sid_ts ON block_pvt_geodetic(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_sat_visibility_ts ON block_sat_visibility(ts DESC);
CREATE INDEX IF NOT EXISTS idx_satellite_tracking_ts ON block_satellite_tracking(ts DESC);
CREATE INDEX IF NOT EXISTS idx_satellite_tracking_sid_ts ON block_satellite_tracking(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_disk_status_sid_ts ON block_disk_status(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_health_summary_ts ON block_health_summary(ts DESC);
CREATE INDEX IF NOT EXISTS idx_health_summary_sid_ts ON block_health_summary(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ping_status_sid_ts ON block_ping_status(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ping_status_ts ON block_ping_status(ts DESC);
CREATE INDEX IF NOT EXISTS idx_port_status_sid_ts ON block_port_status(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_logging_status_sid_ts ON block_logging_status(sid, ts DESC);

-- Aggregation indexes
CREATE INDEX IF NOT EXISTS idx_agg_hourly_ts ON agg_hourly(hour DESC);
CREATE INDEX IF NOT EXISTS idx_agg_daily_date ON agg_daily(date DESC);
CREATE INDEX IF NOT EXISTS idx_agg_daily_status ON agg_daily(overall_status) WHERE overall_status != 'healthy';

-- File tracking indexes
CREATE UNIQUE INDEX idx_file_tracking_hourly
    ON file_tracking(sid, session_type, file_date, file_hour)
    WHERE file_hour IS NOT NULL;
CREATE UNIQUE INDEX idx_file_tracking_daily
    ON file_tracking(sid, session_type, file_date)
    WHERE file_hour IS NULL;
CREATE INDEX idx_file_tracking_status ON file_tracking(status);
CREATE INDEX idx_file_tracking_not_imported
    ON file_tracking(sid, session_type, file_date) WHERE NOT imported_to_db;
CREATE INDEX idx_file_tracking_missing
    ON file_tracking(sid, session_type, file_date) WHERE status = 'missing';
CREATE INDEX idx_file_tracking_updated ON file_tracking(updated_at DESC);
CREATE INDEX idx_file_tracking_format ON file_tracking(format_id) WHERE format_id IS NOT NULL;

-- File locations indexes
CREATE INDEX idx_file_locations_location ON file_locations(location_id);
CREATE INDEX idx_file_locations_verified ON file_locations(verified_at) WHERE verified_at IS NOT NULL;

-- Station areas indexes
CREATE INDEX IF NOT EXISTS idx_station_area_members_sid ON station_area_members(sid);
CREATE INDEX IF NOT EXISTS idx_station_areas_type ON station_areas(area_type);

-- Backfill progress
CREATE INDEX idx_backfill_progress_pending
    ON backfill_progress(last_run ASC NULLS FIRST, sid)
    WHERE status IN ('pending', 'in_progress');

-- ============================================================================
-- VIEWS (created in dependency order)
-- ============================================================================

-- 1. station_latest_metrics (no view dependencies)
CREATE VIEW station_latest_metrics AS
WITH latest_power AS (
    SELECT DISTINCT ON (sid) sid, ts AS power_ts, voltage, power_source
    FROM block_power_status ORDER BY sid, ts DESC
), latest_receiver AS (
    SELECT DISTINCT ON (sid) sid, ts AS receiver_ts, cpu_load, temperature,
           uptime_seconds, rx_status, rx_error
    FROM block_receiver_status ORDER BY sid, ts DESC
), latest_position AS (
    SELECT DISTINCT ON (sid) sid, ts AS position_ts, fix_type, latitude, longitude,
           height, nr_sv AS satellites_used, h_accuracy, v_accuracy
    FROM block_pvt_geodetic ORDER BY sid, ts DESC
), latest_satellites AS (
    SELECT DISTINCT ON (sid) sid, ts AS sat_ts, total::bigint AS satellites_tracked
    FROM block_satellite_tracking ORDER BY sid, ts DESC
), latest_disk AS (
    SELECT DISTINCT ON (sid) sid, ts AS disk_ts, usage_percent AS disk_usage_pct,
           (total_mb - used_mb) AS free_space_mb
    FROM block_disk_status ORDER BY sid, ts DESC
)
SELECT s.sid AS station_id,
    COALESCE(s.marker_name, s.sid) AS station_name,
    lp.voltage, lp.power_source, lp.power_ts,
    lr.cpu_load, lr.temperature, lr.uptime_seconds, lr.rx_status, lr.rx_error, lr.receiver_ts,
    lpos.fix_type, lpos.latitude, lpos.longitude, lpos.height,
    lpos.satellites_used, lpos.h_accuracy, lpos.v_accuracy, lpos.position_ts,
    ls.satellites_tracked, ls.sat_ts,
    ld.disk_usage_pct, ld.free_space_mb, ld.disk_ts,
    (EXTRACT(epoch FROM (now() - GREATEST(lp.power_ts, lr.receiver_ts, lpos.position_ts))))::integer AS seconds_since_update,
    GREATEST(lp.power_ts, lr.receiver_ts, lpos.position_ts) AS last_update
FROM stations s
    LEFT JOIN latest_power lp ON s.sid = lp.sid
    LEFT JOIN latest_receiver lr ON s.sid = lr.sid
    LEFT JOIN latest_position lpos ON s.sid = lpos.sid
    LEFT JOIN latest_satellites ls ON s.sid = ls.sid
    LEFT JOIN latest_disk ld ON s.sid = ld.sid;
COMMENT ON VIEW station_latest_metrics IS 'Latest metrics per station';

-- 2. station_logging_status (no view dependencies)
CREATE VIEW station_logging_status AS
SELECT DISTINCT ON (sid) sid, ts AS last_check, active_sessions,
    session_15s_24hr, session_1hz_1hr, session_status_1hr, status
FROM block_logging_status ORDER BY sid, ts DESC;
COMMENT ON VIEW station_logging_status IS 'Latest logging session status for each station';

-- 3. station_port_status (no view dependencies) — 3-check debounce
CREATE VIEW station_port_status AS
WITH latest_three AS (
    SELECT sid, ts, download_port, download_status, download_response_ms,
           health_port, health_status, health_response_ms,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_port_status
),
latest AS (SELECT * FROM latest_three WHERE rn = 1),
debounced AS (
    SELECT sid,
        BOOL_OR(download_status IN ('open', 'ok')) FILTER (WHERE rn <= 3) AS download_any_open,
        BOOL_OR(health_status IN ('open', 'ok')) FILTER (WHERE rn <= 3) AS health_any_open
    FROM latest_three WHERE rn <= 3 GROUP BY sid
),
effective AS (
    SELECT l.sid, l.ts AS last_check, l.download_port,
        CASE WHEN d.download_any_open THEN 'open'
             ELSE COALESCE(l.download_status, 'unknown') END AS download_status,
        l.download_response_ms, l.health_port,
        CASE WHEN d.health_any_open THEN 'open'
             ELSE COALESCE(l.health_status, 'unknown') END AS health_status,
        l.health_response_ms
    FROM latest l LEFT JOIN debounced d ON l.sid = d.sid
)
SELECT sid, last_check, download_port, download_status, download_response_ms,
    health_port, health_status, health_response_ms,
    CASE
        WHEN download_status IN ('open', 'ok') AND (health_status IN ('open', 'ok') OR health_status IS NULL) THEN 'active'
        WHEN download_status IN ('refused', 'timeout', 'error', 'critical') THEN download_status
        WHEN health_status IN ('refused', 'timeout', 'error', 'critical') THEN health_status
        WHEN download_status = 'warning' OR health_status = 'warning' THEN 'warning'
        ELSE 'unknown'
    END AS overall_port_status
FROM effective;
COMMENT ON VIEW station_port_status IS 'Latest port status per station (3-check debounce)';

-- 4. station_connectivity (depends on block_ping_status, block_port_status, block_ntrip_*)
CREATE VIEW station_connectivity AS
WITH latest_pings AS (
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_ping_status
),
ping_debounced AS (
    SELECT sid, bool_or(is_online) FILTER (WHERE rn <= 3) AS ping_any_ok
    FROM latest_pings WHERE rn <= 3 GROUP BY sid
),
latest_ping AS (
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message
    FROM latest_pings WHERE rn = 1
),
latest_ports AS (
    SELECT sid, ts, download_status,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_port_status
),
port_debounced AS (
    SELECT sid,
           bool_or(download_status IN ('open', 'ok')) FILTER (WHERE rn <= 3) AS port_any_ok,
           bool_and(download_status IN ('refused', 'timeout', 'unreachable', 'critical')) FILTER (WHERE rn <= 3) AS port_all_fail
    FROM latest_ports WHERE rn <= 3 GROUP BY sid
),
latest_ntrip AS (
    SELECT DISTINCT ON (sid) sid, status AS ntrip_status
    FROM (
        SELECT sid, ts, status FROM block_ntrip_server
        UNION ALL
        SELECT sid, ts, status FROM block_ntrip_client
    ) ntrip_all ORDER BY sid, ts DESC
),
ping_with_debounced AS (
    SELECT sid, ts, is_online,
           bool_or(is_online) OVER (PARTITION BY sid ORDER BY ts
               ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS debounced_online
    FROM block_ping_status
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
COMMENT ON VIEW station_connectivity IS 'Current connectivity state per station (3-check debounce, NTRIP override, connection_state)';

-- 5. station_dashboard_data (depends on station_latest_metrics, station_connectivity, station_port_status)
CREATE VIEW station_dashboard_data AS
WITH health_ranked AS (
    SELECT sid, ts, overall_status, status_details,
           ftp_open, http_open, control_open, ftp_port, http_port, control_port,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_health_summary
),
health_debounced_ports AS (
    SELECT sid,
        BOOL_OR(ftp_open) FILTER (WHERE rn <= 3) AS ftp_open_db,
        BOOL_OR(http_open) FILTER (WHERE rn <= 3) AS http_open_db,
        BOOL_OR(control_open) FILTER (WHERE rn <= 3) AS control_open_db
    FROM health_ranked WHERE rn <= 3 GROUP BY sid
),
latest_health AS (
    SELECT DISTINCT ON (r.sid)
        r.sid,
        COALESCE(good.overall_status, r.overall_status) AS overall_status,
        COALESCE(good.status_details, r.status_details) AS status_details,
        dp.ftp_open_db AS ftp_open,
        dp.http_open_db AS http_open,
        dp.control_open_db AS control_open,
        COALESCE(r.ftp_port, good.ftp_port) AS ftp_port,
        COALESCE(r.http_port, good.http_port) AS http_port,
        COALESCE(r.control_port, good.control_port) AS control_port,
        r.ts AS health_ts
    FROM health_ranked r
    JOIN health_debounced_ports dp ON dp.sid = r.sid
    LEFT JOIN LATERAL (
        SELECT overall_status, status_details, ftp_port, http_port, control_port
        FROM health_ranked g
        WHERE g.sid = r.sid AND g.rn <= 3
          AND (NOT dp.control_open_db OR g.control_open)
          AND (NOT dp.ftp_open_db OR g.ftp_open)
          AND (NOT dp.http_open_db OR g.http_open)
        ORDER BY g.ts DESC LIMIT 1
    ) good ON true
    WHERE r.rn = 1 ORDER BY r.sid
),
latest_ntrip AS (
    SELECT DISTINCT ON (sid) sid, status AS ntrip_status
    FROM (
        SELECT sid, ts, status FROM block_ntrip_server
        UNION ALL
        SELECT sid, ts, status FROM block_ntrip_client
    ) ntrip_all ORDER BY sid, ts DESC
),
latest_sat_breakdown AS (
    SELECT DISTINCT ON (sid) sid, gps AS gps_sats, glonass AS glonass_sats,
           galileo AS galileo_sats, beidou AS beidou_sats
    FROM block_satellite_tracking ORDER BY sid, ts DESC
)
SELECT
    m.station_id, m.station_name,
    s.receiver_type, s.antenna_type, s.ip_address, s.power_type,
    s.http_port AS station_http_port,
    m.voltage, m.power_source, m.power_ts,
    m.cpu_load, m.temperature, m.uptime_seconds, m.rx_status, m.rx_error, m.receiver_ts,
    m.latitude AS metrics_latitude, m.longitude AS metrics_longitude, m.height AS metrics_height,
    m.satellites_used, m.h_accuracy, m.v_accuracy, m.position_ts,
    COALESCE(s.latitude, m.latitude) AS latitude,
    COALESCE(s.longitude, m.longitude) AS longitude,
    m.satellites_tracked, m.sat_ts,
    lsb.gps_sats, lsb.glonass_sats, lsb.galileo_sats, lsb.beidou_sats,
    m.disk_usage_pct, m.free_space_mb, m.disk_ts,
    COALESCE(m.seconds_since_update, EXTRACT(EPOCH FROM (NOW() - sc.last_check))::integer) AS seconds_since_update,
    COALESCE(m.last_update, sc.last_check) AS last_update,
    sc.is_online, sc.connection_state, sc.last_check, sc.state_since, sc.state_duration,
    sc.response_time_ms AS ping_response_ms, sc.packet_loss,
    lh.overall_status, lh.status_details,
    lh.ftp_open, lh.http_open, lh.control_open,
    lh.ftp_port, lh.http_port AS health_http_port, lh.control_port,
    ln.ntrip_status,
    sp.download_status, sp.health_status AS port_health_status,
    CASE WHEN m.seconds_since_update IS NULL THEN 'unknown'
         WHEN m.seconds_since_update > 3600 THEN 'offline'
         WHEN m.seconds_since_update > 300 THEN 'stale'
         ELSE 'online' END AS connection_status,
    CASE WHEN m.voltage IS NULL THEN 'unknown'
         WHEN m.voltage < 11.0 OR m.voltage > 16.0 THEN 'critical'
         WHEN m.voltage < 11.8 OR m.voltage > 15.0 THEN 'warning'
         ELSE 'ok' END AS voltage_status,
    CASE WHEN m.temperature IS NULL THEN 'unknown'
         WHEN m.temperature > 60 THEN 'critical'
         WHEN m.temperature > 50 THEN 'warning'
         ELSE 'ok' END AS temperature_status,
    CASE WHEN m.cpu_load IS NULL THEN 'unknown'
         WHEN m.cpu_load > 90 THEN 'critical'
         WHEN m.cpu_load > 75 THEN 'warning'
         ELSE 'ok' END AS cpu_status,
    CASE WHEN m.satellites_used IS NULL THEN 'unknown'
         WHEN m.satellites_used < 4 THEN 'critical'
         WHEN m.satellites_used < 8 THEN 'warning'
         ELSE 'ok' END AS satellite_status,
    s.station_status, s.health_check
FROM station_latest_metrics m
JOIN stations s ON s.sid = m.station_id
LEFT JOIN latest_health lh ON lh.sid = m.station_id
LEFT JOIN latest_ntrip ln ON ln.sid = m.station_id
LEFT JOIN latest_sat_breakdown lsb ON lsb.sid = m.station_id
LEFT JOIN station_connectivity sc ON sc.sid = m.station_id
LEFT JOIN station_port_status sp ON sp.sid = m.station_id;
COMMENT ON VIEW station_dashboard_data IS 'Unified dashboard data with 3-check port debounce';

-- 6. station_data_flow_status (depends on station_dashboard_data, station_logging_status, file_tracking)
CREATE VIEW station_data_flow_status AS
WITH latest_raw_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking WHERE session_type = '15s_24hr' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_raw_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking WHERE session_type = '1Hz_1hr' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
latest_rinex_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking WHERE session_type = '15s_24hr_rinex' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_rinex_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking WHERE session_type = '1Hz_1hr_rinex' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
health_streak AS (
    SELECT sid,
           COALESCE(MIN(rn) FILTER (WHERE overall_status != 'critical'), 7) - 1 AS consecutive_critical
    FROM (
        SELECT sid, overall_status,
               ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
        FROM block_health_summary
    ) recent WHERE rn <= 6 GROUP BY sid
),
ever_checked AS (
    SELECT DISTINCT sid FROM block_health_summary
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
                 AND COALESCE(hs.consecutive_critical, 1) >= 2 THEN 2
            WHEN d.overall_status = 'critical' THEN 1
            ELSE -1
        END AS health_status,
        CASE
            WHEN d.station_status IS NOT NULL THEN -2
            WHEN d.receiver_type IS NULL
                 AND NOT COALESCE(l.session_15s_24hr, false)
                 AND r24.file_date IS NULL THEN -2
            WHEN ec.sid IS NULL AND r24.file_date IS NULL THEN -1
            WHEN r24.file_date IS NULL OR r24.file_date < CURRENT_DATE - 1 THEN 2
            WHEN x24.file_date IS NULL OR x24.file_date < r24.file_date THEN 1
            ELSE 0
        END AS status_24h,
        CASE
            WHEN d.station_status IS NOT NULL THEN -2
            WHEN NOT COALESCE(l.session_1hz_1hr, false) AND r1h.latest_ts IS NULL THEN -2
            WHEN ec.sid IS NULL AND r1h.latest_ts IS NULL THEN -1
            WHEN r1h.latest_ts >= NOW() - INTERVAL '1.5 hours' THEN 0
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
            WHEN NOT COALESCE(l.session_1hz_1hr, false) AND r1h.latest_ts IS NULL THEN -2
            WHEN ec.sid IS NULL AND x1h.latest_ts IS NULL AND r1h.latest_ts IS NULL THEN -1
            WHEN x1h.latest_ts >= NOW() - INTERVAL '1.5 hours' THEN 0
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
SELECT sid, health_status, status_24h, status_1hz,
    rinex_24h_status, rinex_1hz_status,
    raw_24h_date, raw_1hz_ts, rinex_24h_date, rinex_1hz_ts,
    logging_15s, logging_1hz,
    CASE
        WHEN health_status < 0 AND status_24h < 0 THEN -1
        WHEN health_status = 2 AND status_24h = 2 THEN 2
        WHEN GREATEST(
            CASE WHEN health_status < 0 THEN 0 ELSE health_status END,
            CASE WHEN status_24h < 0 THEN 0 ELSE status_24h END
        ) = 2 THEN 1
        WHEN GREATEST(
            CASE WHEN health_status < 0 THEN 0 ELSE health_status END,
            CASE WHEN status_24h < 0 THEN 0 ELSE status_24h END
        ) = 1 THEN 1
        ELSE 0
    END AS combined_status
FROM base;
COMMENT ON VIEW station_data_flow_status IS 'Time-based data flow status codes per station';

-- Other views (no complex dependencies)

CREATE OR REPLACE VIEW station_status_summary AS
SELECT station_id, station_name,
    CASE WHEN seconds_since_update IS NULL THEN 'unknown'
         WHEN seconds_since_update > 3600 THEN 'offline'
         WHEN seconds_since_update > 300 THEN 'stale'
         ELSE 'online' END AS connection_status,
    CASE WHEN voltage IS NULL THEN 'unknown'
         WHEN voltage < 11.0 OR voltage > 16.0 THEN 'critical'
         WHEN voltage < 11.8 OR voltage > 15.0 THEN 'warning'
         ELSE 'ok' END AS voltage_status,
    CASE WHEN temperature IS NULL THEN 'unknown'
         WHEN temperature > 60 THEN 'critical'
         WHEN temperature > 50 THEN 'warning'
         ELSE 'ok' END AS temperature_status,
    CASE WHEN cpu_load IS NULL THEN 'unknown'
         WHEN cpu_load > 90 THEN 'critical'
         WHEN cpu_load > 75 THEN 'warning'
         ELSE 'ok' END AS cpu_status,
    CASE WHEN satellites_used IS NULL THEN 'unknown'
         WHEN satellites_used < 4 THEN 'critical'
         WHEN satellites_used < 8 THEN 'warning'
         ELSE 'ok' END AS satellite_status,
    CASE WHEN fix_type IS NULL THEN 'unknown'
         WHEN fix_type IN ('fixed', 'rtk_fixed', '3d', 'standalone') THEN 'ok'
         WHEN fix_type IN ('float', 'rtk_float', 'single', 'dgps') THEN 'warning'
         ELSE 'critical' END AS position_status,
    voltage, temperature, cpu_load, satellites_used, fix_type,
    uptime_seconds, seconds_since_update, last_update
FROM station_latest_metrics;

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
            THEN jsonb_build_object('total', sat.total,
                'by_constellation', jsonb_build_object(
                    'GPS', sat.gps, 'GLONASS', sat.glonass,
                    'Galileo', sat.galileo, 'BeiDou', sat.beidou, 'SBAS', sat.sbas))
            WHEN pvt.nr_sv IS NOT NULL THEN jsonb_build_object('total', pvt.nr_sv)
            ELSE '{}'::jsonb END,
        'position', CASE WHEN pvt.latitude IS NOT NULL
            THEN jsonb_build_object('latitude', pvt.latitude, 'longitude', pvt.longitude,
                'height', pvt.height, 'h_accuracy_m', pvt.h_accuracy,
                'v_accuracy_m', pvt.v_accuracy, 'fix_mode', pvt.fix_type)
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

CREATE OR REPLACE VIEW icinga_check_data AS
SELECT station_id,
    CASE WHEN seconds_since_update <= 300 THEN 0 ELSE 2 END AS ping_exit_code,
    CASE WHEN seconds_since_update <= 300 THEN format('OK - %s responding', station_id)
         ELSE format('CRITICAL - %s not responding for %s seconds', station_id, seconds_since_update) END AS ping_output,
    CASE WHEN temperature IS NULL THEN 3 WHEN temperature > 60 THEN 2 WHEN temperature > 50 THEN 1 ELSE 0 END AS temp_exit_code,
    format('Temperature: %s°C', COALESCE(temperature::text, 'unknown')) AS temp_output,
    format('temp=%sC;50;60', COALESCE(temperature::text, '')) AS temp_perfdata,
    CASE WHEN voltage IS NULL THEN 3 WHEN voltage < 11.0 OR voltage > 16.0 THEN 2 WHEN voltage < 11.8 OR voltage > 15.0 THEN 1 ELSE 0 END AS volt_exit_code,
    format('Voltage: %sV', COALESCE(voltage::text, 'unknown')) AS volt_output,
    format('voltage=%sV;11.8:15.0;11.0:16.0', COALESCE(voltage::text, '')) AS volt_perfdata,
    CASE WHEN cpu_load IS NULL THEN 3 WHEN cpu_load > 90 THEN 2 WHEN cpu_load > 75 THEN 1 ELSE 0 END AS cpu_exit_code,
    format('CPU Load: %s%%', COALESCE(cpu_load::text, 'unknown')) AS cpu_output,
    format('cpu=%s%%;75;90', COALESCE(cpu_load::text, '')) AS cpu_perfdata,
    CASE WHEN satellites_used IS NULL THEN 3 WHEN satellites_used < 4 THEN 2 WHEN satellites_used < 8 THEN 1 ELSE 0 END AS sat_exit_code,
    format('Satellites: %s used', COALESCE(satellites_used::text, 'unknown')) AS sat_output,
    format('satellites=%s;8:;4:', COALESCE(satellites_used::text, '')) AS sat_perfdata,
    last_update
FROM station_latest_metrics;
COMMENT ON VIEW icinga_check_data IS 'Pre-computed Icinga check results from latest metrics';

CREATE OR REPLACE VIEW data_availability AS
SELECT sid, session_type, file_date, status, imported_to_db, samples_imported,
    CASE WHEN status = 'missing' THEN 0
         WHEN status = 'downloaded' AND samples_imported IS NOT NULL THEN
             ROUND(samples_imported::numeric /
                 CASE session_type WHEN 'status_1hr' THEN 1440
                      WHEN '1Hz_1hr' THEN 86400 ELSE 1440 END * 100, 1)
         ELSE NULL END AS completeness_pct,
    last_checked, error_count
FROM file_tracking WHERE file_hour IS NULL
ORDER BY sid, session_type, file_date DESC;
COMMENT ON VIEW data_availability IS 'Summary view of data availability per station/date';

CREATE OR REPLACE VIEW v_station_areas AS
SELECT sa.area_id, sa.area_name, sa.area_type, sa.description, sam.sid
FROM station_areas sa
JOIN station_area_members sam ON sa.area_id = sam.area_id
ORDER BY sa.area_type, sa.area_name, sam.sid;

CREATE OR REPLACE VIEW file_tracking_with_format AS
SELECT ft.id, ft.sid, ft.session_type, ft.file_date, ft.file_hour,
    ft.filename, ft.file_size, ft.status, ft.format_id,
    af.file_category, af.rinex_version, af.naming_convention,
    af.hatanaka, af.compression AS format_compression, af.file_extension,
    af.dir_template, af.filename_template,
    ft.last_checked, ft.updated_at
FROM file_tracking ft
LEFT JOIN archive_format af ON ft.format_id = af.format_id;
COMMENT ON VIEW file_tracking_with_format IS 'File tracking records joined with format metadata';

-- ============================================================================
-- FUNCTIONS
-- ============================================================================

CREATE OR REPLACE FUNCTION compute_hourly_aggregate(p_sid VARCHAR, p_hour TIMESTAMPTZ)
RETURNS VOID AS $$
BEGIN
    INSERT INTO agg_hourly (
        sid, hour, voltage_mean, voltage_min, voltage_max,
        cpu_mean, cpu_max, temp_mean, temp_min, temp_max,
        sample_count, overall_status
    )
    SELECT p.sid, date_trunc('hour', p.ts),
        AVG(p.voltage), MIN(p.voltage), MAX(p.voltage),
        AVG(r.cpu_load), MAX(r.cpu_load),
        AVG(r.temperature), MIN(r.temperature), MAX(r.temperature),
        COUNT(*)::SMALLINT,
        CASE WHEN MIN(p.voltage) < 11.0 OR MAX(r.temperature) > 60 THEN 'critical'
             WHEN MIN(p.voltage) < 11.8 OR MAX(r.temperature) > 50 THEN 'warning'
             ELSE 'healthy' END
    FROM block_power_status p
    LEFT JOIN block_receiver_status r ON p.sid = r.sid AND p.ts = r.ts
    WHERE p.sid = p_sid AND p.ts >= p_hour AND p.ts < p_hour + INTERVAL '1 hour'
    GROUP BY p.sid, date_trunc('hour', p.ts)
    ON CONFLICT (sid, hour) DO UPDATE SET
        voltage_mean = EXCLUDED.voltage_mean, voltage_min = EXCLUDED.voltage_min,
        voltage_max = EXCLUDED.voltage_max, cpu_mean = EXCLUDED.cpu_mean,
        cpu_max = EXCLUDED.cpu_max, temp_mean = EXCLUDED.temp_mean,
        temp_min = EXCLUDED.temp_min, temp_max = EXCLUDED.temp_max,
        sample_count = EXCLUDED.sample_count, overall_status = EXCLUDED.overall_status;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION is_file_missing(
    p_sid VARCHAR(4), p_session_type VARCHAR(20), p_date DATE, p_hour SMALLINT DEFAULT NULL
) RETURNS BOOLEAN AS $$
BEGIN
    IF p_hour IS NULL THEN
        RETURN EXISTS (SELECT 1 FROM file_tracking
            WHERE sid = p_sid AND session_type = p_session_type AND file_date = p_date
              AND file_hour IS NULL AND status = 'missing' AND last_checked > NOW() - INTERVAL '7 days');
    ELSE
        RETURN EXISTS (SELECT 1 FROM file_tracking
            WHERE sid = p_sid AND session_type = p_session_type AND file_date = p_date
              AND file_hour = p_hour AND status = 'missing' AND last_checked > NOW() - INTERVAL '7 days');
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION is_health_imported(
    p_sid VARCHAR(4), p_date DATE, p_checksum VARCHAR(64) DEFAULT NULL
) RETURNS BOOLEAN AS $$
BEGIN
    IF p_checksum IS NOT NULL THEN
        RETURN EXISTS (SELECT 1 FROM file_tracking
            WHERE sid = p_sid AND session_type = 'status_1hr' AND file_date = p_date
              AND file_hour IS NULL AND imported_to_db = TRUE AND import_checksum = p_checksum);
    END IF;
    RETURN EXISTS (SELECT 1 FROM file_tracking
        WHERE sid = p_sid AND session_type = 'status_1hr' AND file_date = p_date
          AND file_hour IS NULL AND imported_to_db = TRUE);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION upsert_file_tracking(
    p_sid VARCHAR(4), p_session_type VARCHAR(20), p_date DATE, p_hour SMALLINT,
    p_filename VARCHAR(100), p_status VARCHAR(20),
    p_file_size BIGINT DEFAULT NULL, p_samples INTEGER DEFAULT NULL,
    p_checksum VARCHAR(64) DEFAULT NULL, p_json_path VARCHAR(255) DEFAULT NULL,
    p_error TEXT DEFAULT NULL
) RETURNS INTEGER AS $$
DECLARE v_id INTEGER;
BEGIN
    IF p_hour IS NULL THEN
        SELECT id INTO v_id FROM file_tracking
        WHERE sid = p_sid AND session_type = p_session_type AND file_date = p_date AND file_hour IS NULL;
    ELSE
        SELECT id INTO v_id FROM file_tracking
        WHERE sid = p_sid AND session_type = p_session_type AND file_date = p_date AND file_hour = p_hour;
    END IF;
    IF v_id IS NOT NULL THEN
        UPDATE file_tracking SET
            filename = COALESCE(p_filename, filename), status = p_status,
            file_size = COALESCE(p_file_size, file_size), last_checked = NOW(),
            last_attempt = CASE WHEN p_status IN ('downloaded', 'missing', 'error') THEN NOW() ELSE last_attempt END,
            download_count = CASE WHEN p_status IN ('downloaded', 'missing') THEN download_count + 1 ELSE download_count END,
            imported_to_db = CASE WHEN p_samples IS NOT NULL THEN TRUE ELSE imported_to_db END,
            imported_at = CASE WHEN p_samples IS NOT NULL THEN NOW() ELSE imported_at END,
            samples_imported = COALESCE(p_samples, samples_imported),
            import_checksum = COALESCE(p_checksum, import_checksum),
            json_written = CASE WHEN p_json_path IS NOT NULL THEN TRUE ELSE json_written END,
            json_path = COALESCE(p_json_path, json_path),
            json_written_at = CASE WHEN p_json_path IS NOT NULL THEN NOW() ELSE json_written_at END,
            last_error = p_error,
            error_count = CASE WHEN p_error IS NOT NULL THEN error_count + 1 ELSE error_count END,
            updated_at = NOW()
        WHERE id = v_id;
    ELSE
        INSERT INTO file_tracking (
            sid, session_type, file_date, file_hour, filename, status, file_size,
            first_checked, last_checked, last_attempt, download_count,
            imported_to_db, imported_at, samples_imported, import_checksum,
            json_written, json_path, json_written_at, last_error, error_count
        ) VALUES (
            p_sid, p_session_type, p_date, p_hour, p_filename, p_status, p_file_size,
            NOW(), NOW(), NOW(), 1,
            p_samples IS NOT NULL, CASE WHEN p_samples IS NOT NULL THEN NOW() END, p_samples, p_checksum,
            p_json_path IS NOT NULL, p_json_path, CASE WHEN p_json_path IS NOT NULL THEN NOW() END,
            p_error, CASE WHEN p_error IS NOT NULL THEN 1 ELSE 0 END
        ) RETURNING id INTO v_id;
    END IF;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION get_archive_format(p_format_id VARCHAR(40))
RETURNS TABLE (
    format_id VARCHAR(40), session_type VARCHAR(20), file_category VARCHAR(20),
    receiver_type VARCHAR(20), frequency VARCHAR(4), rinex_version VARCHAR(8),
    naming_convention VARCHAR(10), hatanaka BOOLEAN, compression VARCHAR(4),
    file_extension VARCHAR(20), dir_template TEXT, filename_template TEXT
) AS $$
BEGIN
    RETURN QUERY SELECT af.format_id, af.session_type, af.file_category, af.receiver_type,
           af.frequency, af.rinex_version, af.naming_convention, af.hatanaka,
           af.compression, af.file_extension, af.dir_template, af.filename_template
    FROM archive_format af WHERE af.format_id = p_format_id;
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION format_duration(duration INTERVAL)
RETURNS TEXT AS $$
BEGIN
    IF duration >= INTERVAL '7 days' THEN
        RETURN ROUND(EXTRACT(EPOCH FROM duration) / 86400 / 7) || 'w';
    ELSIF duration >= INTERVAL '1 day' THEN
        RETURN ROUND(EXTRACT(EPOCH FROM duration) / 86400) || 'd';
    ELSIF duration >= INTERVAL '1 hour' THEN
        RETURN ROUND(EXTRACT(EPOCH FROM duration) / 3600) || 'h';
    ELSE
        RETURN ROUND(EXTRACT(EPOCH FROM duration) / 60) || 'm';
    END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ============================================================================
-- SEED DATA: Archive format definitions
-- ============================================================================

INSERT INTO archive_format (format_id, session_type, file_category, receiver_type, frequency,
    rinex_version, naming_convention, hatanaka, compression, file_extension,
    dir_template, filename_template, description)
VALUES
    ('polarx5_15s_24hr_raw', '15s_24hr', 'raw', 'polarx5', '1D',
     NULL, NULL, NULL, 'gz', '.sbf.gz',
     '%Y/#b/{station}/15s_24hr/raw/',
     '{station}%Y%m%d%H00{session_letter}.sbf.gz',
     'PolaRX5 daily 15s SBF raw file'),
    ('polarx5_1hz_1hr_raw', '1Hz_1hr', 'raw', 'polarx5', '1H',
     NULL, NULL, NULL, 'gz', '.sbf.gz',
     '%Y/#b/{station}/1Hz_1hr/raw/',
     '{station}%Y%m%d%H00{session_letter}.sbf.gz',
     'PolaRX5 hourly 1Hz SBF raw file'),
    ('polarx5_status_1hr_raw', 'status_1hr', 'raw', 'polarx5', '1H',
     NULL, NULL, NULL, 'gz', '.sbf.gz',
     '%Y/#b/{station}/status_1hr/raw/',
     '{station}%Y%m%d%H00{session_letter}.sbf.gz',
     'PolaRX5 hourly status SBF raw file'),
    ('polarx5_15s_24hr_rinex', '15s_24hr', 'rinex', 'polarx5', '1D',
     '3.04', 'short', true, 'Z', '.d.Z',
     '%Y/#b/{station}/15s_24hr/rinex/',
     '{station}#Rin2d.Z',
     'PolaRX5 daily 15s RINEX 3 Hatanaka compressed (short naming)'),
    ('polarx5_1hz_1hr_rinex', '1Hz_1hr', 'rinex', 'polarx5', '1H',
     '3.04', 'short', true, 'Z', '.d.Z',
     '%Y/#b/{station}/1Hz_1hr/rinex/',
     '{station}#Rin2d.Z',
     'PolaRX5 hourly 1Hz RINEX 3 Hatanaka compressed (short naming)')
ON CONFLICT (format_id) DO NOTHING;

-- ============================================================================
-- MARK ALL INDIVIDUAL MIGRATIONS AS APPLIED
-- ============================================================================

INSERT INTO schema_migrations (migration_name) VALUES
    ('001_block_aligned_schema'),
    ('002_file_tracking'),
    ('003_satellite_tracking'),
    ('004_health_summary_ports'),
    ('005_widen_fix_type'),
    ('006_health_summary_details'),
    ('007_stations_power_type'),
    ('008_station_areas'),
    ('009_ping_status'),
    ('010_port_status'),
    ('011_fix_views_and_thresholds'),
    ('012_station_dashboard_view'),
    ('013_receiver_identity'),
    ('014_logging_status'),
    ('015_health_check_status'),
    ('016_backfill_progress'),
    ('017_backfill_multi_session'),
    ('018_data_flow_status'),
    ('019_station_name'),
    ('020_port_debounce'),
    ('021_archive_format'),
    ('022_station_owner'),
    ('023_connection_state'),
    ('000_consolidated_schema')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
