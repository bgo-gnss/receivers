-- Migration 046: shorten is_file_missing TTL from 7 days to 24 hours
--
-- Problem: When a download attempt hits FTP 550 / HTTP 404 (file not on
-- receiver), the file_tracker marks the row status='missing'. The
-- is_file_missing() PG function then suppresses retries for that
-- (sid, session, date) tuple for **7 days**.
--
-- The 7-day window is too aggressive for two reasons:
--
-- 1. Receivers sometimes serve a previously-missing file later in the day.
--    Example: HVSK on 2026-05-07 returned 404 at 09:50 morning; at 12:49
--    it was still flagged "known missing" so retry was skipped — even
--    though by then the receiver may well have published the file.
--
-- 2. Operator-driven manual downloads are also blocked by the same
--    short-circuit. After a single 404, manual `receivers download`
--    silently skips for a week.
--
-- Reduce the TTL to 24 h. After 24 h:
--   * If the file genuinely never existed, the next attempt re-confirms
--     and re-marks status='missing' (one extra round trip per day).
--   * If the receiver caught up, we get the file.
--
-- Schema unchanged — only the body of is_file_missing() changes.

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

COMMENT ON FUNCTION is_file_missing IS
    'Check if file is known to be missing (skip download). 24 h TTL: '
    'after that, re-attempt the download in case the receiver caught up '
    'or the previous 404/550 was transient.';

COMMIT;
