-- Migration 054: storage_location → file-server registry + retention model
--
-- Part of the Unified File Index (docs/architecture/unified-file-index-plan.md,
-- milestone M1). Turns the thin storage_location table (a bare name/base_path
-- lookup for the archive_format layer) into a proper multi-server registry that
-- the unified catalog can key off — one row per file server the receivers stack
-- touches: the local ring (raw + rinex), the IMO long-term archive, the EPOS
-- portal, and the receiver's own internal buffer (a logical upstream).
--
-- Additive ONLY: existing columns and rows are untouched; new columns are
-- nullable / defaulted so the archive_format code that already reads this table
-- keeps working. The location_type CHECK is DROPPED (not narrowed) because the
-- registry now needs values the old CHECK forbids ('logical' for the receiver
-- buffer, 'remote' for the portal) — the richer `protocol` column carries the
-- real transport semantics; location_type stays as a coarse legacy hint.
--
-- Two child tables:
--   * storage_retention   — per (location, session) ring-buffer floor. This is a
--     DERIVED projection of scheduler.yaml [local_prune] (the single source of
--     truth for retention): the seeder writes it, it is never hand-edited. It
--     lets the differential (M2) reason about "how far back should this location
--     still hold data" without re-parsing yaml.
--   * receiver_buffer_depth — per (receiver_type, session) how many days the
--     receiver itself keeps on its internal store. The M2 missing_on_receiver
--     floor: below this depth a file has aged off the receiver and must be
--     re-pulled from the archive, never re-fetched from the receiver. Seeded
--     with conservative defaults (see notes column) — refine per receiver disk.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/054_storage_location_registry.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- storage_location: enrich into a real file-server registry
-- ---------------------------------------------------------------------------

-- The old inline CHECK (location_type IN ('local','nfs','server')) rejects the
-- new logical/remote rows. Drop it; `protocol` is the authoritative typing now.
ALTER TABLE storage_location
    DROP CONSTRAINT IF EXISTS storage_location_location_type_check;

ALTER TABLE storage_location
    -- transport to reach this location; the authoritative type column now.
    ADD COLUMN IF NOT EXISTS protocol     VARCHAR(16),  -- local|nfs-ro|ssh|rsync|ftp|https|logical
    -- hostname (NULL for a local filesystem / logical location).
    ADD COLUMN IF NOT EXISTS host         TEXT,
    -- path root AT THE LOCATION's host (e.g. '~/gpsdata' on the archive gateway).
    -- For a local location this equals base_path; kept separate so a remote root
    -- (distinct from any local mount base_path) can be recorded.
    ADD COLUMN IF NOT EXISTS root_path    TEXT,
    -- a permanent location is never pruned and can be treated as a safe fallback
    -- (a file present here must never be re-fetched from the receiver).
    ADD COLUMN IF NOT EXISTS is_permanent BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN storage_location.protocol IS
    'Transport to reach this location: local|nfs-ro|ssh|rsync|ftp|https|logical. '
    'Authoritative type; location_type is a coarse legacy hint.';
COMMENT ON COLUMN storage_location.is_permanent IS
    'Never pruned; a file present here is a safe fallback (do not re-fetch from receiver).';

-- ---------------------------------------------------------------------------
-- storage_retention: per (location, session) ring floor — DERIVED from
-- scheduler.yaml [local_prune]. Seeded by db.seeder, never hand-edited.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS storage_retention (
    location_id  VARCHAR(30) NOT NULL
        REFERENCES storage_location(location_id) ON DELETE CASCADE,
    session_type VARCHAR(32) NOT NULL,
    -- normal ring floor in days; NULL = no prune (permanent).
    retention_days           INTEGER,
    -- applied instead while free space is below min_free_gb (shorter floor).
    emergency_retention_days INTEGER,
    -- provenance: where this row was derived from (default: the yaml key).
    source       TEXT NOT NULL DEFAULT 'scheduler.yaml:local_prune',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (location_id, session_type)
);

COMMENT ON TABLE storage_retention IS
    'Per (location, session) ring-buffer retention, DERIVED from scheduler.yaml '
    '[local_prune] by db.seeder. Single source of truth is the yaml; this is a '
    'query-friendly projection, never hand-edited.';

-- ---------------------------------------------------------------------------
-- receiver_buffer_depth: per (receiver_type, session) internal-store depth.
-- The M2 missing_on_receiver floor. Conservative seeds — validate per model.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS receiver_buffer_depth (
    receiver_type VARCHAR(32) NOT NULL,
    session_type  VARCHAR(32) NOT NULL,
    depth_days    INTEGER NOT NULL,
    notes         TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (receiver_type, session_type)
);

COMMENT ON TABLE receiver_buffer_depth IS
    'How many days each receiver type keeps on its internal store, per session. '
    'The missing_on_receiver floor (M2): below this depth a file has aged off '
    'the receiver → re-pull from archive, never re-fetch from receiver. '
    'Seeds are CONSERVATIVE defaults — refine against real receiver disk sizes.';

-- Conservative defaults. Deliberately UNDER-estimates so the floor errs toward
-- routing to the archive rather than churning a 404 against a receiver that has
-- already discarded the file. Refine when real per-model retention is measured.
INSERT INTO receiver_buffer_depth (receiver_type, session_type, depth_days, notes) VALUES
    ('polarx5', '1Hz_1hr',    7,  'conservative default — PolaRx5 internal disk, validate'),
    ('polarx5', '15s_24hr',   30, 'conservative default — PolaRx5 internal disk, validate'),
    ('polarx5', 'status_1hr', 7,  'conservative default — PolaRx5 internal disk, validate'),
    ('netr9',   '15s_24hr',   14, 'conservative default — Trimble NetR9, validate'),
    ('netr9',   '1Hz_1hr',    3,  'conservative default — Trimble NetR9, validate'),
    ('netrs',   '15s_24hr',   14, 'conservative default — Trimble NetRS (15s only), validate'),
    ('g10',     '15s_24hr',   14, 'conservative default — Leica G10, validate')
ON CONFLICT (receiver_type, session_type) DO NOTHING;

INSERT INTO schema_migrations (migration_name)
VALUES ('054_storage_location_registry')
ON CONFLICT DO NOTHING;

COMMIT;
