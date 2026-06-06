-- Rollback for migration 048 — drop the unified snapshot schema.
--
-- Safe to run before any production data lives in these tables. After Phase 3
-- (aggregator service writing data) and Phase 6 (dashboards reading from
-- station_snapshot_60s) this rollback becomes data-destructive: data in
-- station_*_60s would be lost if migration 048 is rolled back, and dashboards
-- would break until block_*_status reads are restored.
--
-- TimescaleDB-specific notes:
--   - drop_chunks is not needed; DROP TABLE on a hypertable removes all chunks.
--   - Continuous aggregates must be dropped before the source hypertable.
--   - Compression / retention / refresh policies are removed automatically
--     when the table or CAGG is dropped.
--   - The timescaledb extension is NOT removed — it may still be in use by
--     other tables added later.

BEGIN;

-- 1.  View first (depends on the dense tables)
DROP VIEW IF EXISTS station_snapshot_60s;

-- 2.  Continuous aggregate (depends on station_health_60s)
DROP MATERIALIZED VIEW IF EXISTS station_health_1h;

-- 3.  Hypertables (compression / retention policies dropped automatically)
DROP TABLE IF EXISTS station_sat_signal_60s;
DROP TABLE IF EXISTS station_sat_summary_60s;
DROP TABLE IF EXISTS station_network_60s;
DROP TABLE IF EXISTS station_health_60s;

COMMIT;
