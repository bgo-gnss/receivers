-- Rollback for migration 050: drop archive_catalog
--
-- Destructive: discards the archive index (content hashes, integrity state).
-- Only the index is lost — the archive files themselves are untouched and the
-- catalog can be rebuilt by re-running the sync (forward) + backfill (history).

BEGIN;

DROP TABLE IF EXISTS archive_catalog;

DELETE FROM schema_migrations WHERE migration_name = '050_archive_catalog';

COMMIT;
