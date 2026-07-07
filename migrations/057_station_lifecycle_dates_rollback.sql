-- Rollback 057: drop the station lifecycle-date columns + fallback function.

BEGIN;

DROP FUNCTION IF EXISTS sync_station_dates_from_observed();

ALTER TABLE stations
    DROP COLUMN IF EXISTS data_start,
    DROP COLUMN IF EXISTS data_end;

DELETE FROM schema_migrations WHERE migration_name = '057_station_lifecycle_dates';

COMMIT;
