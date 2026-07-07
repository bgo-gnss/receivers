-- Migration 060: missing_on_receiver + needs_repull + rinex_org root fold
--
-- Part of the Unified File Index (docs/architecture/unified-file-index-plan.md,
-- milestone M2, slice 2a — the ROOT-tier differential). Where slice 1
-- (missing_rinex) was a pure catalog join, this synthesizes the EXPECTED set of
-- raw files a running station should have produced and diffs it against what is
-- present / confirmed-absent — the "which raw to fetch from the receiver"
-- worklist, replacing the directory glob.
--
-- HARD RULES baked in (plan §3.5, adversarially reviewed):
--   * Session-map: a station is only expected to produce the sessions its
--     receiver_type has floors for (receiver_buffer_depth) — NetRS = 15s only.
--     Joined on lower(receiver_type) (stations stores 'PolaRX5', the seed
--     'polarx5'); mosaic-x5 seeded here as a polarx5-equivalent.
--   * Receiver-horizon floor: bound the expected set to the recent, still-
--     fetchable window (current_date - depth_days). Keeps generate_series small
--     and never routes a long-aged file to the receiver (→ needs_repull instead).
--     Static depth now; the real per-station receiver horizon is slice 2b.
--   * Ceiling = last FULLY-ELAPSED slot in UTC (daily → yesterday; hourly → the
--     last complete hour). NEVER the current period, or a not-yet-produced slot
--     is marked absent = data loss. Iceland is UTC year-round.
--   * Daily → ONE NULL-hour row; hourly → 24 rows. Anti-joins are NULL-safe
--     (IS NOT DISTINCT FROM), or a terminal daily slot re-appears forever.
--   * Lifecycle: only active stations (status NULL, not passive/inactive/
--     discontinued); bounded [data_start, coalesce(data_end, ceiling)].
--   * "Present" = a RAW ROOT exists (raw OR rinex_org, D8) — a legit rinex-only
--     observation is not "missing raw".
--
-- ADVISORY: the receiver floor uses the CONSERVATIVE static receiver_buffer_depth
-- seeds (marked "validate"), so missing_on_receiver may over- or under-report the
-- true fetchable window until slice 2b probes the real receiver horizon. Do not
-- wire to an automated fetch loop before then.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/060_missing_on_receiver.sql

BEGIN;

-- mosaic-X5 is a PolaRX5 subclass (same sessions) — give it a session-map/floor.
INSERT INTO receiver_buffer_depth (receiver_type, session_type, depth_days, notes) VALUES
    ('mosaic-x5', '1Hz_1hr',    7,  'conservative default — mosaic-X5 (PolaRX5 subclass), validate'),
    ('mosaic-x5', '15s_24hr',   30, 'conservative default — mosaic-X5 (PolaRX5 subclass), validate'),
    ('mosaic-x5', 'status_1hr', 7,  'conservative default — mosaic-X5 (PolaRX5 subclass), validate')
ON CONFLICT (receiver_type, session_type) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Recreate file_coverage with the rinex_org root fold (D8). Ordered drop (no
-- CASCADE): missing_rinex depends on file_coverage.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS missing_rinex;
DROP MATERIALIZED VIEW IF EXISTS file_coverage;

CREATE MATERIALIZED VIEW file_coverage AS
SELECT
    station,
    session_type,
    file_date,
    file_hour,
    COALESCE(file_hour, -1)                                               AS obs_hour,
    bool_or(file_category = 'raw'   AND storage_location = 'local_raw')    AS raw_local,
    bool_or(file_category = 'raw'   AND storage_location = 'imo_archive')  AS raw_archive,
    bool_or(file_category = 'rinex' AND storage_location = 'local_rinex')  AS rinex_local,
    bool_or(file_category = 'rinex' AND storage_location = 'imo_archive')  AS rinex_archive,
    bool_or(file_category = 'rinex' AND storage_location = 'epos_portal')  AS rinex_portal,
    -- rinex_org = the immutable preserved original for a rinex-only observation
    -- (header_fix.py). Its presence means BOTH a root exists AND the rinex
    -- deliverable exists (D8 rinex_is_original). Inert until these rows carry a
    -- file_date (same short-daily parser gap as the legacy archive rows).
    bool_or(file_category = 'rinex_org')                                  AS rinex_org_any,
    bool_or(file_category = 'raw')                                        AS raw_any,
    bool_or(file_category = 'rinex')                                      AS rinex_any,
    -- D8 root: a raw exists OR the observation is a preserved rinex-only original.
    bool_or(file_category IN ('raw', 'rinex_org'))                        AS root_any,
    bool_or(file_category = 'raw'   AND storage_location = 'imo_archive')  AS raw_permanent,
    count(*)                                                              AS product_rows
FROM archive_catalog
WHERE station IS NOT NULL
  AND file_date IS NOT NULL
GROUP BY station, session_type, file_date, file_hour
WITH DATA;

CREATE UNIQUE INDEX idx_file_coverage_obs
    ON file_coverage (station, session_type, file_date, obs_hour);
CREATE INDEX idx_file_coverage_session_date
    ON file_coverage (session_type, file_date);

COMMENT ON MATERIALIZED VIEW file_coverage IS
    'Per-observation (station,session,date,hour) product-presence matrix over '
    'archive_catalog (D8 lineage grain). root_any = raw OR rinex_org (the D8 '
    'root). Materialized — refresh via refresh_file_coverage().';

CREATE OR REPLACE FUNCTION refresh_file_coverage()
RETURNS VOID AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY file_coverage;
END;
$$ LANGUAGE plpgsql;

