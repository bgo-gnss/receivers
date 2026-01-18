-- Rollback: 002_file_tracking_rollback.sql
-- Description: Rollback file tracking migration
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/002_file_tracking_rollback.sql

BEGIN;

-- Drop view first
DROP VIEW IF EXISTS data_availability CASCADE;

-- Drop functions
DROP FUNCTION IF EXISTS is_file_missing(VARCHAR(4), VARCHAR(20), DATE, SMALLINT);
DROP FUNCTION IF EXISTS is_health_imported(VARCHAR(4), DATE, VARCHAR(64));
DROP FUNCTION IF EXISTS upsert_file_tracking(VARCHAR(4), VARCHAR(20), DATE, SMALLINT, VARCHAR(100), VARCHAR(20), BIGINT, INTEGER, VARCHAR(64), VARCHAR(255), TEXT);

-- Drop table (cascade drops indexes)
DROP TABLE IF EXISTS file_tracking CASCADE;

COMMIT;
