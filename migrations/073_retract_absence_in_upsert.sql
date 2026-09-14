-- Migration 073: retract a contradicting absence wherever a file becomes present
--
-- c5a1e2a put the retraction in `FileTracker.mark_file_downloaded`. That is the
-- right seam for a DOWNLOAD and it is the only one: measured on rek-d01
-- 2026-09-14, 86 contradicted rows accumulated on GONH (43 `1Hz_1hr` + 43
-- `status_1hr`) because GONH is `acquisition_mode = stream` — its data arrives
-- through the stream capture, which calls `mark_file_archived`, never
-- `mark_file_downloaded`. A manual archive push produced a 87th, TERMINAL.
--
-- Every path that marks a file present already funnels through ONE place, and
-- it is this function:
--
--     mark_file_downloaded          file_tracker.py:412   'downloaded'
--     mark_file_archived            file_tracker.py:478   'archived'
--     sync_archive_to_db  (x2)      file_tracker.py:2219/2233
--     GapDetector                   file_tracker.py:2597
--
-- and through mark_file_archived, six more callers: the stream scheduler, the
-- integrity checker, external_fetch, three PolaRX5 sites and download_tracker.
-- Putting the retraction here covers all of them, and covers callers not yet
-- written — which is the point. Patching the two Python seams would have left
-- the next one to rediscover this.
--
-- WHY IT MATTERS. A non-terminal absence row is not inert: it keeps its
-- `confirmations` counter and stays a promotion candidate. Promote one and
-- `is_file_missing()` returns TRUE for a file in the archive, so gap detection
-- and the backfill skip a slot we hold. That is the failure migration 072 and
-- the whole #174/S2 line of work exist to prevent.
--
-- SCOPE. Guarded on `p_status IN ('archived','downloaded')`, so a 'missing',
-- 'error' or 'suspect' write never probes `file_absence` — those cannot
-- contradict anything. Restricted to `source_location = 'receiver'`, matching
-- the Python retraction and migration 072.
--
-- COST. The DELETE is a single probe of `file_absence_slot_uniq`, the UNIQUE
-- index on exactly (source_location, sid, session_type, file_date, file_hour)
-- NULLS NOT DISTINCT. `file_hour IS NOT DISTINCT FROM` mirrors that index; a
-- plain `=` never matches NULL and would silently skip every DAILY slot.
--
-- Everything else in the function is unchanged — this adds one guarded block
-- immediately before RETURN.
--
-- Usage:
--   psql -h localhost -d gps_health -f migrations/073_retract_absence_in_upsert.sql
--   psql -h pgdev.vedur.is -d gps_health -f migrations/073_retract_absence_in_upsert.sql

BEGIN;

CREATE OR REPLACE FUNCTION public.upsert_file_tracking(
    p_sid character varying, p_session_type character varying, p_date date,
    p_hour smallint, p_filename character varying, p_status character varying,
    p_file_size bigint DEFAULT NULL::bigint, p_samples integer DEFAULT NULL::integer,
    p_checksum character varying DEFAULT NULL::character varying,
    p_json_path character varying DEFAULT NULL::character varying,
    p_error text DEFAULT NULL::text,
    p_remote_file_size bigint DEFAULT NULL::bigint)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE
    v_id INTEGER;
BEGIN
    -- Try to find existing record
    IF p_hour IS NULL THEN
        SELECT id INTO v_id FROM file_tracking
        WHERE sid = p_sid AND session_type = p_session_type
          AND file_date = p_date AND file_hour IS NULL;
    ELSE
        SELECT id INTO v_id FROM file_tracking
        WHERE sid = p_sid AND session_type = p_session_type
          AND file_date = p_date AND file_hour = p_hour;
    END IF;

    IF v_id IS NOT NULL THEN
        -- Update existing
        UPDATE file_tracking SET
            filename = COALESCE(p_filename, filename),
            status = p_status,
            file_size = COALESCE(p_file_size, file_size),
            remote_file_size = COALESCE(p_remote_file_size, remote_file_size),
            last_checked = NOW(),
            last_attempt = CASE WHEN p_status IN ('downloaded', 'missing', 'error') THEN NOW() ELSE last_attempt END,
            download_count = CASE WHEN p_status IN ('downloaded', 'missing') THEN download_count + 1 ELSE download_count END,
            imported_to_db = CASE WHEN p_samples IS NOT NULL THEN TRUE ELSE imported_to_db END,
            imported_at = CASE WHEN p_samples IS NOT NULL THEN NOW() ELSE imported_at END,
            samples_imported = COALESCE(p_samples, samples_imported),
            import_checksum = COALESCE(p_checksum, import_checksum),
            json_written = CASE WHEN p_json_path IS NOT NULL THEN TRUE ELSE json_written END,
            json_path = COALESCE(p_json_path, json_path),
            json_written_at = CASE WHEN p_json_path IS NOT NULL THEN NOW() ELSE json_written_at END,
            last_error = p_error,
            error_count = CASE WHEN p_error IS NOT NULL THEN error_count + 1 ELSE error_count END,
            updated_at = NOW()
        WHERE id = v_id;
    ELSE
        -- Insert new
        INSERT INTO file_tracking (
            sid, session_type, file_date, file_hour, filename, status, file_size,
            remote_file_size,
            first_checked, last_checked, last_attempt, download_count,
            imported_to_db, imported_at, samples_imported, import_checksum,
            json_written, json_path, json_written_at, last_error, error_count
        ) VALUES (
            p_sid, p_session_type, p_date, p_hour, p_filename, p_status, p_file_size,
            p_remote_file_size,
            NOW(), NOW(), NOW(), 1,
            p_samples IS NOT NULL, CASE WHEN p_samples IS NOT NULL THEN NOW() END, p_samples, p_checksum,
            p_json_path IS NOT NULL, p_json_path, CASE WHEN p_json_path IS NOT NULL THEN NOW() END,
            p_error, CASE WHEN p_error IS NOT NULL THEN 1 ELSE 0 END
        ) RETURNING id INTO v_id;
    END IF;

    -- MIGRATION 073: this slot is now demonstrably present, so a
    -- "absent on the receiver" row for it is a contradiction. Retract it.
    IF p_status IN ('archived', 'downloaded') THEN
        DELETE FROM file_absence
         WHERE source_location = 'receiver'
           AND sid = p_sid
           AND session_type = p_session_type
           AND file_date = p_date
           AND file_hour IS NOT DISTINCT FROM p_hour;
    END IF;

    RETURN v_id;
END;
$function$;

INSERT INTO schema_migrations (migration_name)
VALUES ('073_retract_absence_in_upsert')
ON CONFLICT DO NOTHING;

COMMIT;
