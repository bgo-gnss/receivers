-- Rollback: 009_ping_status_rollback.sql
-- Reverts: 009_ping_status.sql
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/009_ping_status_rollback.sql

BEGIN;

DROP FUNCTION IF EXISTS format_duration(INTERVAL);
DROP VIEW IF EXISTS station_connectivity;
DROP INDEX IF EXISTS idx_ping_status_ts;
DROP INDEX IF EXISTS idx_ping_status_sid_ts;
DROP TABLE IF EXISTS block_ping_status;

COMMIT;
