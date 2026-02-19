-- Migration: 025_file_integrity.sql
-- Description: Post-download validation and periodic integrity checking
-- Date: 2026-02-18
--
-- Adds:
--   1. remote_file_size column to track expected file size from receiver
--   2. integrity_checked_at column for periodic integrity verification
--   3. Updated upsert_file_tracking() with 12th parameter (backward compatible)
--   4. Partial indexes for integrity checking queries
--   5. 'suspect' status value for files that fail integrity checks
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/025_file_integrity.sql

BEGIN;

-- ============================================================================
-- NEW COLUMNS
-- ============================================================================

ALTER TABLE file_tracking ADD COLUMN IF NOT EXISTS remote_file_size BIGINT;
ALTER TABLE file_tracking ADD COLUMN IF NOT EXISTS integrity_checked_at TIMESTAMPTZ;

COMMENT ON COLUMN file_tracking.remote_file_size IS 'File size reported by receiver (FTP SIZE / HTTP Content-Length)';
COMMENT ON COLUMN file_tracking.integrity_checked_at IS 'Last time integrity was verified by periodic checker';

-- ============================================================================
-- PARTIAL INDEXES
-- ============================================================================

-- Files needing integrity check (downloaded/archived but never verified)
CREATE INDEX IF NOT EXISTS idx_file_tracking_needs_integrity
    ON file_tracking (sid, session_type, file_date)
    WHERE status IN ('downloaded', 'archived') AND integrity_checked_at IS NULL;

-- Suspect files (failed integrity check)
CREATE INDEX IF NOT EXISTS idx_file_tracking_suspect
    ON file_tracking (sid, session_type)
    WHERE status = 'suspect';

-- ============================================================================
-- UPDATED UPSERT FUNCTION (12th parameter: p_remote_file_size)
-- ============================================================================

CREATE OR REPLACE FUNCTION upsert_file_tracking(
    p_sid VARCHAR(4),
    p_session_type VARCHAR(20),
    p_date DATE,
    p_hour SMALLINT,
    p_filename VARCHAR(100),
    p_status VARCHAR(20),
    p_file_size BIGINT DEFAULT NULL,
    p_samples INTEGER DEFAULT NULL,
    p_checksum VARCHAR(64) DEFAULT NULL,
    p_json_path VARCHAR(255) DEFAULT NULL,
    p_error TEXT DEFAULT NULL,
    p_remote_file_size BIGINT DEFAULT NULL
) RETURNS INTEGER AS $$
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
        )
        RETURNING id INTO v_id;
    END IF;

    RETURN v_id;
END;
$$ LANGUAGE plpgsql;

-- Drop the old 11-parameter overload to avoid ambiguity.
-- The new 12-param version is backward compatible (p_remote_file_size DEFAULT NULL).
DROP FUNCTION IF EXISTS upsert_file_tracking(VARCHAR(4), VARCHAR(20), DATE, SMALLINT, VARCHAR(100), VARCHAR(20), BIGINT, INTEGER, VARCHAR(64), VARCHAR(255), TEXT);

COMMENT ON FUNCTION upsert_file_tracking(VARCHAR(4), VARCHAR(20), DATE, SMALLINT, VARCHAR(100), VARCHAR(20), BIGINT, INTEGER, VARCHAR(64), VARCHAR(255), TEXT, BIGINT) IS 'Insert or update file tracking record (v2: with remote_file_size)';

COMMIT;
