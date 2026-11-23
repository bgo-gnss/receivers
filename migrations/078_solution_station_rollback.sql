-- Rollback 078 — drop solution_station and its writer. Deploy code that no
-- longer sends station_info first, or every check run fails to record.

BEGIN;
DROP FUNCTION IF EXISTS record_solution_stations(jsonb);
DROP TABLE IF EXISTS solution_station;
DELETE FROM schema_migrations WHERE migration_name = '078_solution_station';
COMMIT;
