-- Rollback 059: restore the mig-056 is_file_missing (terminal always honoured,
-- 4-arg signature). Drops the 5-arg advisory overload first.

BEGIN;

DROP FUNCTION IF EXISTS is_file_missing(VARCHAR, VARCHAR, DATE, SMALLINT, BOOLEAN);

CREATE OR REPLACE FUNCTION is_file_missing(
    p_sid VARCHAR,
    p_session_type VARCHAR,
    p_date DATE,
    p_hour SMALLINT DEFAULT NULL
) RETURNS BOOLEAN AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM file_absence
        WHERE source_location = 'receiver'
          AND sid = p_sid
          AND session_type = p_session_type
          AND file_date = p_date
          AND file_hour IS NOT DISTINCT FROM p_hour
          AND terminal
    ) THEN
        RETURN TRUE;
    END IF;
    RETURN EXISTS (
        SELECT 1 FROM file_tracking
        WHERE sid = p_sid
          AND session_type = p_session_type
          AND file_date = p_date
          AND file_hour IS NOT DISTINCT FROM p_hour
          AND status = 'missing'
          AND last_checked > NOW() - INTERVAL '24 hours'
    );
END;
$$ LANGUAGE plpgsql;

DELETE FROM schema_migrations WHERE migration_name = '059_is_file_missing_advisory_terminal';

COMMIT;
