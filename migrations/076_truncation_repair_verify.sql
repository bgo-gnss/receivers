-- Verify migration 076 — behavioural, inside a rolled-back transaction.

\set ON_ERROR_STOP on

SELECT CASE WHEN count(*) = 1 THEN 'PASS'
            ELSE 'FAIL: migration row missing' END AS migration_recorded
FROM schema_migrations WHERE migration_name = '076_truncation_repair';

BEGIN;

-- 1. the writer's INSERT shape is accepted (salvage, repaired, partial)
INSERT INTO truncation_repair (run_id, rel_path, station, session_type, file_date,
    file_hour, action, outcome, is_partial, source, readable_bytes, kept_bytes, blocks)
VALUES ('verify-076', '1999/jan/TEST/1Hz_1hr/raw/TEST199901011200b.sbf.gz', 'TEST',
    '1Hz_1hr', '1999-01-01', 12, 'salvage', 'repaired', true, 'archive', 100, 96, 3);
SELECT 'PASS' AS insert_accepted;

-- 2. a partial flag on anything but a repaired salvage/replace is refused
SAVEPOINT s;
DO $$ BEGIN
  INSERT INTO truncation_repair (run_id, rel_path, station, session_type, file_date,
      action, outcome, is_partial)
  VALUES ('verify-076', 'x', 'TEST', '1Hz_1hr', '1999-01-01', 'refetch', 'repaired', true);
  RAISE EXCEPTION 'FAIL: partial refetch accepted';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS: partial refetch refused';
END $$;
ROLLBACK TO SAVEPOINT s;

-- 3. the planner's ledger query runs
SELECT CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'PASS (rows present)' END AS planner_query
FROM truncation_repair WHERE outcome = 'not_on_receiver';

ROLLBACK;
