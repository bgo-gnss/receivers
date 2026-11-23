-- Verify migration 080 — behavioural, inside a rolled-back transaction.

\set ON_ERROR_STOP on

SELECT CASE WHEN count(*) = 1 THEN 'PASS'
            ELSE 'FAIL: migration row missing' END AS migration_recorded
FROM schema_migrations WHERE migration_name = '080_solution_station_operator';

SELECT CASE WHEN prosecdef THEN 'PASS' ELSE 'FAIL: writer is not SECURITY DEFINER' END
       AS writer_definer
FROM pg_proc WHERE proname = 'record_solution_stations';

BEGIN;

SELECT record_solution_stations(jsonb_build_object(
  'run_id', '00000000-0000-4000-8000-000000000080',
  'checked_at', '1999-01-03T10:00:00+00:00',
  'station_info', jsonb_build_array(
     jsonb_build_object('station', 'ZZ01', 'local', true, 'agency', 'KAUST',
                        'operator', 'IMO'),
     jsonb_build_object('station', 'ZZ02', 'local', true, 'agency', 'IMO'))));

SELECT CASE WHEN count(*) = 2 AND count(operator) = 1
             AND bool_or(station = 'ZZ01' AND operator = 'IMO' AND agency = 'KAUST')
            THEN 'PASS'
            ELSE 'FAIL: operator not stored (or a missing one not NULL)' END AS written
FROM solution_station WHERE station IN ('ZZ01', 'ZZ02');

ROLLBACK;
