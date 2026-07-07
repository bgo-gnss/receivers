-- Rollback 060: drop missing_on_receiver + needs_repull, restore the mig-058
-- file_coverage / missing_rinex (without the rinex_org root fold), remove the
-- mosaic-x5 seeds. Ordered drops (no CASCADE).

BEGIN;

DROP FUNCTION IF EXISTS refresh_missing_on_receiver();
DROP MATERIALIZED VIEW IF EXISTS missing_on_receiver;
DROP VIEW IF EXISTS needs_repull_from_archive;

-- Restore the mig-058 file_coverage + missing_rinex (no rinex_org / root_any).
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
    bool_or(file_category = 'raw')                                        AS raw_any,
    bool_or(file_category = 'rinex')                                      AS rinex_any,
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

CREATE OR REPLACE FUNCTION refresh_file_coverage()
RETURNS VOID AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY file_coverage;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE VIEW missing_rinex AS
SELECT station, session_type, file_date, file_hour, raw_local, raw_archive
FROM file_coverage
WHERE raw_any
  AND NOT rinex_any
  AND session_type IN ('15s_24hr', '1Hz_1hr');

DELETE FROM receiver_buffer_depth WHERE receiver_type = 'mosaic-x5';

DELETE FROM schema_migrations WHERE migration_name = '060_missing_on_receiver';

COMMIT;
