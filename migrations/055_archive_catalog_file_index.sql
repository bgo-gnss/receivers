-- Migration 055: archive_catalog — file_hour, compressed_sha256, EPOS md5 attrs
--
-- Part of the Unified File Index (docs/architecture/unified-file-index-plan.md,
-- milestone M1). Generalises archive_catalog from an IMO-archive-only integrity
-- ledger into the multi-server unified file index. ADDITIVE-ONLY DDL:
--
--   * file_hour SMALLINT       — the missing-hour a worklist could not name
--     before (plan §3.2 blocker). Populated from the path/filename on every
--     forward write going forward; the differential's hourly grain.
--   * compressed_sha256 CHAR(64) — SHA-256 over the ON-DISK (compressed) bytes,
--     the counterpart to content_sha256 (over DECOMPRESSED content). The valid
--     sha256↔md5 mapping is compressed_sha256 ↔ md5checksum (same bytes); it is
--     lazy-filled, never computed on the hot path.
--   * md5checksum / md5uncompressed CHAR(32) — stored ONLY on epos_portal rows,
--     the external EPOS/M3G contract (md5 is NOT derivable from any sha256, so
--     it is captured at push so the rinex_file export stays catalog-derived).
--
-- Plus a NON-UNIQUE logical index on the cross-location join grain
-- (station, session_type, file_category, file_date, file_hour). The existing
-- canonical_key UNIQUE is KEPT unchanged — swapping the UNIQUE on a live,
-- durable integrity ledger with legacy NULL-station/date rows is delicate and
-- unnecessary for M1 (plan §3.2 additive-first de-risk).
--
-- IMPORTANT — this migration MUTATES NO EXISTING ROWS. In particular it does
-- NOT back-populate file_date/file_hour on the current imo_archive rows: those
-- NULL file_dates keep verify.py's local↔archive cross-check inert. Activating
-- that cross-check (by populating imo_archive dates) is a separate, gated step
-- that must ship AFTER the verify.py fix (plan §8.4) — a later milestone.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/055_archive_catalog_file_index.sql

BEGIN;

ALTER TABLE archive_catalog
    -- hour-of-day for hourly products (0..23); NULL for daily (15s_24hr) and
    -- for legacy rows whose filename does not encode an hour.
    ADD COLUMN IF NOT EXISTS file_hour         SMALLINT,
    -- SHA-256 hex over the ON-DISK (compressed) bytes. NULL until lazy-filled.
    ADD COLUMN IF NOT EXISTS compressed_sha256 CHAR(64),
    -- EPOS external md5 contract, stored on epos_portal rows only. NULL elsewhere.
    ADD COLUMN IF NOT EXISTS md5checksum       CHAR(32),
    ADD COLUMN IF NOT EXISTS md5uncompressed   CHAR(32);

COMMENT ON COLUMN archive_catalog.file_hour IS
    'Hour-of-day (0..23) for hourly products; NULL for daily/legacy. The '
    'hourly grain the differential worklists need to name a missing hour.';
COMMENT ON COLUMN archive_catalog.compressed_sha256 IS
    'SHA-256 over ON-DISK (compressed) bytes; counterpart to content_sha256 '
    '(decompressed). Valid md5 map: compressed_sha256 ↔ md5checksum. Lazy-filled.';
COMMENT ON COLUMN archive_catalog.md5checksum IS
    'EPOS md5 over on-disk bytes — epos_portal rows only (external contract).';
COMMENT ON COLUMN archive_catalog.md5uncompressed IS
    'EPOS md5 over gzip+CRX2RNX-decompressed obs — epos_portal rows only.';

-- Cross-location join grain + hourly worklists (plan D7). NON-UNIQUE: the
-- canonical_key UNIQUE still guards against duplicate rows within a location.
CREATE INDEX IF NOT EXISTS idx_archive_catalog_logical
    ON archive_catalog (station, session_type, file_category, file_date, file_hour);

-- compressed-hash lookups (dedup / EPOS md5 cross-ref) when filled.
CREATE INDEX IF NOT EXISTS idx_archive_catalog_compressed_sha256
    ON archive_catalog (compressed_sha256)
    WHERE compressed_sha256 IS NOT NULL;

INSERT INTO schema_migrations (migration_name)
VALUES ('055_archive_catalog_file_index')
ON CONFLICT DO NOTHING;

COMMIT;
