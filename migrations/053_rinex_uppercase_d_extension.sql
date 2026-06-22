-- Migration 053: RINEX archive naming — lowercase .d.Z -> uppercase .D.Z
--
-- At the old-rek -> rek_new cutover (DOY 172, 2026) the archived RINEX files
-- switched from the historical UPPERCASE Hatanaka type letter (.D.Z, produced by
-- the legacy teqc/sbf2rin pipeline) to lowercase .d.Z (rnx2crx's own default,
-- carried straight through by rek_new's converter). okada's getimorinex.py
-- requests the uppercase name, found no file once old rek stopped, and left a
-- 0-byte download -> "gzip: unexpected end of file" in the GAMIT feed.
--
-- The fix keeps the whole archive on the established .D.Z convention:
--   * converter_base._apply_hatanaka_compression now renames rnx2crx's .d -> .D
--     (this is what actually names the file on disk; archive-sync rsyncs the
--      source name verbatim, it does NOT re-derive from the template below).
--   * archive_format.filename_template / file_extension are updated to match so
--     FormatResolver.build_path() lookups stay consistent.
--
-- The seed INSERTs (migrations 000/021) use ON CONFLICT DO NOTHING, so existing
-- rows are NOT updated by re-running them — hence this explicit UPDATE. Generic
-- over file_category='rinex' so any receiver-specific rinex format is covered.
--
-- Content is unchanged (still RINEX 3.04); only the type-letter case differs.
-- canonical_key() is case-insensitive, so the archive_catalog / file_tracking
-- content-hash dedup is unaffected by the rename.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/053_rinex_uppercase_d_extension.sql

BEGIN;

UPDATE archive_format
SET file_extension    = '.D.Z',
    filename_template = REPLACE(filename_template, '#Rin2d.Z', '#Rin2D.Z')
WHERE file_category = 'rinex'
  AND filename_template LIKE '%#Rin2d.Z';

INSERT INTO schema_migrations (migration_name)
VALUES ('053_rinex_uppercase_d_extension')
ON CONFLICT DO NOTHING;

COMMIT;
