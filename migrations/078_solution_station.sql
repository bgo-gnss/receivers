-- Migration 078: per-station facts for the GPS Daily Solutions board.
--
-- solution_station   one row per roster station: is it local (in Iceland and
--                    not inactive, per stations.cfg), and its agency
--                    (station_owner). The board's "Stations: local / all"
--                    option and its Agency filter read it.
--
-- Why not the stations table: it holds only the stations IMO health-checks, so
-- HOFN, REYK and stations run by others (BJAC) are missing from it. Why not a
-- column on solution_roster_station: that snapshot is replaced only when the
-- roster FILE changes, and these facts come from stations.cfg, which changes
-- on its own. So every check run refreshes them here, through
-- record_solution_stations(payload), in the same transaction as
-- record_solution_check. A late older run never overwrites a newer fact.
--
-- Apply as the OWNER (bgo), on each host, BEFORE the code that sends
-- station_info is deployed (record_check calls the function only when the
-- payload carries station_info, but then it must exist):
--   psql -d gps_health -v ON_ERROR_STOP=1 -f migrations/078_solution_station.sql
-- Reversible: 078_solution_station_rollback.sql.

BEGIN;

CREATE TABLE IF NOT EXISTS solution_station (
    station     VARCHAR(4)  PRIMARY KEY CHECK (station ~ '^[A-Z0-9]{4}$'),
    is_local    BOOLEAN     NOT NULL,
    agency      TEXT,                       -- stations.cfg station_owner; NULL = none
    as_of       TIMESTAMPTZ NOT NULL,       -- checked_at of the run that wrote it
    run_id      UUID        NOT NULL
);

CREATE OR REPLACE FUNCTION record_solution_stations(payload jsonb)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    v_at  timestamptz := (payload ->> 'checked_at')::timestamptz;
    v_run uuid        := (payload ->> 'run_id')::uuid;
    v_n   integer;
BEGIN
    IF v_at IS NULL OR v_run IS NULL THEN
        RAISE EXCEPTION 'record_solution_stations: run_id and checked_at are required';
    END IF;
    IF payload -> 'station_info' IS NULL THEN
        RETURN 0;
    END IF;
    IF jsonb_typeof(payload -> 'station_info') <> 'array' THEN
        RAISE EXCEPTION 'record_solution_stations: station_info must be an array';
    END IF;
    INSERT INTO solution_station AS s (station, is_local, agency, as_of, run_id)
    SELECT i.station, i.local, nullif(btrim(i.agency), ''), v_at, v_run
    FROM jsonb_to_recordset(payload -> 'station_info')
         AS i(station text, local boolean, agency text)
    ON CONFLICT (station) DO UPDATE
        SET is_local = EXCLUDED.is_local, agency = EXCLUDED.agency,
            as_of = EXCLUDED.as_of, run_id = EXCLUDED.run_id
        WHERE s.as_of <= EXCLUDED.as_of;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END
$fn$;

REVOKE ALL ON FUNCTION record_solution_stations(jsonb) FROM PUBLIC;

DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gpsops') THEN
        GRANT EXECUTE ON FUNCTION record_solution_stations(jsonb) TO gpsops;
        GRANT SELECT ON solution_station TO gpsops;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_read') THEN
        GRANT SELECT ON solution_station TO grafana_read;
    END IF;
END
$grants$;

INSERT INTO schema_migrations (migration_name)
VALUES ('078_solution_station')
ON CONFLICT DO NOTHING;

COMMIT;
