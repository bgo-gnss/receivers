-- Rollback 077 — drop the daily-solution monitoring tables and functions.
--
-- Loses the solution fact history (first-seen, revisions, transitions). Export first:
--   \copy (SELECT * FROM solution_fact) TO 'solution_fact.csv' CSV HEADER
--   \copy (SELECT * FROM solution_transition) TO 'solution_transition.csv' CSV HEADER

BEGIN;
DROP FUNCTION IF EXISTS solution_day_grid(text, date, date, timestamptz);
DROP FUNCTION IF EXISTS record_solution_check(jsonb);
DROP TABLE IF EXISTS solution_completion_item;
DROP TABLE IF EXISTS solution_completion;
DROP TABLE IF EXISTS solution_transition;
DROP TABLE IF EXISTS solution_fact;
DROP TABLE IF EXISTS solution_check_run;
DROP TABLE IF EXISTS solution_roster_interval;
DROP TABLE IF EXISTS solution_roster_station;
DROP TABLE IF EXISTS solution_roster;
DROP TABLE IF EXISTS solution_stream;
DELETE FROM schema_migrations WHERE migration_name = '077_daily_solutions';
COMMIT;
