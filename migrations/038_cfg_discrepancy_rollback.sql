-- Rollback for migration 038: cfg discrepancy event log

BEGIN;

DROP INDEX IF EXISTS cfg_discrepancy_key_time;
DROP INDEX IF EXISTS cfg_discrepancy_station_time;
DROP INDEX IF EXISTS cfg_discrepancy_open_unique;
DROP TABLE IF EXISTS cfg_discrepancy;

DELETE FROM schema_migrations WHERE migration_name = '038_cfg_discrepancy';

COMMIT;