-- missing_rinex (root-aware): a raw root present but no rinex product anywhere.
-- rinex_org counts as rinex-present, so a preserved-original observation is never
-- flagged. Advisory-only (see mig 058 comment; same gates).
CREATE OR REPLACE VIEW missing_rinex AS
SELECT station, session_type, file_date, file_hour, raw_local, raw_archive
FROM file_coverage
WHERE raw_any
  AND NOT rinex_any
  AND NOT rinex_org_any
  AND session_type IN ('15s_24hr', '1Hz_1hr');

COMMENT ON VIEW missing_rinex IS
    'Observations with a raw root present but no rinex product (D8). Excludes '
    'raw-only sessions and rinex-only (rinex_org) originals. ADVISORY until the '
    'rinex_config_valid_from gate + the M4 imo_archive file_date backfill land.';

-- ---------------------------------------------------------------------------
-- needs_repull_from_archive — a pure catalog join (no generate_series): a raw
-- present at a PERMANENT location but absent from the LOCAL ring. Copy from the
-- archive, never re-fetch from the receiver. Inert until imo_archive rows carry
-- a file_date (M4), then correct automatically.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW needs_repull_from_archive AS
SELECT station, session_type, file_date, file_hour
FROM file_coverage
WHERE raw_permanent           -- raw is safe in the permanent archive
  AND NOT raw_local;          -- but not in the local ring

COMMENT ON VIEW needs_repull_from_archive IS
    'Raw present in the permanent archive but missing from the local ring — copy '
    'from archive, do not re-fetch from the receiver. Inert until imo_archive '
    'file_date is populated (M4).';

-- ---------------------------------------------------------------------------
-- missing_on_receiver (MATERIALIZED) — the actionable "fetch from receiver" list.
-- Expected (session-map, receiver-horizon floor, last-complete-slot ceiling)
-- MINUS present-root MINUS terminal-absent.
-- ---------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS missing_on_receiver;
CREATE MATERIALIZED VIEW missing_on_receiver AS
WITH station_session AS (
    SELECT s.sid,
           d.session_type,
           d.depth_days,
           s.data_start,
           s.data_end,
           -- hourly iff the session name carries '1hr' (mirrors DownloadTracker)
           (lower(d.session_type) LIKE '%1hr%') AS is_hourly
    FROM stations s
    JOIN receiver_buffer_depth d ON d.receiver_type = lower(s.receiver_type)
    WHERE s.station_status IS NULL                    -- active only
      AND (s.health_check IS DISTINCT FROM 'passive') -- not passively monitored
      AND s.receiver_type IS NOT NULL
      AND s.data_start IS NOT NULL                    -- need a floor
),
expected AS (
    -- daily sessions: one NULL-hour row per date up to the last complete day (UTC)
    SELECT ss.sid, ss.session_type, gd::date AS file_date, NULL::smallint AS file_hour
    FROM station_session ss
    CROSS JOIN generate_series(
        greatest(ss.data_start, current_date - ss.depth_days)::timestamp,
        least(coalesce(ss.data_end, current_date - 1), current_date - 1)::timestamp,
        interval '1 day'
    ) AS gd
    WHERE NOT ss.is_hourly

    UNION ALL

    -- hourly sessions: 24 slots/day, only those whose hour has FULLY elapsed (UTC)
    SELECT ss.sid, ss.session_type, gd::date AS file_date, gh::smallint AS file_hour
    FROM station_session ss
    CROSS JOIN generate_series(
        greatest(ss.data_start, current_date - ss.depth_days)::timestamp,
        least(coalesce(ss.data_end, current_date), current_date)::timestamp,
        interval '1 day'
    ) AS gd
    CROSS JOIN generate_series(0, 23) AS gh
    WHERE ss.is_hourly
      AND (gd::date + make_interval(hours => gh) + interval '1 hour')
          <= (now() AT TIME ZONE 'UTC')
)
SELECT e.sid AS station, e.session_type, e.file_date, e.file_hour,
       COALESCE(e.file_hour, -1) AS obs_hour
FROM expected e
LEFT JOIN file_coverage fc
       ON fc.station = e.sid
      AND fc.session_type = e.session_type
      AND fc.file_date = e.file_date
      AND fc.file_hour IS NOT DISTINCT FROM e.file_hour
LEFT JOIN file_absence fa
       ON fa.source_location = 'receiver'
      AND fa.sid = e.sid
      AND fa.session_type = e.session_type
      AND fa.file_date = e.file_date
      AND fa.file_hour IS NOT DISTINCT FROM e.file_hour
      AND fa.terminal
WHERE (fc.station IS NULL OR NOT fc.root_any)   -- no raw root present anywhere
  AND fa.sid IS NULL                            -- and not confirmed terminally absent
WITH DATA;

CREATE UNIQUE INDEX idx_missing_on_receiver_obs
    ON missing_on_receiver (station, session_type, file_date, obs_hour);
CREATE INDEX idx_missing_on_receiver_station
    ON missing_on_receiver (station, session_type);

COMMENT ON MATERIALIZED VIEW missing_on_receiver IS
    'Expected raw slots (session-map + receiver-horizon floor + last-complete-slot '
    'UTC ceiling) with no raw root present and not terminally absent — the fetch-'
    'from-receiver worklist. Materialized; refresh via refresh_missing_on_receiver(). '
    'ADVISORY (static receiver_buffer_depth floor) until slice 2b probes the real '
    'per-station receiver horizon.';

CREATE OR REPLACE FUNCTION refresh_missing_on_receiver()
RETURNS VOID AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY missing_on_receiver;
END;
$$ LANGUAGE plpgsql;

INSERT INTO schema_migrations (migration_name)
VALUES ('060_missing_on_receiver')
ON CONFLICT DO NOTHING;

COMMIT;
