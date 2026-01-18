-- Migration: 002_file_tracking.sql
-- Description: Track file availability and import status
-- Date: 2026-01-18
--
-- This table serves multiple purposes:
--   1. Track which files exist/are missing on receivers (avoid retrying)
--   2. Track which files have been downloaded
--   3. Track which files have been imported to the database (avoid reimporting)
--   4. Provide data availability statistics
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/002_file_tracking.sql

BEGIN;

-- ============================================================================
-- FILE TRACKING TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS file_tracking (
    id SERIAL PRIMARY KEY,

    -- Identification
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    session_type VARCHAR(20) NOT NULL,      -- '15s_24hr', '1Hz_1hr', 'status_1hr'
    file_date DATE NOT NULL,
    file_hour SMALLINT,                     -- NULL for daily files, 0-23 for hourly

    -- File information
    filename VARCHAR(100),                  -- Expected filename
    file_size BIGINT,                       -- File size in bytes (if known)

    -- Availability status
    status VARCHAR(20) NOT NULL DEFAULT 'unknown',
    -- Possible values:
    --   'unknown'    - Not yet checked
    --   'available'  - File exists on receiver
    --   'missing'    - File confirmed missing on receiver
    --   'downloaded' - File successfully downloaded
    --   'archived'   - File archived locally
    --   'error'      - Download/processing error

    -- Tracking timestamps
    first_checked TIMESTAMPTZ,              -- When we first checked for this file
    last_checked TIMESTAMPTZ,               -- Most recent check
    last_attempt TIMESTAMPTZ,               -- Most recent download attempt
    download_count INTEGER DEFAULT 0,       -- Number of download attempts

    -- Import tracking (for health extraction)
    imported_to_db BOOLEAN DEFAULT FALSE,
    imported_at TIMESTAMPTZ,
    samples_imported INTEGER,               -- Number of samples imported
    import_checksum VARCHAR(64),            -- Hash of imported data (to detect changes)

    -- JSON tracking
    json_written BOOLEAN DEFAULT FALSE,
    json_path VARCHAR(255),
    json_written_at TIMESTAMPTZ,

    -- Error tracking
    last_error TEXT,
    error_count INTEGER DEFAULT 0,

    -- Metadata
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Unique constraint: station + session + date + hour
-- Use partial indexes for NULL handling
CREATE UNIQUE INDEX idx_file_tracking_hourly
    ON file_tracking(sid, session_type, file_date, file_hour)
    WHERE file_hour IS NOT NULL;

CREATE UNIQUE INDEX idx_file_tracking_daily
    ON file_tracking(sid, session_type, file_date)
    WHERE file_hour IS NULL;

COMMENT ON TABLE file_tracking IS 'Track file availability and import status for downloads and health extraction';

-- ============================================================================
-- INDEXES
-- ============================================================================

-- Find files by status (e.g., all missing files)
CREATE INDEX idx_file_tracking_status ON file_tracking(status);

-- Find files needing import
CREATE INDEX idx_file_tracking_not_imported
    ON file_tracking(sid, session_type, file_date)
    WHERE NOT imported_to_db;

-- Find missing files to skip
CREATE INDEX idx_file_tracking_missing
    ON file_tracking(sid, session_type, file_date)
    WHERE status = 'missing';

-- Recent activity
CREATE INDEX idx_file_tracking_updated ON file_tracking(updated_at DESC);

-- ============================================================================
-- HELPER FUNCTIONS
-- ============================================================================

-- Check if a file is known to be missing (should skip download)
CREATE OR REPLACE FUNCTION is_file_missing(
    p_sid VARCHAR(4),
    p_session_type VARCHAR(20),
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

COMMENT ON FUNCTION is_file_missing IS 'Check if file is known to be missing (skip download)';

-- Check if health data is already imported
CREATE OR REPLACE FUNCTION is_health_imported(
    p_sid VARCHAR(4),
    p_date DATE,
    p_checksum VARCHAR(64) DEFAULT NULL
) RETURNS BOOLEAN AS $$
BEGIN
    IF p_checksum IS NOT NULL THEN
        RETURN EXISTS (
            SELECT 1 FROM file_tracking
            WHERE sid = p_sid
              AND session_type = 'status_1hr'
              AND file_date = p_date
              AND file_hour IS NULL
              AND imported_to_db = TRUE
              AND import_checksum = p_checksum
        );
    END IF;

    RETURN EXISTS (
        SELECT 1 FROM file_tracking
        WHERE sid = p_sid
          AND session_type = 'status_1hr'
          AND file_date = p_date
          AND file_hour IS NULL
          AND imported_to_db = TRUE
    );
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION is_health_imported IS 'Check if health data for date is already imported';

-- Upsert file tracking record
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
    p_error TEXT DEFAULT NULL
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
            first_checked, last_checked, last_attempt, download_count,
            imported_to_db, imported_at, samples_imported, import_checksum,
            json_written, json_path, json_written_at, last_error, error_count
        ) VALUES (
            p_sid, p_session_type, p_date, p_hour, p_filename, p_status, p_file_size,
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

COMMENT ON FUNCTION upsert_file_tracking IS 'Insert or update file tracking record';

-- ============================================================================
-- DATA AVAILABILITY VIEW
-- ============================================================================

CREATE OR REPLACE VIEW data_availability AS
SELECT
    sid,
    session_type,
    file_date,
    status,
    imported_to_db,
    samples_imported,
    CASE
        WHEN status = 'missing' THEN 0
        WHEN status = 'downloaded' AND samples_imported IS NOT NULL THEN
            ROUND(samples_imported::numeric /
                CASE session_type
                    WHEN 'status_1hr' THEN 1440
                    WHEN '1Hz_1hr' THEN 86400
                    ELSE 1440
                END * 100, 1)
        ELSE NULL
    END AS completeness_pct,
    last_checked,
    error_count
FROM file_tracking
WHERE file_hour IS NULL  -- Daily summary only
ORDER BY sid, session_type, file_date DESC;

COMMENT ON VIEW data_availability IS 'Summary view of data availability per station/date';

COMMIT;
