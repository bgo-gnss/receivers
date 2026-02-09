-- Migration: 009_ping_status.sql
-- Description: Add ping status tracking table for online/offline monitoring
-- Date: 2026-02-07
--
-- Tracks ping checks to determine if stations are online (reachable) or offline
-- Stores state transitions to calculate duration of current state
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
-- STATE TRANSITION TRACKING VIEW
-- ============================================================================

-- View to get current state and duration for each station.
-- Requires 2 consecutive failed pings before reporting offline.
-- A single failed ping after a success is still shown as online to
-- avoid false-offline reports on lossy 3G/4G links.
CREATE OR REPLACE VIEW station_connectivity AS
WITH latest_two AS (
    -- Get the two most recent pings for each station
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message,
        ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_ping_status
),
latest_ping AS (
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message
    FROM latest_two WHERE rn = 1
),
prev_ping AS (
    SELECT sid, is_online AS prev_online
    FROM latest_two WHERE rn = 2
),
effective_state AS (
    -- Only report offline if BOTH latest pings failed.
    -- Single failure after success stays online (lossy link tolerance).
    SELECT
        lp.sid,
        lp.ts,
        CASE
            WHEN lp.is_online THEN true
            WHEN pp.prev_online IS NULL THEN lp.is_online  -- only 1 ping exists
            WHEN pp.prev_online = false THEN false          -- 2 consecutive failures
            ELSE true                                        -- single failure, prev was ok
        END AS is_online,
        lp.response_time_ms,
        lp.packet_loss,
        lp.error_message
    FROM latest_ping lp
    LEFT JOIN prev_ping pp ON lp.sid = pp.sid
),
state_changes AS (
    -- Find when the current state started by looking at state transitions
    SELECT
        p.sid,
        p.ts,
        p.is_online,
        LAG(p.is_online) OVER (PARTITION BY p.sid ORDER BY p.ts) as prev_state
    FROM block_ping_status p
),
state_start AS (
    -- Get the timestamp when the current state started
    SELECT DISTINCT ON (sid)
        sid,
        ts as state_since
    FROM state_changes
    WHERE is_online != prev_state OR prev_state IS NULL
    ORDER BY sid, ts DESC
)
SELECT
    es.sid,
    es.ts as last_check,
    es.is_online,
    es.response_time_ms,
    es.packet_loss,
    es.error_message,
    COALESCE(ss.state_since, es.ts) as state_since,
    NOW() - COALESCE(ss.state_since, es.ts) as state_duration
FROM effective_state es
LEFT JOIN state_start ss ON es.sid = ss.sid;

COMMENT ON VIEW station_connectivity IS 'Current connectivity state per station (requires 2 consecutive failed pings for offline)';

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
