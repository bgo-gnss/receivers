-- Migration: 021_archive_format.sql
-- Description: Archive format definitions, storage locations, and file location tracking
-- Date: 2026-02-11
--
-- Adds table-driven RINEX metadata and path templates so that:
--   1. RINEX version, naming convention, Hatanaka, compression are tracked per-file
--   2. Format definitions with dir/filename templates enable gtimes.datepathlist() path construction
--   3. Files can be tracked across multiple storage locations (local archive, cold storage, NFS)
--   4. New file formats can be added without code changes
--
-- Backward compatible: existing file_tracking data unchanged; format_id is nullable FK.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/021_archive_format.sql

BEGIN;

-- ============================================================================
-- ARCHIVE FORMAT DEFINITIONS
-- ============================================================================

CREATE TABLE IF NOT EXISTS archive_format (
    format_id VARCHAR(40) PRIMARY KEY,

    -- Classification
    session_type VARCHAR(20) NOT NULL,      -- '15s_24hr', '1Hz_1hr', 'status_1hr'
    file_category VARCHAR(20) NOT NULL,     -- 'raw', 'rinex', 'nav', 'timeseries'
    receiver_type VARCHAR(20),              -- 'polarx5', 'netr9', NULL for universal

    -- Time resolution (gtimes frequency code)
    frequency VARCHAR(4) NOT NULL,          -- '1D' (daily), '1H' (hourly)

    -- RINEX metadata (NULL for non-RINEX files)
    rinex_version VARCHAR(8),               -- NULL for raw, '3.04' for RINEX 3
    naming_convention VARCHAR(10),          -- NULL, 'short', 'long'
    hatanaka BOOLEAN,                       -- NULL for non-RINEX, true/false
    compression VARCHAR(4),                 -- 'Z', 'gz', NULL (uncompressed)

    -- File identification
    file_extension VARCHAR(20) NOT NULL,    -- '.sbf.gz', '.d.Z', '.rnx.gz'

    -- Path templates (for use with gtimes.datepathlist())
    -- {station} and {session_letter} are replaced before passing to gtimes
    -- %Y, %m, %d, %H, #b, #Rin2, etc. are handled by gtimes
    dir_template TEXT NOT NULL,             -- '%Y/#b/{station}/15s_24hr/raw/'
    filename_template TEXT NOT NULL,        -- '{station}%Y%m%d%H00{session_letter}.sbf.gz'

    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE archive_format IS 'Format definitions for archive files — drives path construction and RINEX metadata';
COMMENT ON COLUMN archive_format.format_id IS 'Human-readable key, e.g. polarx5_15s_24hr_raw';
COMMENT ON COLUMN archive_format.dir_template IS 'Directory template with {station}, {session_letter} placeholders + gtimes date codes';
COMMENT ON COLUMN archive_format.filename_template IS 'Filename template with same placeholders; combined with dir_template for full path';
COMMENT ON COLUMN archive_format.receiver_type IS 'Receiver type (polarx5, netr9, etc.) or NULL for universal formats';

-- ============================================================================
-- STORAGE LOCATIONS
-- ============================================================================

CREATE TABLE IF NOT EXISTS storage_location (
    location_id VARCHAR(30) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    base_path TEXT NOT NULL,                -- '/home/bgo/tmp/gpsdata' or '/mnt_data/gpsdata'
    location_type VARCHAR(10) NOT NULL      -- 'local', 'nfs', 'server' (rsync target)
        CHECK (location_type IN ('local', 'nfs', 'server')),
    is_primary BOOLEAN DEFAULT false,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE storage_location IS 'Storage locations for archive files — environment-specific base paths';
COMMENT ON COLUMN storage_location.location_type IS 'local=direct filesystem, nfs=mounted network share, server=rsync target';

-- ============================================================================
-- FILE-TO-LOCATION TRACKING (many-to-many)
-- ============================================================================

CREATE TABLE IF NOT EXISTS file_locations (
    file_tracking_id INTEGER NOT NULL
        REFERENCES file_tracking(id) ON DELETE CASCADE,
    location_id VARCHAR(30) NOT NULL
        REFERENCES storage_location(location_id) ON DELETE CASCADE,
    stored_at TIMESTAMPTZ DEFAULT NOW(),
    verified_at TIMESTAMPTZ,                -- Last time file existence was verified
    file_path TEXT,                          -- Full path at this location (cached)
    file_size BIGINT,                        -- Size at this location (for integrity)
    PRIMARY KEY (file_tracking_id, location_id)
);

COMMENT ON TABLE file_locations IS 'Tracks which files exist at which storage locations';

CREATE INDEX idx_file_locations_location
    ON file_locations(location_id);

CREATE INDEX idx_file_locations_verified
    ON file_locations(verified_at)
    WHERE verified_at IS NOT NULL;

-- ============================================================================
-- EXTEND file_tracking WITH format_id (nullable FK for backward compatibility)
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'file_tracking' AND column_name = 'format_id'
    ) THEN
        ALTER TABLE file_tracking
            ADD COLUMN format_id VARCHAR(40)
            REFERENCES archive_format(format_id);
    END IF;
END $$;

COMMENT ON COLUMN file_tracking.format_id IS 'Optional link to archive_format for RINEX metadata and path templates';

CREATE INDEX idx_file_tracking_format
    ON file_tracking(format_id)
    WHERE format_id IS NOT NULL;

-- ============================================================================
-- SEED DATA: PolaRX5 format definitions
-- ============================================================================

-- Raw SBF files
INSERT INTO archive_format (format_id, session_type, file_category, receiver_type, frequency,
    rinex_version, naming_convention, hatanaka, compression, file_extension,
    dir_template, filename_template, description)
VALUES
    ('polarx5_15s_24hr_raw', '15s_24hr', 'raw', 'polarx5', '1D',
     NULL, NULL, NULL, 'gz', '.sbf.gz',
     '%Y/#b/{station}/15s_24hr/raw/',
     '{station}%Y%m%d%H00{session_letter}.sbf.gz',
     'PolaRX5 daily 15s SBF raw file'),

    ('polarx5_1hz_1hr_raw', '1Hz_1hr', 'raw', 'polarx5', '1H',
     NULL, NULL, NULL, 'gz', '.sbf.gz',
     '%Y/#b/{station}/1Hz_1hr/raw/',
     '{station}%Y%m%d%H00{session_letter}.sbf.gz',
     'PolaRX5 hourly 1Hz SBF raw file'),

    ('polarx5_status_1hr_raw', 'status_1hr', 'raw', 'polarx5', '1H',
     NULL, NULL, NULL, 'gz', '.sbf.gz',
     '%Y/#b/{station}/status_1hr/raw/',
     '{station}%Y%m%d%H00{session_letter}.sbf.gz',
     'PolaRX5 hourly status SBF raw file'),

-- RINEX observation files (Hatanaka compressed, RINEX 3.04, short naming)
    ('polarx5_15s_24hr_rinex', '15s_24hr', 'rinex', 'polarx5', '1D',
     '3.04', 'short', true, 'Z', '.D.Z',
     '%Y/#b/{station}/15s_24hr/rinex/',
     '{station}#Rin2D.Z',
     'PolaRX5 daily 15s RINEX 3 Hatanaka compressed (short naming)'),

    ('polarx5_1hz_1hr_rinex', '1Hz_1hr', 'rinex', 'polarx5', '1H',
     '3.04', 'short', true, 'Z', '.D.Z',
     '%Y/#b/{station}/1Hz_1hr/rinex/',
     '{station}#Rin2D.Z',
     'PolaRX5 hourly 1Hz RINEX 3 Hatanaka compressed (short naming)')
ON CONFLICT (format_id) DO NOTHING;

-- ============================================================================
-- HELPER: Look up format for a file_tracking record
-- ============================================================================

CREATE OR REPLACE FUNCTION get_archive_format(p_format_id VARCHAR(40))
RETURNS TABLE (
    format_id VARCHAR(40),
    session_type VARCHAR(20),
    file_category VARCHAR(20),
    receiver_type VARCHAR(20),
    frequency VARCHAR(4),
    rinex_version VARCHAR(8),
    naming_convention VARCHAR(10),
    hatanaka BOOLEAN,
    compression VARCHAR(4),
    file_extension VARCHAR(20),
    dir_template TEXT,
    filename_template TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT af.format_id, af.session_type, af.file_category, af.receiver_type,
           af.frequency, af.rinex_version, af.naming_convention, af.hatanaka,
           af.compression, af.file_extension, af.dir_template, af.filename_template
    FROM archive_format af
    WHERE af.format_id = p_format_id;
END;
$$ LANGUAGE plpgsql STABLE;

COMMENT ON FUNCTION get_archive_format IS 'Look up archive format definition by format_id';

-- ============================================================================
-- VIEW: File tracking with format metadata
-- ============================================================================

CREATE OR REPLACE VIEW file_tracking_with_format AS
SELECT
    ft.id,
    ft.sid,
    ft.session_type,
    ft.file_date,
    ft.file_hour,
    ft.filename,
    ft.file_size,
    ft.status,
    ft.format_id,
    af.file_category,
    af.rinex_version,
    af.naming_convention,
    af.hatanaka,
    af.compression AS format_compression,
    af.file_extension,
    af.dir_template,
    af.filename_template,
    ft.last_checked,
    ft.updated_at
FROM file_tracking ft
LEFT JOIN archive_format af ON ft.format_id = af.format_id;

COMMENT ON VIEW file_tracking_with_format IS 'File tracking records joined with format metadata';

COMMIT;
