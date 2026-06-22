-- Rollback for migration 053: revert RINEX archive naming to lowercase .d.Z
--
-- NOTE: this only reverts the archive_format catalog rows. It does NOT undo the
-- converter_base change (which renames rnx2crx .d -> .D on disk) nor rename any
-- files already written as .D.Z on rawdata. A full revert also needs the code
-- change reverted and the on-disk files renamed back.

BEGIN;

UPDATE archive_format
SET file_extension    = '.d.Z',
    filename_template = REPLACE(filename_template, '#Rin2D.Z', '#Rin2d.Z')
WHERE file_category = 'rinex'
  AND filename_template LIKE '%#Rin2D.Z';

DELETE FROM schema_migrations WHERE migration_name = '053_rinex_uppercase_d_extension';

COMMIT;
