-- Rollback 058: drop missing_rinex + file_coverage + refresh_file_coverage.
--
-- Explicit ordered drops (NO CASCADE): missing_rinex depends on file_coverage,
-- so drop it first. No CASCADE so that a later slice's objects built on
-- file_coverage (missing_on_receiver, needs_repull_from_archive) cause a LOUD
-- failure here rather than being silently dropped.

BEGIN;

DROP VIEW IF EXISTS missing_rinex;
DROP FUNCTION IF EXISTS refresh_file_coverage();
DROP MATERIALIZED VIEW IF EXISTS file_coverage;

DELETE FROM schema_migrations WHERE migration_name = '058_file_coverage';

COMMIT;
