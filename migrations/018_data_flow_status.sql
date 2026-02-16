-- Migration: 018_data_flow_status.sql
-- Description: View computing time-based data flow status codes per station
-- Date: 2026-02-10
-- Updated: 2026-02-12  Added health_status and combined_status columns
-- Updated: 2026-02-13  Evidence-based session detection (file_tracking overrides probe)
-- Updated: 2026-02-16  Debounce health_status: soften critical→warning when
--                       debounced-online and issues are connectivity-only
--
-- Joins file_tracking + station_logging_status + station_dashboard_data to produce:
--   health_status:    hardware/connectivity health (from overall_status)
--   status_24h:       data flow for 15s_24hr (raw + RINEX progression)
--   combined_status:  derived from health_status + status_24h
--   status_1hz:       data flow for 1Hz_1hr
--   rinex_24h_status / rinex_1hz_status: RINEX-specific for detail panel
--
-- Session detection is EVIDENCE-BASED:
--   A station "logs 15s_24hr" if:
--     1. The health probe says so (PolaRX5 TCP logging check), OR
--     2. file_tracking contains actual 15s_24hr files for that station, OR
--     3. The station has a known receiver_type (all types support 15s downloads)
--   This fixes Trimble/G10 receivers where probes can't detect session names.
--
-- Status codes:
--   0  = OK (green)       — healthy / RINEX present
--   1  = Warning (yellow) — warning / raw data present, no RINEX yet
--   2  = Critical (red)   — critical / no raw data since yesterday
--   -1 = Unknown (grey)   — no data yet
--   -2 = N/A (grey text)  — station inactive/passive/doesn't log this session
--
-- Combined status logic:
--   Both red (2+2)           → critical (2)
--   One red, other not       → warning (1)
--   At least one yellow      → warning (1)
--   Both green               → OK (0)
--   Both unknown/N/A         → unknown (-1)
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/018_data_flow_status.sql

BEGIN;

-- ============================================================================
-- STATION DATA FLOW STATUS VIEW
-- ============================================================================

CREATE OR REPLACE VIEW station_data_flow_status AS
WITH latest_raw_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking
    WHERE session_type = '15s_24hr' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_raw_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking
    WHERE session_type = '1Hz_1hr' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
latest_rinex_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking
    WHERE session_type = '15s_24hr_rinex' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_rinex_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking
    WHERE session_type = '1Hz_1hr_rinex' AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
