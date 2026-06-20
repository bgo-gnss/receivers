-- Rollback for migration 052: drop file_tracking content-hash columns
--
-- Destructive: discards locally-computed content_sha256 values. After a rollback
-- the integrity checker re-hashes present files from scratch (compute-on-the-fly),
-- so no data is lost — only the cached local hashes.

BEGIN;

DROP INDEX IF EXISTS idx_file_tracking_needs_hash;
DROP INDEX IF EXISTS idx_file_tracking_content_sha256;

ALTER TABLE file_tracking
    DROP COLUMN IF EXISTS content_hashed_at,
    DROP COLUMN IF EXISTS content_sha256;

DELETE FROM schema_migrations WHERE migration_name = '052_file_tracking_content_hash';

COMMIT;
