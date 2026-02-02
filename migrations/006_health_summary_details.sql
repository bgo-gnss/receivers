-- Migration: 006_health_summary_details.sql
-- Description: Add status_details column to block_health_summary
-- Date: 2026-02-02
--
-- Stores a short human-readable explanation of why the overall status is
-- critical or warning (e.g. "NTRIP error, FTP down").  Populated by the
-- health db_writer from individual metric statuses.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/006_health_summary_details.sql

BEGIN;

ALTER TABLE block_health_summary
    ADD COLUMN IF NOT EXISTS status_details TEXT;

COMMENT ON COLUMN block_health_summary.status_details
    IS 'Human-readable list of metrics causing non-healthy status';

COMMIT;
