-- Migration: 010_port_status.sql
-- Description: Add port status tracking table for download/health port monitoring
-- Date: 2026-02-07
--
-- Tracks port check results to show which ports are open/closed/timeout
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/010_port_status.sql

BEGIN;

-- ============================================================================
-- PORT STATUS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS block_port_status (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    -- Download port (FTP for Septentrio, HTTP for Trimble)
    download_port INTEGER,
    download_status VARCHAR(20),      -- 'open', 'refused', 'timeout', 'error'
    download_response_ms REAL,
    -- Health/control port (HTTP for web interface)
    health_port INTEGER,
    health_status VARCHAR(20),        -- 'open', 'refused', 'timeout', 'error'
    health_response_ms REAL,
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_port_status IS 'Port check results for download and health monitoring';

-- Index for latest status queries
CREATE INDEX IF NOT EXISTS idx_port_status_sid_ts ON block_port_status(sid, ts DESC);

-- ============================================================================
-- VIEW FOR DASHBOARD
-- ============================================================================

CREATE OR REPLACE VIEW station_port_status AS
SELECT DISTINCT ON (sid)
    sid,
    ts as last_check,
    download_port,
    download_status,
    download_response_ms,
    health_port,
    health_status,
    health_response_ms,
    -- Overall status for dashboard coloring
    CASE
        WHEN download_status = 'open' AND (health_status = 'open' OR health_status IS NULL) THEN 'active'
        WHEN download_status IN ('refused', 'timeout', 'error') THEN download_status
        WHEN health_status IN ('refused', 'timeout', 'error') THEN health_status
        ELSE 'unknown'
    END as overall_port_status
FROM block_port_status
ORDER BY sid, ts DESC;

COMMENT ON VIEW station_port_status IS 'Latest port status for each station';

COMMIT;
