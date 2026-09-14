-- Rollback 072: put the retracted absence rows back.
--
-- Re-inserts everything migration 072 deleted from its backup table. Safe to
-- run more than once (ON CONFLICT DO NOTHING against the slot unique key).
--
-- NOTE: if the scheduler has been running since 072, some of these slots will
-- have been legitimately re-recorded by record_file_absence. The conflict
-- clause keeps the LIVE row in that case rather than resurrecting a stale
-- confirmation counter over it.

BEGIN;

INSERT INTO file_absence
SELECT * FROM file_absence_retracted_072
ON CONFLICT DO NOTHING;

DELETE FROM schema_migrations
WHERE migration_name = '072_retract_absences_for_files_we_have';

COMMIT;
