-- Companion to migrate_rinex_d_to_D.sh: realign the catalog + local index from
-- .d.Z to .D.Z AFTER the on-disk files have been renamed.
--
-- canonical_key is case-insensitive (utils.canonical_key lowercases), so the
-- catalog KEY is unchanged by the rename — only the stored path/filename strings
-- need the s/d.Z/D.Z/ rewrite, or verify/integrity will look for the old names.
--
-- Scoped to session_type='15s_24hr' to match the daily-path migration. Widen to
-- '1Hz_1hr' only once that tier's files are renamed on disk too (keep DB and
-- disk in lockstep — never update a session here that you haven't renamed yet).
--
-- gps_health dual-writes to the pgdev mirror, so run this on BOTH:
--   psql -h localhost          -d gps_health -f migrate_rinex_d_to_D.sql   # rek-d01
--   psql -h pgdev.vedur.is     -d gps_health -f migrate_rinex_d_to_D.sql   # mirror (as bgo)

BEGIN;

-- archive_catalog: file_path holds the rawdata-side path, e.g.
-- ~/gpsdata/2026/jun/THOB/15s_24hr/rinex/THOB1720.26d.Z
UPDATE archive_catalog
SET file_path = regexp_replace(file_path, 'd\.Z$', 'D.Z')
WHERE file_category = 'rinex'
  AND session_type  = '15s_24hr'
  AND file_path ~ '[0-9][0-9]d\.Z$';

-- file_tracking: local rolling index; filename only (no directory)
UPDATE file_tracking
SET filename = regexp_replace(filename, 'd\.Z$', 'D.Z')
WHERE session_type = '15s_24hr'
  AND filename ~ '[0-9][0-9]d\.Z$';

COMMIT;
