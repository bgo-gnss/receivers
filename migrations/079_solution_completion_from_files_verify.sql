-- Verify migration 079 — behavioural, inside a rolled-back transaction.

\set ON_ERROR_STOP on

SELECT CASE WHEN count(*) = 1 THEN 'PASS'
            ELSE 'FAIL: migration row missing' END AS migration_recorded
FROM schema_migrations WHERE migration_name = '079_solution_completion_from_files';

SELECT CASE WHEN prosecdef THEN 'PASS' ELSE 'FAIL: writer is not SECURITY DEFINER' END
       AS writer_definer
FROM pg_proc WHERE proname = 'record_solution_completions';

BEGIN;

SELECT record_solution_completions(jsonb_build_object(
  'stream', 'imo.24hr_itrf14', 'checked_at', '1999-01-03T10:00:00+00:00',
  'completions', jsonb_build_array(
     jsonb_build_object('completion_id', 'files:verify-079:1999-01-01',
                        'day_from', '1999-01-01', 'day_to', '1999-01-02'))));

SELECT CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL: completion not written' END
       AS written
FROM solution_completion WHERE completion_id = 'files:verify-079:1999-01-01';

ROLLBACK;
