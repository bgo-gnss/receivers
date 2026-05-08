-- Rollback for migration 046 — restore the original 7-day TTL.

BEGIN;

CREATE OR REPLACE FUNCTION is_file_missing(
    p_sid VARCHAR,
    p_session_type VARCHAR,
    p_date DATE,
    p_hour SMALLINT DEFAULT NULL
) RETURNS BOOLEAN AS $$
BEGIN
    IF p_hour IS NULL THEN
        RETURN EXISTS (
            SELECT 1 FROM file_tracking
            WHERE sid = p_sid
              AND session_type = p_session_type
              AND file_date = p_date
              AND file_hour IS NULL
              AND status = 'missing'
              AND last_checked > NOW() - INTERVAL '7 days'
        );
    ELSE
        RETURN EXISTS (
            SELECT 1 FROM file_tracking
            WHERE sid = p_sid
              AND session_type = p_session_type
              AND file_date = p_date
              AND file_hour = p_hour
              AND status = 'missing'
              AND last_checked > NOW() - INTERVAL '7 days'
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION is_file_missing IS
    'Check if file is known to be missing (skip download)';

COMMIT;
