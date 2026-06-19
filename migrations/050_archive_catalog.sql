-- Migration 050: archive_catalog — index of the IMO long-term GPS archive
--
-- Standalone index of every file in the long-term archive (ananas, via rawdata),
-- keyed by a compression-invariant content hash. Primary purpose is DATA
-- INTEGRITY (detect bit-rot / silent loss); it secondarily feeds dedup,
-- provenance (#34), and the dissemination layer. See design note
-- 1781867391-data-dissemination-archive-sync-design.
--
-- This is the first step of the Monday archive-sync MVP (vault todo #36).
-- The Monday sync computes content_sha256 on each local file at push time and
-- writes a row here ("forward-for-free" indexing). The 30+ years of history
-- rek_new never touched are backfilled gradually on a separate, throttled track.
--
-- Why a dedicated table (not bolted onto file_tracking):
--   file_tracking only knows rek_new-collected files and is operationally
--   TRUNCATE ... CASCADE-d. The archive catalog must outlive that churn and
--   span files rek_new never produced — so it is independent, and the optional
--   link back to file_tracking (file_tracking_id) is a SOFT reference with NO
--   foreign key: a real FK would either block the TRUNCATE or cascade-wipe this
--   table, both unacceptable for an integrity ledger.
--
-- The unifying primitive is content_sha256 over DECOMPRESSED content
-- (compression-invariant), so one column serves integrity, back-zip verify
-- (hash(.T00)==hash(.T00.gz) => safe delete), cross-format dedup, and #34's
-- canonical product hash. Hatanaka stays inside the hashed content, keeping this
-- hash in lockstep with utils.canonical_key (which folds compression only).

BEGIN;

CREATE TABLE IF NOT EXISTS archive_catalog (
    id                BIGSERIAL   PRIMARY KEY,
    -- where the file lives (one logical file per location). MVP: 'imo_archive'
    -- (ananas via rawdata). Future dissemination targets get their own value.
    storage_location  TEXT        NOT NULL DEFAULT 'imo_archive',
    -- 4-char station id (uppercase). NULL only for unparsable legacy files.
    station           VARCHAR(16),
    file_date         DATE,
    -- '15s_24hr' / '1Hz_1hr' / 'status_1hr' / etc. NULL for legacy files whose
    -- session cannot be inferred (see UNIQUE-grain caveat below).
    session_type      VARCHAR(32),
    -- 'raw' | 'rinex'. MVP indexes the raw tier only.
    file_category     VARCHAR(16) NOT NULL DEFAULT 'raw',
    -- compression-/case-invariant filename identity (utils.canonical_key).
    -- Stable across a back-zip: '.T00' -> '.T00.gz' keeps the same key.
    canonical_key     TEXT        NOT NULL,
    -- current physical path AT THE ARCHIVE (not the local rek-d01 path the hash
    -- was computed from). Updated in place when a file is back-zipped.
    file_path         TEXT        NOT NULL,
    -- on-disk compression suffix actually present: '.gz' / '.Z' / '' (none).
    compression       VARCHAR(8),
    -- on-disk (compressed) size in bytes.
    file_size         BIGINT,
    -- SHA-256 hex over DECOMPRESSED content (64 chars). NULL until computed.
    content_sha256    CHAR(64),
    -- #34 provenance flags. Nullable, forward-compatible; unset for the raw-tier MVP.
    is_rinexed        BOOLEAN,    -- a raw file has a corresponding RINEX product
    rinex_is_original BOOLEAN,    -- a RINEX product with no raw (frozen original)
    raw_available     BOOLEAN,    -- raw is present for this product
    -- SOFT link to file_tracking.id for the rek_new-overlap window. NO FK by
    -- design (file_tracking is TRUNCATE CASCADE-d); may dangle, that is fine.
    file_tracking_id  BIGINT,
    indexed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- last time the file was re-hashed against the archive copy. NULL = indexed
    -- (hash computed on the local file at push time) but not yet verified-on-archive.
    last_verified_at  TIMESTAMPTZ,

    -- Logical identity. Wider than (location, canonical_key) on purpose: two
    -- distinct files can share a basename across session dirs (e.g. a future
    -- 1Hz vs status raw with the same timestamp+letter). session_type+category
    -- in the key make an upsert safe against silently overwriting another file's
    -- row. Caveat: NULL session_type does NOT dedup under this UNIQUE — only an
    -- issue for the legacy-backfill track, where session is inferred; the forward
    -- (Monday) path always sets session_type.
    CONSTRAINT archive_catalog_logical_key
        UNIQUE (storage_location, session_type, file_category, canonical_key)
);

-- per-station date-range scans (integrity dashboards, freshness per station)
CREATE INDEX IF NOT EXISTS idx_archive_catalog_station_date
    ON archive_catalog (station, file_date);

-- dedup / integrity / local<->archive divergence lookups by content hash
CREATE INDEX IF NOT EXISTS idx_archive_catalog_sha256
    ON archive_catalog (content_sha256);

-- session-scoped freshness (is this session's latest archived date current?)
CREATE INDEX IF NOT EXISTS idx_archive_catalog_session_date
    ON archive_catalog (session_type, file_date);

INSERT INTO schema_migrations (migration_name)
VALUES ('050_archive_catalog')
ON CONFLICT DO NOTHING;

COMMIT;
