-- Migration 070: drop four never-scanned indexes (3.75 GB, ~12 % of the DB)
--
-- Sibling of migration 065, which did this for file_tracking. Measured on
-- rek-d01 2026-09-13. pg_stat_database.stats_reset is NULL, so every counter
-- below covers the ENTIRE life of the database — "0 scans" is not an artifact
-- of a recent reset.
--
--   idx_archive_catalog_sha256             1287 MB   idx_scan = 0   idx_tup_read = 0
--   idx_pvt_geodetic_sid_ts                1233 MB   idx_scan = 0   idx_tup_read = 0
--   idx_archive_catalog_compressed_sha256   986 MB   idx_scan = 0   idx_tup_read = 0
--   idx_logging_status_sid_ts                335 MB   idx_scan = 0   idx_tup_read = 0
--                                          -------
--                                          3841 MB   against a 32 GB database
--
-- WHY EACH ONE IS DEAD
--
-- The two sha256 indexes: nothing in the tree looks a row up BY hash. Every
-- occurrence of content_sha256 / compressed_sha256 in a WHERE clause is an
-- `IS NOT NULL` index predicate or a COUNT filter; archive/verify.py SELECTs
-- the column but keys on (storage_location, session_type, file_category,
-- canonical_key). Same story migration 065 recorded for file_tracking: built
-- for "dedup lookups by content hash", a feature that was never written.
--
-- The two (sid, ts DESC) indexes are redundant with their tables' primary keys:
--
--   block_pvt_geodetic_pkey     btree (sid, ts)        <- PK
--   idx_pvt_geodetic_sid_ts     btree (sid, ts DESC)   <- this
--   block_logging_status_pkey   btree (sid, ts)        <- PK
--   idx_logging_status_sid_ts   btree (sid, ts DESC)   <- this
--
-- PostgreSQL scans a btree backwards, so the PK already serves both orderings
-- of the same leading column. Both have never been chosen by the planner, and
-- that was verified by plan inspection rather than inferred from the counters —
-- on rek-d01 2026-09-13, for the exact descending query these indexes exist for:
--
--   EXPLAIN SELECT * FROM block_logging_status WHERE sid='GONH'
--           ORDER BY ts DESC LIMIT 10;
--     -> Index Scan Backward using block_logging_status_pkey
--
--   EXPLAIN SELECT * FROM block_pvt_geodetic WHERE sid='GONH'
--           ORDER BY ts DESC LIMIT 10;
--     -> Index Scan using idx_pvt_geodetic_ts  (Filter on sid)
--
-- The second is worth noting: that query picks the 303 MB ts-only index, NOT
-- the (sid, ts DESC) one and NOT the PK. So idx_pvt_geodetic_ts (idx_scan = 2,
-- 43M tuples read) IS live and must NOT be dropped, even though its scan count
-- looks negligible. Scan counts alone would have got that backwards.
--
-- THE COST IS WRITES, NOT DISK (the point migration 065 makes)
--
-- block_pvt_geodetic and block_logging_status take ~150,000 inserts/day EACH
-- from the 5-minute health sweep (47,800 checks/day). Every insert maintains
-- every index on the table. Dropping these stops ~300,000 index-entry writes
-- per day that no query has ever read, and removes 3.75 GB from the buffer
-- cache's working set on a host where shared_buffers is still the 128 MB
-- install default and the cache hit ratio is 85.3 %.
--
-- DELIBERATELY NOT DROPPED — the 2026-09-13 review proposed these and it was
-- WRONG about them:
--
--   idx_satellite_tracking_sid_ts  1213 MB  idx_scan = 564,385  (43.8M tuples)
--       Heavily used. The review called it a redundant (sid, ts DESC) twin.
--   idx_power_status_sid_ts        1223 MB  idx_scan =   1,451
--   idx_receiver_status_sid_ts     1227 MB  idx_scan =     400
--       Low but NON-zero. Dropping them would push real queries onto the PK
--       backward scan. Probably fine, but "probably" is not a reason to drop
--       2.4 GB of index that something demonstrably reads. Revisit with
--       pg_stat_statements (not yet installed) showing which queries they are.
--   idx_file_coverage_obs           499 MB  idx_scan = 0, but UNIQUE
--       REFRESH MATERIALIZED VIEW CONCURRENTLY requires a unique index. Drop it
--       only together with the file_coverage matview itself, not on its own.
--   archive_catalog_pkey            259 MB  idx_scan = 0, but it is the PK
--       Backs the primary key constraint; cannot be dropped independently.
--
-- CONCURRENTLY so no table is locked against the live health writers. That
-- means these statements CANNOT run inside a transaction block — apply this
-- file with psql directly (no BEGIN/COMMIT wrapper), and run it on rek-d01 AND
-- pgdev: DDL is not mirrored by the dual-write path.

DROP INDEX CONCURRENTLY IF EXISTS idx_archive_catalog_sha256;
DROP INDEX CONCURRENTLY IF EXISTS idx_archive_catalog_compressed_sha256;
DROP INDEX CONCURRENTLY IF EXISTS idx_pvt_geodetic_sid_ts;
DROP INDEX CONCURRENTLY IF EXISTS idx_logging_status_sid_ts;

-- NB: no BEGIN/COMMIT wrapper, unlike every other migration here. DROP INDEX
-- CONCURRENTLY is forbidden inside a transaction block, and locking these
-- tables against the 5-minute health writers is not worth the tidiness.
INSERT INTO schema_migrations (migration_name)
VALUES ('070_drop_unused_archive_catalog_indexes')
ON CONFLICT DO NOTHING;
