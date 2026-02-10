-- Migration: 015_station_status.sql
-- Description: Add station_status column for lifecycle, keep health_check for monitoring mode
-- Date: 2026-02-09
--
-- Two separate concepts:
--
-- station_status (lifecycle):
--   NULL (default) = active station
--   'inactive'     = no receiver installed or temporarily out of service
--   'discontinued' = station decommissioned, no longer operational
--
-- health_check (monitoring mode):
--   NULL (default) = active, normal health checking
--   'passive'      = not directly checked (external data delivery)
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/015_health_check_status.sql

BEGIN;

-- If health_check was previously renamed to station_status, rename it back
-- and add station_status as a new column
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'stations' AND column_name = 'station_status'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'stations' AND column_name = 'health_check'
    ) THEN
        -- Previous migration renamed health_check → station_status
        -- Add health_check back as separate column
        ALTER TABLE stations ADD COLUMN health_check VARCHAR(20);
        -- Move passive values from station_status to health_check
        UPDATE stations SET health_check = 'passive', station_status = NULL
            WHERE station_status = 'passive';
    ELSIF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'stations' AND column_name = 'station_status'
    ) AND EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'stations' AND column_name = 'health_check'
    ) THEN
        -- Original state: only health_check exists
        ALTER TABLE stations ADD COLUMN station_status VARCHAR(20);
        -- Move discontinued/inactive to station_status, keep passive on health_check
        UPDATE stations SET station_status = health_check, health_check = NULL
            WHERE health_check IN ('discontinued', 'inactive');
    ELSIF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'stations' AND column_name = 'station_status'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'stations' AND column_name = 'health_check'
    ) THEN
        -- Fresh install: add both columns
        ALTER TABLE stations ADD COLUMN station_status VARCHAR(20);
        ALTER TABLE stations ADD COLUMN health_check VARCHAR(20);
    END IF;
    -- Else: both columns already exist, no schema changes needed
END $$;

COMMENT ON COLUMN stations.station_status IS 'Station lifecycle: NULL=active, inactive=no instrument, discontinued=decommissioned';
COMMENT ON COLUMN stations.health_check IS 'Monitoring mode: NULL=active, passive=external data delivery';

-- Set known values from stations.cfg
-- Note: station_status/health_check = 'active' in config means NULL in DB (active is default)
UPDATE stations SET station_status = 'discontinued' WHERE sid IN ('ASVE', 'BLAL', 'ICEB', 'ICEC');
UPDATE stations SET station_status = 'inactive' WHERE sid IN ('INGC');
UPDATE stations SET health_check = 'passive' WHERE sid IN ('KRAC', 'MYVA', 'RVIT', 'SYRF', 'THRC', 'TORK');

