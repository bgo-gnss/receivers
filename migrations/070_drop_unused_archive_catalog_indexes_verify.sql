-- Verify migration 070. Every check must report PASS.

\echo === 1. the four indexes are gone ===
SELECT CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL: ' || string_agg(indexname, ', ') END
       AS dropped_check
FROM pg_indexes
WHERE indexname IN ('idx_archive_catalog_sha256',
                    'idx_archive_catalog_compressed_sha256',
                    'idx_pvt_geodetic_sid_ts',
                    'idx_logging_status_sid_ts');

\echo === 2. the primary keys that now serve those lookups are intact ===
SELECT CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL: expected 2, got ' || count(*) END
       AS pk_check
FROM pg_indexes
WHERE indexname IN ('block_pvt_geodetic_pkey', 'block_logging_status_pkey');

\echo === 3. the indexes deliberately KEPT are still present ===
SELECT CASE WHEN count(*) = 4 THEN 'PASS' ELSE 'FAIL: expected 4, got ' || count(*) END
       AS kept_check
FROM pg_indexes
WHERE indexname IN ('idx_satellite_tracking_sid_ts',
                    'idx_power_status_sid_ts',
                    'idx_receiver_status_sid_ts',
                    'idx_file_coverage_obs');

\echo === 4. the migration is recorded ===
SELECT CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END AS recorded_check
FROM schema_migrations
WHERE migration_name = '070_drop_unused_archive_catalog_indexes';

\echo === 5. reclaimed space (informational, expect DB total down ~3.75 GB) ===
SELECT pg_size_pretty(pg_database_size('gps_health')) AS db_size_now;

\echo === 6. the PK is actually used for a descending lookup (plan check) ===
EXPLAIN (COSTS OFF)
SELECT * FROM block_pvt_geodetic WHERE sid = 'GONH' ORDER BY ts DESC LIMIT 10;
