-- Rollback 075 — drop ltb_runs and the migration record.
--
-- The rows are observations of past runs, not state anything reads back:
-- the scheduler never queries this table, and the writer
-- (receivers.scheduling.ltb_run_metrics.record_ltb_run) degrades to a
-- WARNING when the table is gone, so the LTB keeps running. Only the Grafana
-- LTB panel goes blank.

BEGIN;

DROP TABLE IF EXISTS ltb_runs;

DELETE FROM schema_migrations
 WHERE migration_name = '075_ltb_runs';

COMMIT;
