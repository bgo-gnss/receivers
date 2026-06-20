-- Migration 052: file_tracking.content_sha256 — local-side content hash
--
-- The local archive index (file_tracking) is the rolling, retention-bounded
-- record of what rek_new holds. Until now it carried NO content hash of the
-- raw/RINEX file itself: `import_checksum` is the *health-import* digest (a
-- 16-char hash of extracted sample JSON), unrelated to file bytes.
--
-- This column stores the compression-invariant content_sha256 (SHA-256 over the
-- DECOMPRESSED content, receivers.utils.content_hash) of the local file. It is
-- the local counterpart to archive_catalog.content_sha256: a local↔archive
-- integrity check is now a hash COMPARISON between the two, and local bit-rot
-- becomes detectable independent of a push.
--
-- RAW tier first (immutable: hash is write-once, never changes). RINEX is
-- mutable (a header fix regenerates the file → new content → the hash is
-- RE-COMPUTED and updated on edit; the raw/original stays frozen with its
-- original hash).
--
-- Nullable: NULL = not yet hashed (existing rows backfill lazily / via the
-- integrity checker). Hatanaka content is hashed INSIDE the decompressed bytes,
-- keeping this in lockstep with utils.canonical_key (compression-folded only).
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/052_file_tracking_content_hash.sql

BEGIN;

ALTER TABLE file_tracking
    ADD COLUMN IF NOT EXISTS content_sha256 CHAR(64),
    -- when content_sha256 was last (re)computed on the local file; lets the
    -- integrity checker re-hash oldest-first and detect local bit-rot over time.
    ADD COLUMN IF NOT EXISTS content_hashed_at TIMESTAMPTZ;

COMMENT ON COLUMN file_tracking.content_sha256 IS
    'SHA-256 over DECOMPRESSED file content (compression-invariant); local '
    'counterpart to archive_catalog.content_sha256. NULL until computed.';

-- local↔archive divergence / dedup lookups by content hash
CREATE INDEX IF NOT EXISTS idx_file_tracking_content_sha256
    ON file_tracking (content_sha256)
    WHERE content_sha256 IS NOT NULL;

-- drive lazy backfill / periodic re-hash: present files not yet hashed, or
-- hashed longest ago, oldest first.
CREATE INDEX IF NOT EXISTS idx_file_tracking_needs_hash
    ON file_tracking (content_hashed_at NULLS FIRST)
    WHERE status IN ('downloaded', 'archived');

INSERT INTO schema_migrations (migration_name)
VALUES ('052_file_tracking_content_hash')
ON CONFLICT DO NOTHING;

COMMIT;
