-- Rollback migration 013: Remove receiver identity columns from stations table

BEGIN;

ALTER TABLE stations DROP COLUMN IF EXISTS firmware_version;
ALTER TABLE stations DROP COLUMN IF EXISTS detected_model;
ALTER TABLE stations DROP COLUMN IF EXISTS serial_number;
ALTER TABLE stations DROP COLUMN IF EXISTS identity_last_checked;

COMMIT;
