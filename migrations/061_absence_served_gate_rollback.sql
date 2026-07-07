-- Rollback 061: restore the mig-056 record_file_absence (no served-gate).

BEGIN;

DROP FUNCTION IF EXISTS record_file_absence(
    VARCHAR, VARCHAR, DATE, SMALLINT, VARCHAR, INTEGER, INTEGER, INTEGER);

CREATE OR REPLACE FUNCTION record_file_absence(
    p_sid                 VARCHAR,
    p_session_type        VARCHAR,
    p_date                DATE,
    p_hour                SMALLINT DEFAULT NULL,
    p_source              VARCHAR  DEFAULT 'receiver',
    p_terminal_after_days INTEGER  DEFAULT 3,
    p_min_confirmations   INTEGER  DEFAULT 3
) RETURNS VOID AS $$
BEGIN
    UPDATE file_absence
       SET confirmations     = confirmations + 1,
           last_confirmed_at  = now(),
           terminal = terminal OR (
               now() - first_confirmed_at
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

DELETE FROM schema_migrations WHERE migration_name = '061_absence_served_gate';

COMMIT;
