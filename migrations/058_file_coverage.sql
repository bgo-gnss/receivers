-- Migration 058: file_coverage + missing_rinex — the D8 observation-key differential
--
-- Part of the Unified File Index (docs/architecture/unified-file-index-plan.md,
-- milestone M2, slice 1). Builds the LINEAGE half of the differential (D8): group
-- archive_catalog rows by the OBSERVATION key (station, session_type, file_date,
-- file_hour) — the D7 logical key MINUS file_category — so every product of one
-- original download (raw, its rinex) lands in one group. From that grouping:
--
--   * file_coverage    — per-observation product-presence matrix (which tiers
--     exist, at which locations). MATERIALIZED so Grafana / worklists read a flat
--     indexed table, never a live GROUP BY over millions of rows (the 2026-05-27
--     pgdev-incident class). Refresh on a schedule via refresh_file_coverage().
--   * missing_rinex    — observations whose RAW ROOT is present but no rinex
--     product exists anywhere. A pure provenance question (D8): needs no
--     generate_series and yields no false positive for a date no raw ever existed.
--
-- Scope note: this slice is the DERIVED-tier differential (rinex expected iff a
-- raw root exists). The ROOT-tier differential (missing_on_receiver — raw
-- expected because the station was running) needs the bounded date-range
-- expected-set + per-source floors and lands in a later slice (058b).
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/058_file_coverage.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- file_coverage — per-observation product presence (MATERIALIZED).
-- One row per (station, session_type, file_date, file_hour). NULL file_hour for
-- daily groups all that day's daily products together (GROUP BY groups NULLs).
-- ---------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS file_coverage CASCADE;
CREATE MATERIALIZED VIEW file_coverage AS
SELECT
    station,
    session_type,
    file_date,
    file_hour,
    -- NULL hour folded to -1 as a REAL column so the unique index below is a
    -- plain column index (an expression unique index is riskier for REFRESH
    -- CONCURRENTLY) and daily (NULL) observations dedup cleanly.
    COALESCE(file_hour, -1)                                               AS obs_hour,
    -- presence by tier and location (bool_or over the observation's rows).
    -- NB: raw_archive/rinex_archive/raw_permanent are definitionally FALSE until
    -- the M4 gate populates imo_archive file_date (its rows are the load-bearing
    -- NULL-date legacy rows, excluded by the WHERE below). Until then coverage
    -- reflects the LOCAL ring only — see missing_rinex's advisory-only caveat.
    bool_or(file_category = 'raw'   AND storage_location = 'local_raw')    AS raw_local,
    bool_or(file_category = 'raw'   AND storage_location = 'imo_archive')  AS raw_archive,
    bool_or(file_category = 'rinex' AND storage_location = 'local_rinex')  AS rinex_local,
    bool_or(file_category = 'rinex' AND storage_location = 'imo_archive')  AS rinex_archive,
    bool_or(file_category = 'rinex' AND storage_location = 'epos_portal')  AS rinex_portal,
    -- tier presence ANYWHERE (the D8 root / derived questions)
    bool_or(file_category = 'raw')                                        AS raw_any,
    bool_or(file_category = 'rinex')                                      AS rinex_any,
    -- present at any PERMANENT location (safe fallback — never re-fetch from receiver)
    bool_or(file_category = 'raw'   AND storage_location = 'imo_archive')  AS raw_permanent,
    count(*)                                                              AS product_rows
FROM archive_catalog
WHERE station IS NOT NULL
  AND file_date IS NOT NULL
GROUP BY station, session_type, file_date, file_hour
WITH DATA;

-- Plain unique index on the (materialized) obs_hour column — the identity + the
-- row-matcher REFRESH ... CONCURRENTLY needs.
CREATE UNIQUE INDEX idx_file_coverage_obs
    ON file_coverage (station, session_type, file_date, obs_hour);

-- session/date scans for dashboards + the missing_rinex predicate.
CREATE INDEX idx_file_coverage_session_date
    ON file_coverage (session_type, file_date);

COMMENT ON MATERIALIZED VIEW file_coverage IS
    'Per-observation (station,session,date,hour) product-presence matrix over '
    'archive_catalog (D8 lineage grain). Materialized — refresh via '
    'refresh_file_coverage(); consumers read this, never the live GROUP BY.';

CREATE OR REPLACE FUNCTION refresh_file_coverage()
RETURNS VOID AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY file_coverage;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION refresh_file_coverage IS
    'Refresh the materialized file_coverage (CONCURRENTLY — no read lock). '
    'Schedule alongside the catalog writes / integrity checker.';

-- ---------------------------------------------------------------------------
-- missing_rinex — raw root present, no rinex anywhere (D8). A re-rinex worklist.
-- status_1hr is raw-only (never RINEXed) → excluded. rinex-only/legacy
-- observations (rinex present, no raw) are excluded by the raw_any requirement —
-- their rinex IS the root (D8 rinex_is_original), so nothing is "missing".
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW missing_rinex AS
SELECT station, session_type, file_date, file_hour,
       raw_local, raw_archive
FROM file_coverage
WHERE raw_any                                    -- a raw root exists (the origin)
  AND NOT rinex_any                              -- but no rinex product anywhere
  -- WHITELIST the rinex-producing sessions (fail closed: a new session is
  -- excluded until vetted, not silently flagged). Principled source is
  -- archive_format (session+category → produces rinex); hardcoded for this slice.
  AND session_type IN ('15s_24hr', '1Hz_1hr');

COMMENT ON VIEW missing_rinex IS
    'Observations with a raw root present but no rinex product (D8): re-rinex '
    'worklist. Excludes raw-only sessions and rinex-only legacy observations. '
    'ADVISORY ONLY until two gates land: (1) rinex_config_valid_from "needs TOS '
    'below this date" (old raws cannot be auto-rinexed); (2) the M4 imo_archive '
    'file_date backfill (until then rinex_any sees only the LOCAL ring, so a rinex '
    'pruned locally but present in the archive can false-positive). Do not wire to '
    'an automated re-rinex job before both land.';

INSERT INTO schema_migrations (migration_name)
VALUES ('058_file_coverage')
ON CONFLICT DO NOTHING;

COMMIT;
