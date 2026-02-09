-- Migration: 014_logging_status.sql
-- Description: Add logging session status table for tracking active receiver logging sessions
-- Date: 2026-02-09
--
-- Tracks which data logging sessions are active on each receiver.
-- Populated from receiver APIs (NetRS activity page, PolaRX5 SBF, etc.)
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/014_logging_status.sql

BEGIN;

-- ============================================================================
-- LOGGING STATUS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS block_logging_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    active_sessions INTEGER,          -- Number of active logging sessions
    session_15s_24hr BOOLEAN,         -- Is 15s_24hr session logging?
    session_1hz_1hr BOOLEAN,          -- Is 1Hz_1hr session logging?
    session_status_1hr BOOLEAN,       -- Is status_1hr session logging?
    status VARCHAR(10),               -- 'ok', 'warning', 'critical'
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_logging_status IS 'Active logging session status from receiver API';

CREATE INDEX IF NOT EXISTS idx_logging_status_sid_ts ON block_logging_status(sid, ts DESC);

-- ============================================================================
-- VIEW FOR LATEST STATUS
-- ============================================================================

CREATE OR REPLACE VIEW station_logging_status AS
SELECT DISTINCT ON (sid)
    sid,
    ts as last_check,
    active_sessions,
    session_15s_24hr,
    session_1hz_1hr,
    session_status_1hr,
    status
FROM block_logging_status
ORDER BY sid, ts DESC;

COMMENT ON VIEW station_logging_status IS 'Latest logging session status for each station';

COMMIT;
