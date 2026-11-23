-- Verify migration 078 — behavioural, inside a rolled-back transaction.

\set ON_ERROR_STOP on

SELECT CASE WHEN count(*) = 1 THEN 'PASS'
            ELSE 'FAIL: migration row missing' END AS migration_recorded
FROM schema_migrations WHERE migration_name = '078_solution_station';

SELECT CASE WHEN prosecdef THEN 'PASS' ELSE 'FAIL: writer is not SECURITY DEFINER' END
       AS writer_definer
FROM pg_proc WHERE proname = 'record_solution_stations';

BEGIN;

SELECT record_solution_stations(jsonb_build_object(
  'run_id', '00000000-0000-4000-8000-000000000078',
  'checked_at', '1999-01-03T10:00:00+00:00',
  'station_info', jsonb_build_array(
     jsonb_build_object('station', 'ZZ01', 'local', true, 'agency', 'IMO'),
     jsonb_build_object('station', 'ZZ02', 'local', false, 'agency', ''))));

SELECT CASE WHEN count(*) = 2 AND count(agency) = 1 THEN 'PASS'
            ELSE 'FAIL: rows not written (or empty agency kept)' END AS written
FROM solution_station WHERE station IN ('ZZ01', 'ZZ02');

-- an OLDER run arriving late does not overwrite
SELECT record_solution_stations(jsonb_build_object(
  'run_id', '00000000-0000-4000-8000-000000000079',
  'checked_at', '1999-01-02T10:00:00+00:00',
  'station_info', jsonb_build_array(
     jsonb_build_object('station', 'ZZ01', 'local', false, 'agency', 'OLD'))));

SELECT CASE WHEN is_local AND agency = 'IMO' THEN 'PASS'
            ELSE 'FAIL: a late older run overwrote a newer fact' END AS late_ignored
FROM solution_station WHERE station = 'ZZ01';

ROLLBACK;
