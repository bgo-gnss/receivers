-- Verify migration 077 — behavioural, inside a rolled-back transaction.

\set ON_ERROR_STOP on

SELECT CASE WHEN count(*) = 1 THEN 'PASS'
            ELSE 'FAIL: migration row missing' END AS migration_recorded
FROM schema_migrations WHERE migration_name = '077_daily_solutions';

SELECT CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL: stream not seeded' END AS stream_seeded
FROM solution_stream WHERE stream = 'imo.24hr_itrf14';

SELECT CASE WHEN prosecdef THEN 'PASS' ELSE 'FAIL: writer is not SECURITY DEFINER' END
       AS writer_definer
FROM pg_proc WHERE proname = 'record_solution_check';

SELECT CASE WHEN NOT prosecdef AND provolatile = 's' AND NOT proisstrict AND proconfig IS NULL
            THEN 'PASS' ELSE 'FAIL: grid is not inlinable (definer/volatility/strict/SET)' END
       AS grid_inlinable
FROM pg_proc WHERE proname = 'solution_day_grid';

BEGIN;

-- 1. one complete snapshot (1 station x 2 days) is recorded
SELECT record_solution_check(jsonb_build_object(
  'schema', 'solution-check-facts/2',
  'run_id', '00000000-0000-4000-8000-000000000077',
  'checked_at', '1999-01-03T10:00:00+00:00',
  'stream', 'imo.24hr_itrf14', 'source', 'verify-077', 'outcome', 'ok',
  'window', jsonb_build_object('from', '1999-01-01', 'to', '1999-01-02', 'days', 2),
  'roster', jsonb_build_object('version', repeat('a', 64), 'reviewed_from', '1999-01-01',
     'stations', jsonb_build_array('TEST'),
     'intervals', jsonb_build_array(jsonb_build_object('station', 'TEST',
        'kind', 'expected', 'day_from', '1999-01-01'))),
  'cells', jsonb_build_array(
     jsonb_build_object('station', 'TEST', 'day', '1999-01-01', 'fact', 'valid',
        'fingerprint', repeat('b', 64)),
     jsonb_build_object('station', 'TEST', 'day', '1999-01-02', 'fact', 'absent'))
)) AS recorded;

-- 2. the grid gives verified + overdue for it (day 2's deadline passed)
SELECT CASE WHEN string_agg(state || '/' || reason, ',' ORDER BY day)
                 = 'available/verified,overdue/no_completion_record'
            THEN 'PASS' ELSE 'FAIL: ' || string_agg(state || '/' || reason, ',' ORDER BY day) END
       AS grid_states
FROM solution_day_grid('imo.24hr_itrf14', '1999-01-01', '1999-01-02',
                       '1999-01-03T10:00:00+00:00');

-- 3. an incomplete snapshot is refused and writes nothing
SAVEPOINT s;
DO $$ BEGIN
  PERFORM record_solution_check(jsonb_build_object(
    'schema', 'solution-check-facts/2',
    'run_id', '00000000-0000-4000-8000-000000000078',
    'checked_at', '1999-01-04T10:00:00+00:00',
    'stream', 'imo.24hr_itrf14', 'source', 'verify-077', 'outcome', 'ok',
    'window', jsonb_build_object('from', '1999-01-01', 'to', '1999-01-02'),
    'roster', jsonb_build_object('version', repeat('a', 64), 'reviewed_from', '1999-01-01',
       'stations', jsonb_build_array('TEST'), 'intervals', '[]'::jsonb),
    'cells', jsonb_build_array(
       jsonb_build_object('station', 'TEST', 'day', '1999-01-01', 'fact', 'absent'))));
  RAISE EXCEPTION 'FAIL: incomplete snapshot accepted';
EXCEPTION WHEN raise_exception THEN
  IF SQLERRM LIKE 'FAIL%' THEN RAISE; END IF;
  RAISE NOTICE 'PASS: incomplete snapshot refused';
END $$;
ROLLBACK TO SAVEPOINT s;

ROLLBACK;
