-- Rollback 076 — drop truncation_repair and the migration record.
--
-- Loses the record of which archive files are SALVAGED (partial). Export it
-- first:  \copy (SELECT * FROM truncation_repair) TO 'truncation_repair.csv' CSV HEADER

BEGIN;
DROP TABLE IF EXISTS truncation_repair;
DELETE FROM schema_migrations WHERE migration_name = '076_truncation_repair';
COMMIT;