-- Recreate the dashboard view with both columns
DROP VIEW IF EXISTS station_dashboard_data;
CREATE VIEW station_dashboard_data AS
WITH latest_health AS (
    SELECT DISTINCT ON (sid) sid,
        ts AS health_ts,
        overall_status,
        status_details,
        ftp_open,
        http_open,
        control_open,
        ftp_port,
        http_port,
        control_port
    FROM block_health_summary
    ORDER BY sid, ts DESC
),
latest_ntrip AS (
    SELECT DISTINCT ON (sid) sid, status AS ntrip_status
    FROM (
        SELECT sid, ts, status FROM block_ntrip_server
        UNION ALL
        SELECT sid, ts, status FROM block_ntrip_client
    ) ntrip_all
    ORDER BY sid, ts DESC
),
latest_sat_breakdown AS (
    SELECT DISTINCT ON (sid) sid,
        gps AS gps_sats,
        glonass AS glonass_sats,
        galileo AS galileo_sats,
        beidou AS beidou_sats
    FROM block_satellite_tracking
    ORDER BY sid, ts DESC
)
SELECT
    -- Station identity
    m.station_id,
    m.station_name,
    s.receiver_type,
    s.antenna_type,
    s.ip_address,
    s.power_type,
    s.http_port AS station_http_port,

    -- Power
    m.voltage,
    m.power_source,
    m.power_ts,

    -- Receiver status
    m.cpu_load,
    m.temperature,
    m.uptime_seconds,
    m.rx_status,
    m.rx_error,
    m.receiver_ts,

    -- Position (coordinates only, no fix_type status)
    m.latitude AS metrics_latitude,
    m.longitude AS metrics_longitude,
    m.height AS metrics_height,
    m.satellites_used,
    m.h_accuracy,
    m.v_accuracy,
    m.position_ts,

    -- Use station coordinates for map (always populated), fall back to metrics
    COALESCE(s.latitude, m.latitude) AS latitude,
    COALESCE(s.longitude, m.longitude) AS longitude,

    -- Satellites
    m.satellites_tracked,
    m.sat_ts,
    lsb.gps_sats,
    lsb.glonass_sats,
    lsb.galileo_sats,
    lsb.beidou_sats,

    -- Disk
    m.disk_usage_pct,
    m.free_space_mb,
    m.disk_ts,

    -- Staleness (fall back to ping check time for offline stations with no metrics)
    COALESCE(m.seconds_since_update, EXTRACT(EPOCH FROM (NOW() - sc.last_check))::integer) AS seconds_since_update,
    COALESCE(m.last_update, sc.last_check) AS last_update,

    -- Connectivity (from ping checks)
    sc.is_online,
    sc.last_check,
    sc.state_since,
    sc.state_duration,
    sc.response_time_ms AS ping_response_ms,

    -- Health summary
    lh.overall_status,
    lh.status_details,
    lh.ftp_open,
    lh.http_open,
    lh.control_open,
    lh.ftp_port,
    lh.http_port AS health_http_port,
    lh.control_port,

    -- NTRIP
    ln.ntrip_status,

    -- Port status (download/health ports)
    sp.download_status,
    sp.health_status AS port_health_status,

    -- Computed: connection status
    CASE
        WHEN m.seconds_since_update IS NULL THEN 'unknown'
        WHEN m.seconds_since_update > 3600 THEN 'offline'
        WHEN m.seconds_since_update > 300 THEN 'stale'
        ELSE 'online'
    END AS connection_status,

    -- Computed: voltage status
    CASE
        WHEN m.voltage IS NULL THEN 'unknown'
        WHEN m.voltage < 11.0 OR m.voltage > 16.0 THEN 'critical'
        WHEN m.voltage < 11.8 OR m.voltage > 15.0 THEN 'warning'
        ELSE 'ok'
    END AS voltage_status,

    -- Computed: temperature status
    CASE
        WHEN m.temperature IS NULL THEN 'unknown'
        WHEN m.temperature > 60 THEN 'critical'
        WHEN m.temperature > 50 THEN 'warning'
        ELSE 'ok'
    END AS temperature_status,

    -- Computed: CPU status
    CASE
        WHEN m.cpu_load IS NULL THEN 'unknown'
        WHEN m.cpu_load > 90 THEN 'critical'
        WHEN m.cpu_load > 75 THEN 'warning'
        ELSE 'ok'
    END AS cpu_status,

    -- Computed: satellite status
    CASE
        WHEN m.satellites_used IS NULL THEN 'unknown'
        WHEN m.satellites_used < 4 THEN 'critical'
        WHEN m.satellites_used < 8 THEN 'warning'
        ELSE 'ok'
    END AS satellite_status,

    -- Station lifecycle (NULL=active, 'discontinued', 'inactive')
    s.station_status,
    -- Monitoring mode (NULL=active, 'passive')
    s.health_check

FROM station_latest_metrics m
JOIN stations s ON s.sid = m.station_id
LEFT JOIN latest_health lh ON lh.sid = m.station_id
LEFT JOIN latest_ntrip ln ON ln.sid = m.station_id
LEFT JOIN latest_sat_breakdown lsb ON lsb.sid = m.station_id
LEFT JOIN station_connectivity sc ON sc.sid = m.station_id
LEFT JOIN station_port_status sp ON sp.sid = m.station_id;

COMMENT ON VIEW station_dashboard_data IS 'Unified dashboard data for all Grafana dashboards - one row per station with all metrics, status, and connectivity';

COMMIT;
