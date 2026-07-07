-- Migration 061: record_file_absence — the SERVED-GATE on terminal promotion
--
-- Part of the Unified File Index (docs/architecture/unified-file-index-plan.md,
-- milestone M2, slice 2b). Makes the file_absence ledger trustworthy enough to
-- drive a real skip (the prerequisite for flipping is_file_missing's
-- p_use_terminal on).
--
-- The hole mig 059 left advisory: terminal absence is earned by repeated 404/550
-- over >= terminal_after_days — which a PERSISTENT CONFIG ERROR (a wrong
-- receiver_base_path makes EVERY file 404) also satisfies. Without a gate, such a
-- station would eventually go terminal across the board.
--
-- The gate (advisor: "station-health, not strictly same-run"): a slot may only be
-- promoted to terminal when the station is DEMONSTRABLY SERVING that session —
-- i.e. it has downloaded/archived >= 1 file for the SAME session within a recent
-- serving window. A blanket config error produces NO successful downloads, so the
-- station fails the gate and nothing goes terminal; a healthy station with a
-- genuine per-file gap keeps serving other days/hours, passes the gate, and the
-- gap is correctly ruled terminal.
--
-- KEY INVARIANT: serving_window_days (default 2) < terminal_after_days (default
-- 3). A config error that starts at day 0 stops all successful downloads; by the
-- time the terminal SPAN elapses (day 3), the last success is > 2 days old, so
-- the station fails the serving gate — a config error can NEVER reach terminal.
-- A healthy station always has a success inside the 2-day window, so its genuine
-- gaps still promote. The gate is per-(station, session), so a session-specific
-- config error cannot ride on another session's health.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/061_absence_served_gate.sql

BEGIN;

DROP FUNCTION IF EXISTS record_file_absence(
    VARCHAR, VARCHAR, DATE, SMALLINT, VARCHAR, INTEGER, INTEGER);

CREATE OR REPLACE FUNCTION record_file_absence(
    p_sid                 VARCHAR,
    p_session_type        VARCHAR,
    p_date                DATE,
    p_hour                SMALLINT DEFAULT NULL,
    p_source              VARCHAR  DEFAULT 'receiver',
    p_terminal_after_days INTEGER  DEFAULT 3,
    p_min_confirmations   INTEGER  DEFAULT 3,
    p_serving_window_days INTEGER  DEFAULT 2
) RETURNS VOID AS $$
DECLARE
    v_serving BOOLEAN;
BEGIN
    -- Station-health gate: has this station served THIS session recently? Only a
    -- serving station's confirmations may earn terminal (config-error immunity).
    -- Evaluated once; irrelevant on the INSERT path (first confirm is never
    -- terminal — span is 0).
    v_serving := (p_source <> 'receiver') OR EXISTS (
        SELECT 1 FROM file_tracking ft
        WHERE ft.sid = p_sid
          AND ft.session_type = p_session_type
          AND ft.status IN ('downloaded', 'archived')
          AND ft.last_checked > now() - (p_serving_window_days * INTERVAL '1 day')
    );

    UPDATE file_absence
       SET confirmations     = confirmations + 1,
           last_confirmed_at = now(),
           terminal = terminal OR (
               v_serving
               AND now() - first_confirmed_at
                   >= (p_terminal_after_days * INTERVAL '1 day')
               AND confirmations + 1 >= p_min_confirmations
           )
     WHERE source_location = p_source
       AND sid = p_sid
       AND session_type = p_session_type
       AND file_date = p_date
       AND file_hour IS NOT DISTINCT FROM p_hour;
    IF NOT FOUND THEN
        BEGIN
            INSERT INTO file_absence
                (source_location, sid, session_type, file_date, file_hour)
            VALUES (p_source, p_sid, p_session_type, p_date, p_hour);
        EXCEPTION WHEN unique_violation THEN
            UPDATE file_absence
               SET confirmations = confirmations + 1, last_confirmed_at = now()
             WHERE source_location = p_source AND sid = p_sid
               AND session_type = p_session_type AND file_date = p_date
               AND file_hour IS NOT DISTINCT FROM p_hour;
        END;
    END IF;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION record_file_absence IS
    'Log one reachable-but-absent confirmation. Promotes to terminal only when '
    '(a) confirmations span >= terminal_after_days, (b) >= min_confirmations, and '
    '(c) the station SERVED this session within serving_window_days (config-error '
    'immunity; invariant serving_window < terminal_after). Per-(station,session).';

INSERT INTO schema_migrations (migration_name)
VALUES ('061_absence_served_gate')
ON CONFLICT DO NOTHING;

COMMIT;
