-- Rollback 054: drop the registry child tables and the enrichment columns.
--
-- Restores storage_location to its pre-054 shape (name/base_path/location_type/
-- is_primary/enabled) and re-adds the original location_type CHECK. Only safe if
-- no seed has written rows that depend on the new columns (dev/local use).

BEGIN;

DROP TABLE IF EXISTS receiver_buffer_depth;
DROP TABLE IF EXISTS storage_retention;

ALTER TABLE storage_location
    DROP COLUMN IF EXISTS protocol,
    DROP COLUMN IF EXISTS host,
    DROP COLUMN IF EXISTS root_path,
    DROP COLUMN IF EXISTS is_permanent;

-- Re-add the original narrow CHECK. Uses a guard so a row already violating it
-- (a 'logical'/'remote' location seeded under 054) does not abort the rollback
-- silently — the ALTER will raise, which is the correct loud failure.
ALTER TABLE storage_location
    ADD CONSTRAINT storage_location_location_type_check
    CHECK (location_type IN ('local', 'nfs', 'server'));

DELETE FROM schema_migrations WHERE migration_name = '054_storage_location_registry';

COMMIT;
