-- Rollback: 006_health_summary_details_rollback.sql
-- Description: Remove status_details column from block_health_summary

BEGIN;

ALTER TABLE block_health_summary DROP COLUMN IF EXISTS status_details;

COMMIT;
