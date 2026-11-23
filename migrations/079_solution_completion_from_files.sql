-- Migration 079: completion records inferred from the files (slice 4).
--
-- yang's processing writes no record of a finished run. The checker infers
-- one: the run for day D is done when at least half of the stations expected
-- on D have a valid solution for D (measured 2026-11-23: a processed day sits
-- at 89-91 %, an unprocessed one at 0 %). Each check run sends one record per
-- contiguous range of such days (completion_id 'files:<run_id>:<day_from>'),
-- written here into the solution_completion table 077 created, in the same
-- transaction as record_solution_check. solution_day_grid needs no change:
-- an absent station on a covered day after the deadline becomes
-- overdue/absent_after_completion instead of overdue/no_completion_record.
--
-- Volume: a few rows per hourly run (~100 a day); no retention yet.
--
-- Apply as the OWNER (bgo), on each host, BEFORE the code that sends
-- completions:
--   psql -d gps_health -v ON_ERROR_STOP=1 -f migrations/079_solution_completion_from_files.sql
-- Reversible: 079_solution_completion_from_files_rollback.sql.

BEGIN;

CREATE OR REPLACE FUNCTION record_solution_completions(payload jsonb)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    v_at     timestamptz := (payload ->> 'checked_at')::timestamptz;
    v_stream text        := payload ->> 'stream';
    v_n      integer;
BEGIN
    IF v_at IS NULL OR v_stream IS NULL THEN
        RAISE EXCEPTION 'record_solution_completions: stream and checked_at are required';
    END IF;
    IF payload -> 'completions' IS NULL THEN
        RETURN 0;
    END IF;
    IF jsonb_typeof(payload -> 'completions') <> 'array' THEN
        RAISE EXCEPTION 'record_solution_completions: completions must be an array';
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_to_recordset(payload -> 'completions')
                   AS c(completion_id text, day_from date, day_to date)
               WHERE c.completion_id IS NULL OR c.day_from IS NULL OR c.day_to IS NULL
                  OR c.completion_id !~ '^files:') THEN
        RAISE EXCEPTION 'record_solution_completions: each completion needs a files: id, day_from and day_to';
    END IF;
    INSERT INTO solution_completion (completion_id, stream, completed_at, day_from, day_to)
    SELECT c.completion_id, v_stream, v_at, c.day_from, c.day_to
    FROM jsonb_to_recordset(payload -> 'completions')
         AS c(completion_id text, day_from date, day_to date)
    ON CONFLICT (completion_id) DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END
$fn$;

REVOKE ALL ON FUNCTION record_solution_completions(jsonb) FROM PUBLIC;

DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gpsops') THEN
        GRANT EXECUTE ON FUNCTION record_solution_completions(jsonb) TO gpsops;
    END IF;
END
$grants$;

INSERT INTO schema_migrations (migration_name)
VALUES ('079_solution_completion_from_files')
ON CONFLICT DO NOTHING;

COMMIT;
