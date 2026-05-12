-- Migration 039: Fix position_status for Trimble fix-type strings
--
-- The station_status_summary.position_status CASE used exact matches against a
-- short allow-list ('fixed', 'standalone', etc.) that only covered Septentrio
-- (PolaRX5) values.  Trimble receivers report fix_type as compound strings:
--
--   "WGS84,3D,Autonomous"   (59 stations) → was: CRITICAL  should be: ok
--   "WGS-84,3D,Autonomous"  (10 stations) → was: CRITICAL  should be: ok
--   "PosAutonString"         (3 stations) → was: CRITICAL  should be: ok
--   "Fixed"  (capitalised)   (4 stations) → was: CRITICAL  should be: ok
--   "WGS84,2D,Autonomous"    (1 station)  → was: CRITICAL  should be: warning
--   "PosOldString"           (1 station)  → was: CRITICAL  should be: warning
--   "WGS84,Old"              (1 station)  → was: CRITICAL  should be: warning
--
-- Fix: use lower() for the Septentrio short-strings (handles capitalisation
-- variants) and add explicit matches for all observed Trimble formats.

BEGIN;

CREATE OR REPLACE VIEW station_status_summary AS
SELECT
    station_id,
    station_name,
    CASE
        WHEN seconds_since_update IS NULL  THEN 'unknown'
        WHEN seconds_since_update > 3600   THEN 'offline'
        WHEN seconds_since_update > 300    THEN 'stale'
        ELSE                                    'online'
    END AS connection_status,
    CASE
        WHEN voltage IS NULL                                       THEN 'unknown'
        WHEN voltage < 11.0 OR voltage > 16.0                     THEN 'critical'
        WHEN voltage < 11.8 OR voltage > 15.0                     THEN 'warning'
        ELSE                                                            'ok'
    END AS voltage_status,
    CASE
        WHEN temperature IS NULL   THEN 'unknown'
        WHEN temperature > 60      THEN 'critical'
        WHEN temperature > 50      THEN 'warning'
        ELSE                            'ok'
    END AS temperature_status,
    CASE
        WHEN cpu_load IS NULL   THEN 'unknown'
        WHEN cpu_load > 90      THEN 'critical'
        WHEN cpu_load > 75      THEN 'warning'
        ELSE                         'ok'
    END AS cpu_status,
    CASE
        WHEN COALESCE(satellites_used::bigint, satellites_tracked) IS NULL THEN 'unknown'
        WHEN COALESCE(satellites_used::bigint, satellites_tracked) < 4     THEN 'critical'
        WHEN COALESCE(satellites_used::bigint, satellites_tracked) < 8     THEN 'warning'
        ELSE                                                                     'ok'
    END AS satellite_status,
    -- Trimble receivers report compound strings ("WGS84,3D,Autonomous",
    -- "WGS-84,3D,Autonomous", "PosAutonString").  Septentrio uses short
    -- canonical values ('fixed', 'standalone', …).  Use lower() for the
    -- short list so capitalisation variants like 'Fixed' are handled too.
    CASE
        WHEN fix_type IS NULL THEN 'unknown'
        WHEN lower(fix_type) = ANY(ARRAY['fixed','rtk_fixed','3d','standalone'])
             OR fix_type = ANY(ARRAY[
                 'WGS84,3D,Autonomous',
                 'WGS-84,3D,Autonomous',
                 'PosAutonString'
             ])
             THEN 'ok'
        WHEN lower(fix_type) = ANY(ARRAY['float','rtk_float','single','dgps'])
             OR fix_type = ANY(ARRAY[
                 'WGS84,2D,Autonomous',
                 'WGS-84,2D,Autonomous',
                 'PosOldString',
                 'WGS84,Old'
             ])
             THEN 'warning'
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

INSERT INTO schema_migrations (migration_name) VALUES ('039_position_status_fix_types') ON CONFLICT DO NOTHING;

COMMIT;