-- Consecutive critical count per station (for debounce).
-- Counts unbroken streak of 'critical' from the most recent check backward.
-- A single non-critical check resets the counter.
-- Used to require 2+ consecutive critical (~10 min) before showing Critical.
health_streak AS (
    SELECT sid,
           -- Position of first non-critical in last 6 checks minus 1 = streak length
           COALESCE(
               MIN(rn) FILTER (WHERE overall_status != 'critical'), 7
           ) - 1 AS consecutive_critical
    FROM (
        SELECT sid, overall_status,
               ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
        FROM block_health_summary
    ) recent
    WHERE rn <= 6
    GROUP BY sid
),
-- Base query: compute health_status and data status independently
base AS (
    SELECT
        d.station_id AS sid,

        -- Health status (from connectivity/hardware checks)
        -- Rules:
        --   Offline/degraded stations → -1 (unknown) — can't assess health
        --   Connectivity-only critical + online → capped at Warning
        --   All critical: need 2+ consecutive checks before Critical
        --   Healthy/Warning take effect immediately
        CASE
          WHEN d.station_status IS NOT NULL OR d.health_check IS NOT NULL THEN -2  -- inactive/passive
          WHEN d.is_online = false THEN -1                                         -- offline → unknown (separate category)
          WHEN d.overall_status = 'healthy' THEN 0                                 -- immediate healthy
          WHEN d.overall_status = 'warning' THEN 1                                 -- immediate warning
          WHEN d.overall_status = 'critical'
               AND d.connection_state = 'online'
               AND (d.status_details IS NULL
                    OR d.status_details !~* '(Voltage|Temperature|Disk|Satellite)')
               THEN 1                                                              -- soften connectivity-only critical
          WHEN d.overall_status = 'critical'
               AND COALESCE(hs.consecutive_critical, 1) >= 2
               THEN 2                                                              -- persistent critical (2+ checks)
          WHEN d.overall_status = 'critical' THEN 1                                -- transient critical → warning
          ELSE -1                                                                   -- no health data yet
        END AS health_status,

        -- Evidence-based session flags:
        --   Known receiver_type → supports 15s (all receiver types do)
        --   Probe says true → true
        --   Probe says false but file_tracking has files → true (evidence wins)
        --   No receiver_type + probe says false + no files → N/A

        -- 24h data flow: N/A → Missing → Raw only → OK
        CASE
          WHEN d.station_status IS NOT NULL THEN -2                           -- inactive/discontinued
          WHEN d.receiver_type IS NULL
               AND NOT COALESCE(l.session_15s_24hr, false)
               AND r24.file_date IS NULL THEN -2                              -- unknown receiver + no probe + no files → N/A
          WHEN r24.file_date IS NULL OR r24.file_date < CURRENT_DATE - 1
               THEN 2                                                         -- no recent raw → red
          WHEN x24.file_date IS NULL OR x24.file_date < r24.file_date
               THEN 1                                                         -- raw present, no RINEX → yellow
          ELSE 0                                                              -- RINEX present → green
        END AS status_24h,

        -- 1Hz combined status
        CASE
          WHEN d.station_status IS NOT NULL THEN -2
          WHEN NOT COALESCE(l.session_1hz_1hr, false)
               AND r1h.latest_ts IS NULL THEN -2                              -- probe says no + no files → N/A
          WHEN r1h.latest_ts >= NOW() - INTERVAL '90 minutes' THEN 0         -- green
          WHEN r1h.latest_ts >= NOW() - INTERVAL '6 hours' THEN 1            -- yellow
          WHEN r1h.latest_ts IS NOT NULL THEN 2                               -- red
          ELSE 2
        END AS status_1hz,

        -- RINEX-specific status for detail panel
        CASE
          WHEN d.station_status IS NOT NULL THEN -2
          WHEN d.receiver_type IS NULL
               AND NOT COALESCE(l.session_15s_24hr, false)
               AND r24.file_date IS NULL THEN -2                              -- no evidence of session
          WHEN x24.file_date >= CURRENT_DATE - 1 THEN 0
          WHEN x24.file_date IS NULL AND r24.file_date IS NOT NULL THEN 2
          WHEN x24.file_date IS NULL THEN -1
          WHEN EXTRACT(HOUR FROM NOW()) >= 12 THEN 2
          WHEN EXTRACT(HOUR FROM NOW()) >= 2 THEN 1
          ELSE 0
        END AS rinex_24h_status,

        CASE
          WHEN d.station_status IS NOT NULL THEN -2
          WHEN NOT COALESCE(l.session_1hz_1hr, false)
               AND r1h.latest_ts IS NULL THEN -2                              -- no evidence of session
          WHEN x1h.latest_ts >= NOW() - INTERVAL '90 minutes' THEN 0
          WHEN x1h.latest_ts IS NULL THEN -1
          WHEN x1h.latest_ts >= NOW() - INTERVAL '6 hours' THEN 1
          ELSE 2
        END AS rinex_1hz_status,

        -- Timestamps for detail panels
        r24.file_date AS raw_24h_date,
        r1h.latest_ts AS raw_1hz_ts,
        x24.file_date AS rinex_24h_date,
        x1h.latest_ts AS rinex_1hz_ts,

        -- Effective logging flags (evidence-based: known receiver_type OR probe OR files)
        d.receiver_type IS NOT NULL OR COALESCE(l.session_15s_24hr, false) OR r24.file_date IS NOT NULL AS logging_15s,
        COALESCE(l.session_1hz_1hr, false) OR r1h.latest_ts IS NOT NULL AS logging_1hz

    FROM station_dashboard_data d
    LEFT JOIN station_logging_status l ON l.sid = d.station_id
    LEFT JOIN health_streak hs ON hs.sid = d.station_id
    LEFT JOIN latest_raw_24h r24 ON r24.sid = d.station_id
    LEFT JOIN latest_raw_1hz r1h ON r1h.sid = d.station_id
    LEFT JOIN latest_rinex_24h x24 ON x24.sid = d.station_id
    LEFT JOIN latest_rinex_1hz x1h ON x1h.sid = d.station_id
)
-- Final: add combined_status derived from health + data
SELECT base.*,
    CASE
      -- Both unknown/N/A → unknown
      WHEN base.health_status < 0 AND base.status_24h < 0 THEN -1
      -- Both red → critical
      WHEN base.health_status = 2 AND base.status_24h = 2 THEN 2
      -- One red (treat negative as neutral) → warning
      WHEN GREATEST(
             CASE WHEN base.health_status < 0 THEN 0 ELSE base.health_status END,
             CASE WHEN base.status_24h < 0 THEN 0 ELSE base.status_24h END
           ) = 2 THEN 1
      -- At least one yellow → warning
      WHEN GREATEST(
             CASE WHEN base.health_status < 0 THEN 0 ELSE base.health_status END,
             CASE WHEN base.status_24h < 0 THEN 0 ELSE base.status_24h END
           ) = 1 THEN 1
      -- Both green (or green + N/A) → OK
      ELSE 0
    END AS combined_status
FROM base;

COMMENT ON VIEW station_data_flow_status IS 'Station health, data flow, and combined status codes for dashboards and maps';

COMMIT;
