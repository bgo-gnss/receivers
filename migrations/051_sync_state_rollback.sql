-- Rollback for migration 051: drop sync_state
--
-- Destructive: discards the per-target sync watermarks. After a rollback the
-- next sync run re-bootstraps from the config cutover (re-scans from cutover
-- forward); idempotent upsert + --ignore-existing make that safe, just slower.

BEGIN;

DROP TABLE IF EXISTS sync_state;

DELETE FROM schema_migrations WHERE migration_name = '051_sync_state';

COMMIT;
