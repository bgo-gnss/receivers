-- Rollback 079 — drop the inferred-completion writer and its records. Deploy
-- code that no longer sends completions first, or every check run fails to
-- record. Absent days go back to overdue/no_completion_record.

BEGIN;
DROP FUNCTION IF EXISTS record_solution_completions(jsonb);
DELETE FROM solution_completion WHERE completion_id LIKE 'files:%';
DELETE FROM schema_migrations WHERE migration_name = '079_solution_completion_from_files';
COMMIT;
