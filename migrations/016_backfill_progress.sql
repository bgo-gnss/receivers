-- Migration: 016_backfill_progress.sql
-- Description: Track backfill progress per station/session for resumable backfill operations
-- Date: 2026-02-10
--
-- The backfill_progress table tracks which date each station has been backfilled up to,
-- allowing the backfill to resume after restarts and providing visibility into progress.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/016_backfill_progress.sql

BEGIN;

-- ============================================================================
-- BACKFILL PROGRESS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS backfill_progress (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    session_type VARCHAR(20) NOT NULL DEFAULT 'status_1hr',

    -- Date range to backfill
    backfill_start DATE NOT NULL,       -- Earliest date to backfill (typically NOW - 1 year)
    next_date DATE NOT NULL,         -- Next date to process (avoids PostgreSQL reserved word current_date)
    backfill_end DATE NOT NULL,         -- Latest date to backfill (typically yesterday)

    -- Status tracking
    status VARCHAR(20) DEFAULT 'pending',
    -- Possible values:
    --   'pending'     - Not yet started
    --   'in_progress' - Currently being processed
    --   'completed'   - All dates processed
    --   'paused'      - Manually paused

    -- Progress counters
    files_found INTEGER DEFAULT 0,
    files_imported INTEGER DEFAULT 0,
    files_missing INTEGER DEFAULT 0,
    files_error INTEGER DEFAULT 0,

    -- Timing
    last_run TIMESTAMPTZ,
    last_duration_seconds REAL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (sid, session_type)
);

COMMENT ON TABLE backfill_progress IS 'Track backfill progress per station for resumable status_1hr health extraction';

-- ============================================================================
-- INDEXES
-- ============================================================================

-- Find stations needing backfill work (ordered by least recently processed)
CREATE INDEX idx_backfill_progress_pending
    ON backfill_progress(last_run ASC NULLS FIRST, sid)
    WHERE status IN ('pending', 'in_progress');

-- ============================================================================
-- POPULATE FOR ACTIVE POLARX5 STATIONS
-- ============================================================================

-- Insert initial backfill rows for all active PolaRX5 stations.
-- Only stations that are active (station_status IS NULL) and not passive.
-- Backfill range: 1 year back to yesterday.
INSERT INTO backfill_progress (sid, session_type, backfill_start, next_date, backfill_end, status)
SELECT
    s.sid,
    'status_1hr',
    (CURRENT_DATE - INTERVAL '1 year')::date,
    (CURRENT_DATE - INTERVAL '1 year')::date,
    (CURRENT_DATE - INTERVAL '1 day')::date,
    'pending'
FROM stations s
WHERE LOWER(s.receiver_type) = 'polarx5'
  AND s.station_status IS NULL
  AND (s.health_check IS NULL OR s.health_check != 'passive')
ON CONFLICT (sid, session_type) DO NOTHING;

COMMIT;
