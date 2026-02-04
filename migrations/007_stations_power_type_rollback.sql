-- Rollback migration 007
ALTER TABLE stations DROP COLUMN IF EXISTS power_type;
