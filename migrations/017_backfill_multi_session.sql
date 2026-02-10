-- Migration: 017_backfill_multi_session.sql
-- Description: Populate backfill_progress for 15s_24hr and 1Hz_1hr session types
-- Date: 2026-02-10
--
-- The backfill_progress table (migration 016) already supports multi-session via
-- composite PK (sid, session_type).  This migration adds rows for 15s_24hr and
-- 1Hz_1hr so the scheduler can backfill all three session types.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/017_backfill_multi_session.sql

BEGIN;

-- ============================================================================
-- 15s_24hr BACKFILL: 30 days back
-- ============================================================================
-- Daily files — backfill 30 days for active PolaRX5 stations.

INSERT INTO backfill_progress (sid, session_type, backfill_start, next_date, backfill_end, status)
SELECT
    s.sid,
    '15s_24hr',
    (CURRENT_DATE - INTERVAL '30 days')::date,
    (CURRENT_DATE - INTERVAL '30 days')::date,
    (CURRENT_DATE - INTERVAL '1 day')::date,
    'pending'
FROM stations s
WHERE LOWER(s.receiver_type) = 'polarx5'
  AND s.station_status IS NULL
  AND (s.health_check IS NULL OR s.health_check != 'passive')
ON CONFLICT (sid, session_type) DO NOTHING;

-- ============================================================================
-- 1Hz_1hr BACKFILL: 7 days back
-- ============================================================================
-- Hourly files — backfill 7 days for active PolaRX5 stations.

INSERT INTO backfill_progress (sid, session_type, backfill_start, next_date, backfill_end, status)
SELECT
    s.sid,
    '1Hz_1hr',
    (CURRENT_DATE - INTERVAL '7 days')::date,
    (CURRENT_DATE - INTERVAL '7 days')::date,
    (CURRENT_DATE - INTERVAL '1 day')::date,
    'pending'
FROM stations s
WHERE LOWER(s.receiver_type) = 'polarx5'
  AND s.station_status IS NULL
  AND (s.health_check IS NULL OR s.health_check != 'passive')
ON CONFLICT (sid, session_type) DO NOTHING;

COMMIT;
