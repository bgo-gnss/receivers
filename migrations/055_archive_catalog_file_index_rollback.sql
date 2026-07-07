-- Rollback 055: drop the file-index columns + indexes from archive_catalog.
--
-- Additive migration, so the rollback is a clean drop. Safe on dev/local;
-- on a populated index it discards file_hour/compressed_sha256/md5 data.

BEGIN;

DROP INDEX IF EXISTS idx_archive_catalog_compressed_sha256;
DROP INDEX IF EXISTS idx_archive_catalog_logical;

ALTER TABLE archive_catalog
    DROP COLUMN IF EXISTS file_hour,
    DROP COLUMN IF EXISTS compressed_sha256,
    DROP COLUMN IF EXISTS md5checksum,
    DROP COLUMN IF EXISTS md5uncompressed;

DELETE FROM schema_migrations WHERE migration_name = '055_archive_catalog_file_index';

COMMIT;
