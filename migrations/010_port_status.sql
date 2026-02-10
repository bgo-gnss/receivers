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

-- Requires 2 consecutive failures before reporting port down.
-- 'unknown' = SBF fallback with no port data (treat like timeout).
-- 'refused' is a definitive answer and reported immediately.
-- When latest row has NULL ports (SBF fallback), carry forward previous port numbers.
CREATE OR REPLACE VIEW station_port_status AS
WITH latest_two AS (
    SELECT sid, ts, download_port, download_status, download_response_ms,
           health_port, health_status, health_response_ms,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_port_status
),
latest AS (
    SELECT * FROM latest_two WHERE rn = 1
),
prev AS (
    SELECT sid,
           download_port AS prev_download_port,
           download_status AS prev_download,
           health_port AS prev_health_port,
           health_status AS prev_health
    FROM latest_two WHERE rn = 2
),
effective AS (
    SELECT
        l.sid,
        l.ts AS last_check,
        -- Carry forward port numbers when latest has NULL (SBF fallback)
        COALESCE(l.download_port, p.prev_download_port) AS download_port,
        -- Effective download status: timeout/error/unknown needs 2 consecutive failures
        CASE
            WHEN l.download_status IN ('open', 'ok') THEN l.download_status
            WHEN l.download_status = 'refused' THEN 'refused'
            WHEN l.download_status IN ('timeout', 'error', 'unknown') AND
                 COALESCE(p.prev_download, 'open') IN ('timeout', 'error', 'unknown', 'refused')
                THEN l.download_status                            -- 2 consecutive failures
            WHEN l.download_status IN ('timeout', 'error', 'unknown')
                THEN COALESCE(p.prev_download, l.download_status) -- single failure, use prev
            ELSE l.download_status
        END AS download_status,
        l.download_response_ms,
        COALESCE(l.health_port, p.prev_health_port) AS health_port,
        -- Effective health status: same logic
        CASE
            WHEN l.health_status IN ('open', 'ok') THEN l.health_status
            WHEN l.health_status = 'refused' THEN 'refused'
            WHEN l.health_status IN ('timeout', 'error', 'unknown') AND
                 COALESCE(p.prev_health, 'open') IN ('timeout', 'error', 'unknown', 'refused')
                THEN l.health_status                              -- 2 consecutive failures
            WHEN l.health_status IN ('timeout', 'error', 'unknown')
                THEN COALESCE(p.prev_health, l.health_status)     -- single failure, use prev
            ELSE l.health_status
        END AS health_status,
        l.health_response_ms
    FROM latest l
    LEFT JOIN prev p ON l.sid = p.sid
)
SELECT
    sid,
    last_check,
    download_port,
    download_status,
    download_response_ms,
    health_port,
    health_status,
    health_response_ms,
    -- Overall status for dashboard coloring
    CASE
        WHEN download_status IN ('open', 'ok') AND (health_status IN ('open', 'ok') OR health_status IS NULL) THEN 'active'
        WHEN download_status IN ('refused', 'timeout', 'error', 'critical') THEN download_status
        WHEN health_status IN ('refused', 'timeout', 'error', 'critical') THEN health_status
        WHEN download_status = 'warning' OR health_status = 'warning' THEN 'warning'
        ELSE 'unknown'
    END AS overall_port_status
FROM effective;

COMMENT ON VIEW station_port_status IS 'Latest port status per station (requires 2 consecutive failures for down; unknown/SBF-fallback carries forward previous status)';

COMMIT;
