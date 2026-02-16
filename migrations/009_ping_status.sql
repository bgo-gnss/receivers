-- Migration: 009_ping_status.sql
-- Description: Add ping status tracking table for online/offline monitoring
-- Date: 2026-02-07
-- Updated: 2026-02-13  3-check debounce (ping+port), NTRIP override,
--                      debounced state_since tracking
--
-- Tracks ping checks to determine if stations are online (reachable) or offline
-- Stores state transitions to calculate duration of current state
--
-- Online:  NTRIP connected, OR (any of last 3 pings OK AND any of last 3 port checks OK)
-- Offline: All 3 recent pings failed, OR all 3 recent port checks failed
-- State duration tracks debounced state transitions (not raw ping flips)
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/009_ping_status.sql

BEGIN;

-- ============================================================================
-- PING STATUS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS block_ping_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    is_online BOOLEAN NOT NULL,              -- True if ping succeeded
    response_time_ms REAL,                   -- Ping response time (NULL if offline)
    packet_loss REAL,                        -- Packet loss percentage
    error_message TEXT,                      -- Error message if offline
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_ping_status IS 'Ping check results for online/offline tracking';

-- Index for latest status queries
CREATE INDEX IF NOT EXISTS idx_ping_status_sid_ts ON block_ping_status(sid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ping_status_ts ON block_ping_status(ts DESC);

-- ============================================================================
-- CONNECTIVITY STATE VIEW
-- ============================================================================

-- View to get current connectivity state and duration for each station.
-- Updated: 2026-02-13  3-check debounce for ping+port, NTRIP override,
--                      debounced state_since tracking
--
-- Online/Offline logic:
--   Online:  NTRIP connected, OR (any of last 3 pings OK AND any of last 3 port checks OK)
--   Offline: All 3 recent pings failed, OR all 3 recent port checks failed
--
-- State duration (state_since) tracks debounced state transitions:
--   Computes debounced_online per ping timestamp using a 3-row rolling window,
--   then tracks when that debounced state flips. This prevents short blips
--   from resetting the state_since timer on flapping stations.
--
CREATE OR REPLACE VIEW station_connectivity AS
WITH latest_pings AS (
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message,
        ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_ping_status
),
ping_debounced AS (
    -- Any of the 3 most recent pings succeeded → ping OK
    SELECT sid,
        bool_or(is_online) FILTER (WHERE rn <= 3) AS ping_any_ok
    FROM latest_pings
    WHERE rn <= 3
    GROUP BY sid
),
latest_ping AS (
    -- Most recent ping for response details
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message
    FROM latest_pings WHERE rn = 1
),
latest_ports AS (
    SELECT sid, ts, download_status,
        ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_port_status
),
port_debounced AS (
    -- Any of the 3 most recent port checks succeeded → port OK
    SELECT sid,
        bool_or(download_status IN ('open', 'ok')) FILTER (WHERE rn <= 3) AS port_any_ok
    FROM latest_ports
    WHERE rn <= 3
    GROUP BY sid
),
latest_ntrip AS (
    -- Most recent NTRIP status from either server or client table
    SELECT DISTINCT ON (sid) sid, status AS ntrip_status
    FROM (
        SELECT sid, ts, status FROM block_ntrip_server
        UNION ALL
        SELECT sid, ts, status FROM block_ntrip_client
    ) ntrip_all
    ORDER BY sid, ts DESC
),
-- Debounced state tracking: compute rolling 3-row debounced_online per ping,
-- then find transitions of that debounced state for state_since.
ping_with_debounced AS (
    SELECT sid, ts, is_online,
        bool_or(is_online) OVER (
            PARTITION BY sid ORDER BY ts
            ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
        ) AS debounced_online
    FROM block_ping_status
),
debounced_state_changes AS (
    SELECT sid, ts, debounced_online,
        LAG(debounced_online) OVER (PARTITION BY sid ORDER BY ts) AS prev_debounced
    FROM ping_with_debounced
),
debounced_state_start AS (
    -- Most recent debounced state transition per station
    SELECT DISTINCT ON (sid) sid, ts AS state_since
    FROM debounced_state_changes
    WHERE debounced_online != prev_debounced OR prev_debounced IS NULL
    ORDER BY sid, ts DESC
)
SELECT
    lp.sid,
    lp.ts AS last_check,
    -- Online if ANY signal positive: NTRIP connected, any ping OK, or any port OK
    -- Offline only when ALL signals fail (tolerates slow telemetry links)
    CASE
        WHEN COALESCE(nt.ntrip_status, '') = 'connected' THEN true
        WHEN COALESCE(pd.ping_any_ok, false) THEN true
        WHEN COALESCE(prd.port_any_ok, false) THEN true
        ELSE false
    END AS is_online,
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

COMMENT ON VIEW station_connectivity IS 'Current connectivity state per station (3-check ping+port debounce, NTRIP override, debounced state_since)';

-- ============================================================================
-- HELPER FUNCTION - Format duration for display
-- ============================================================================

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

COMMENT ON FUNCTION format_duration IS 'Format interval as short duration string (e.g., 5d, 2w, 3h)';

COMMIT;
