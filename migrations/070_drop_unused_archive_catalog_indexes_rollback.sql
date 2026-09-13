-- Rollback for migration 070. Recreates the four dropped indexes exactly as
-- they were defined (captured from pg_indexes on rek-d01 before the drop).
--
-- Rebuilding costs ~3.75 GB of disk and a full scan of each table
-- (archive_catalog 9.45M rows, block_pvt_geodetic and block_logging_status
-- ~21M rows each), so expect this to take minutes, not seconds. CONCURRENTLY
-- again, so the live health writers are not blocked.
--
-- Note: if you are rolling back because a query got slower, capture the plan
-- FIRST (EXPLAIN ANALYZE) — none of these four had ever been chosen by the
-- planner over the entire recorded life of the database, so a regression here
-- would be new information worth keeping.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_archive_catalog_sha256
    ON public.archive_catalog USING btree (content_sha256);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_archive_catalog_compressed_sha256
    ON public.archive_catalog USING btree (compressed_sha256)
    WHERE compressed_sha256 IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pvt_geodetic_sid_ts
    ON public.block_pvt_geodetic USING btree (sid, ts DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_logging_status_sid_ts
    ON public.block_logging_status USING btree (sid, ts DESC);

DELETE FROM schema_migrations
 WHERE migration_name = '070_drop_unused_archive_catalog_indexes';
