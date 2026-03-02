-- Rollback for migration 029: Restore pre-optimization views
--
-- This restores the original DISTINCT ON patterns (slower but functionally identical).
-- Run the appropriate forward migration (027 or 028) separately if needed.

BEGIN;

DROP VIEW IF EXISTS station_data_flow_status;
DROP VIEW IF EXISTS station_dashboard_data;
DROP VIEW IF EXISTS icinga_check_data;
DROP VIEW IF EXISTS station_status_summary;
DROP VIEW IF EXISTS station_latest_metrics;
DROP VIEW IF EXISTS station_connectivity;
DROP VIEW IF EXISTS station_port_status;

-- To fully rollback: re-run migration 026_download_performance.sql
-- (which creates the pre-optimization versions of all views)
-- Then optionally re-run 027 and/or 028.

DELETE FROM schema_migrations WHERE migration_name = '029_dashboard_performance';
DELETE FROM schema_migrations WHERE migration_name = '028_dcdc24_voltage';
DELETE FROM schema_migrations WHERE migration_name = '027_staleness_guards';

COMMIT;
