-- Rollback 056: drop file_absence + record_file_absence, restore is_file_missing
-- to its mig-046 (file_tracking-only, 24 h TTL) form.

BEGIN;

-- Restore the mig-046 is_file_missing (no file_absence dependency).
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
              AND last_checked > NOW() - INTERVAL '24 hours'
        );
    ELSE
        RETURN EXISTS (
            SELECT 1 FROM file_tracking
            WHERE sid = p_sid
              AND session_type = p_session_type
              AND file_date = p_date
              AND file_hour = p_hour
              AND status = 'missing'
              AND last_checked > NOW() - INTERVAL '24 hours'
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

DROP FUNCTION IF EXISTS record_file_absence(
    VARCHAR, VARCHAR, DATE, SMALLINT, VARCHAR, INTEGER, INTEGER);
DROP TABLE IF EXISTS file_absence;

DELETE FROM schema_migrations WHERE migration_name = '056_file_absence';

COMMIT;
