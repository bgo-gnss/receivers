-- Rollback: 021_archive_format.sql
-- Removes archive_format, storage_location, file_locations tables and format_id column.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/021_archive_format_rollback.sql

BEGIN;

-- Drop view first (depends on archive_format)
DROP VIEW IF EXISTS file_tracking_with_format;

-- Drop helper function
DROP FUNCTION IF EXISTS get_archive_format(VARCHAR);

-- Remove format_id column from file_tracking
ALTER TABLE file_tracking DROP COLUMN IF EXISTS format_id;

-- Drop tables in dependency order
DROP TABLE IF EXISTS file_locations;
DROP TABLE IF EXISTS storage_location;
DROP TABLE IF EXISTS archive_format;

COMMIT;
