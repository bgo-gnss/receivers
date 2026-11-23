-- Rollback 080 — restore 078's writer and drop the operator column. Revert the
-- board's Operator filter first, or it fails on the missing column.

BEGIN;
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
ALTER TABLE solution_station DROP COLUMN IF EXISTS operator;
DELETE FROM schema_migrations WHERE migration_name = '080_solution_station_operator';
COMMIT;
