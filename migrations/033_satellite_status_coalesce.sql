-- Migration 033: Fix satellite_status for stations with 0 tracked satellites
--
-- Problem: station_status_summary derives satellite_status from
-- block_pvt_geodetic.nr_sv (SVs used in PVT fix).  When a station has 0
-- satellites the receiver can't compute a PVT fix, so nr_sv is NULL, and the
-- view returns 'unknown' instead of 'critical'.
--
-- Fix: Use COALESCE(satellites_used, satellites_tracked) so that when PVT nr_sv
-- is NULL we fall back to the raw satellite-tracking count.  If that is also
-- NULL the status stays 'unknown'.
--
-- Affected stations: GSIG (0 satellites for 40+ days, showing 'unknown').

INSERT INTO schema_migrations (migration_name) VALUES ('033_satellite_status_coalesce');

CREATE OR REPLACE VIEW station_status_summary AS
SELECT
    station_id,
    station_name,
    CASE
        WHEN seconds_since_update IS NULL      THEN 'unknown'
        WHEN seconds_since_update > 3600       THEN 'offline'
        WHEN seconds_since_update > 300        THEN 'stale'
        ELSE 'online'
    END AS connection_status,
    CASE
        WHEN voltage IS NULL                                         THEN 'unknown'
        WHEN voltage < 11.0 OR voltage > 16.0                       THEN 'critical'
        WHEN voltage < 11.8 OR voltage > 15.0                       THEN 'warning'
        ELSE 'ok'
    END AS voltage_status,
    CASE
        WHEN temperature IS NULL        THEN 'unknown'
        WHEN temperature > 60           THEN 'critical'
        WHEN temperature > 50           THEN 'warning'
        ELSE 'ok'
    END AS temperature_status,
    CASE
        WHEN cpu_load IS NULL   THEN 'unknown'
        WHEN cpu_load > 90      THEN 'critical'
        WHEN cpu_load > 75      THEN 'warning'
        ELSE 'ok'
    END AS cpu_status,
    CASE
        WHEN COALESCE(satellites_used, satellites_tracked) IS NULL  THEN 'unknown'
        WHEN COALESCE(satellites_used, satellites_tracked) < 4      THEN 'critical'
        WHEN COALESCE(satellites_used, satellites_tracked) < 8      THEN 'warning'
        ELSE 'ok'
    END AS satellite_status,
    CASE
        WHEN fix_type IS NULL THEN 'unknown'
        WHEN fix_type = ANY(ARRAY['fixed','rtk_fixed','3d','standalone']::varchar[]) THEN 'ok'
        WHEN fix_type = ANY(ARRAY['float','rtk_float','single','dgps']::varchar[])   THEN 'warning'
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
FROM station_latest_metrics;
