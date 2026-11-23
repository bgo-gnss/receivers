-- Migration 080: the station's operator on solution_station.
--
-- stations.cfg separates the owner (station_owner, the board's Agency filter)
-- from the operator (station_operator: who runs the station day to day; IMO
-- runs stations owned by KAUST, UI, PSU ...). The board's Operator filter lets
-- an IMO operator see only the stations IMO operates. record_solution_stations
-- now stores it too; same rule as agency (empty = NULL, a late older run never
-- overwrites a newer fact).
--
-- Order: 078's writer ignores the new key, so the code may go out before or
-- after this; the BOARD needs the column, so apply this first. Apply as the
-- OWNER (bgo), on each host:
--   psql -d gps_health -v ON_ERROR_STOP=1 -f migrations/080_solution_station_operator.sql
-- Reversible: 080_solution_station_operator_rollback.sql.

BEGIN;

ALTER TABLE solution_station
    ADD COLUMN IF NOT EXISTS operator TEXT;   -- stations.cfg station_operator; NULL = none

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
    INSERT INTO solution_station AS s (station, is_local, agency, operator, as_of, run_id)
    SELECT i.station, i.local, nullif(btrim(i.agency), ''),
           nullif(btrim(i.operator), ''), v_at, v_run
    FROM jsonb_to_recordset(payload -> 'station_info')
         AS i(station text, local boolean, agency text, operator text)
    ON CONFLICT (station) DO UPDATE
        SET is_local = EXCLUDED.is_local, agency = EXCLUDED.agency,
            operator = EXCLUDED.operator,
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
    END IF;
END
$grants$;

INSERT INTO schema_migrations (migration_name)
VALUES ('080_solution_station_operator')
ON CONFLICT DO NOTHING;

COMMIT;
