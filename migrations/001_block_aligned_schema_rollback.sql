-- Rollback: 001_block_aligned_schema_rollback.sql
-- Description: Rollback block-aligned schema migration
--
-- WARNING: This will delete all data in the new tables!
-- Only run if you need to revert to the old schema.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/001_block_aligned_schema_rollback.sql

BEGIN;

-- Drop view first (depends on tables)
DROP VIEW IF EXISTS checkcomm CASCADE;

-- Drop helper functions
DROP FUNCTION IF EXISTS compute_hourly_aggregate(VARCHAR(4), TIMESTAMPTZ);

-- Drop aggregation tables
DROP TABLE IF EXISTS agg_daily CASCADE;
DROP TABLE IF EXISTS agg_hourly CASCADE;

-- Drop block tables (in reverse dependency order)
DROP TABLE IF EXISTS block_receiver_setup CASCADE;
DROP TABLE IF EXISTS block_receiver_time CASCADE;
DROP TABLE IF EXISTS block_wifi_status CASCADE;
DROP TABLE IF EXISTS block_ntrip_client CASCADE;
DROP TABLE IF EXISTS block_ntrip_server CASCADE;
DROP TABLE IF EXISTS block_sat_visibility CASCADE;
DROP TABLE IF EXISTS block_pos_covariance CASCADE;
DROP TABLE IF EXISTS block_pvt_geodetic CASCADE;
DROP TABLE IF EXISTS block_disk_status CASCADE;
DROP TABLE IF EXISTS block_receiver_status CASCADE;
DROP TABLE IF EXISTS block_power_status CASCADE;

-- Drop stations table last (other tables reference it)
DROP TABLE IF EXISTS stations CASCADE;

COMMIT;

-- If you had the old checkcomm table backed up:
-- ALTER TABLE checkcomm_old RENAME TO checkcomm;
