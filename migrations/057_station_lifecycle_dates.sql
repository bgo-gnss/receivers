-- Migration 057: stations.data_start / data_end — expected-set bounds
--
-- Part of the Unified File Index (docs/architecture/unified-file-index-plan.md,
-- milestone M2). The differential's expected set must be bounded per station at
-- BOTH ends, or an unbounded generate_series flags a station discontinued years
-- ago as missing every day forever (plan §3.5 blocker).
--
--   * data_start — first date the station produced data (the expected-set floor).
--   * data_end   — last date it produced (NULL = still active; the ceiling is
--     then last-complete-period-UTC).
--
-- The authoritative source is TOS date_start/date_end (§3.6), synced later — that
-- is an ACCURACY upgrade, not a blocker. This migration ships the columns plus a
-- fallback that fills data_start from the EARLIEST OBSERVED file_date per station
-- (archive_catalog ∪ file_tracking), so M2d can bound the raw expected-set today.
-- The fallback UNDER-reports gaps older than the earliest observed row (honest
-- limitation until the TOS sync / history backfill lands) and only fills NULLs,
-- so an authoritative TOS date is never overwritten by the fallback.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/057_station_lifecycle_dates.sql

BEGIN;

ALTER TABLE stations
    ADD COLUMN IF NOT EXISTS data_start DATE,
    ADD COLUMN IF NOT EXISTS data_end   DATE;

COMMENT ON COLUMN stations.data_start IS
    'First date the station produced data — the differential expected-set floor. '
    'Authoritative source is TOS date_start (synced later); fallback is the '
    'earliest observed file_date (sync_station_dates_from_observed).';
COMMENT ON COLUMN stations.data_end IS
    'Last date the station produced data; NULL = still active (ceiling is then '
    'last-complete-period-UTC). Authoritative source is TOS date_end.';

-- Fallback population: earliest observed file_date per station, filling NULLs
-- only (never clobbers a TOS-authoritative value). Returns rows updated.
CREATE OR REPLACE FUNCTION sync_station_dates_from_observed()
RETURNS INTEGER AS $$
DECLARE
    n INTEGER;
BEGIN
    WITH observed AS (
        SELECT sid, min(d) AS min_date
        FROM (
            SELECT station AS sid, file_date AS d
            FROM archive_catalog WHERE file_date IS NOT NULL AND station IS NOT NULL
            UNION ALL
            SELECT sid, file_date
            FROM file_tracking WHERE file_date IS NOT NULL
        ) u
        GROUP BY sid
    )
    UPDATE stations s
       SET data_start = observed.min_date,
           updated_at = now()
      FROM observed
     WHERE s.sid = observed.sid
       AND s.data_start IS NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION sync_station_dates_from_observed IS
    'Fallback: fill stations.data_start (NULLs only) from the earliest observed '
    'file_date (archive_catalog ∪ file_tracking). TOS sync is authoritative.';

INSERT INTO schema_migrations (migration_name)
VALUES ('057_station_lifecycle_dates')
ON CONFLICT DO NOTHING;

COMMIT;
