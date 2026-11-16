-- Migration 076: truncation_repair — ledger of `receivers archive-truncation apply`
--
-- A truncated raw .gz looks present to every cheap check (existence, size,
-- gzip magic), so it is never re-fetched and its RINEX fails forever. The
-- archive-truncation verb repairs it by the first rule that matches:
-- replace from a complete copy, refetch from the receiver, or SALVAGE the
-- CRC-valid SBF prefix. A salvaged file then passes `gzip -t` and looks
-- complete for good, so the fact that it is PARTIAL must live somewhere
-- durable. This table is that place, and the audit trail of every write:
--
--   SELECT rel_path, kept_bytes, readable_bytes, applied_at
--     FROM truncation_repair WHERE is_partial;
--
-- It is also a fact the planner reads: a refetch that found the file missing
-- on the receiver (outcome 'not_on_receiver') makes the next plan treat the
-- receiver as no longer holding it, so the item can move on to salvage.
--
-- Written by apply directly to EVERY catalog host (rek-d01 + pgdev), one
-- connection each, not through the mirror fan-out — so it must exist on both.
--
-- Reversible: 076_truncation_repair_rollback.sql drops it. Nothing in the
-- scheduler reads it; only the planner does, and it degrades to "no ledger".
--
-- Usage (both hosts, as the table owner):
--   psql -d gps_health -v ON_ERROR_STOP=1 -f migrations/076_truncation_repair.sql

BEGIN;

CREATE TABLE IF NOT EXISTS truncation_repair (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT        NOT NULL,
    rel_path            TEXT        NOT NULL,  -- YYYY/mon/STA/session/raw/FILE
    station             VARCHAR(9)  NOT NULL,
    session_type        VARCHAR(32) NOT NULL,
    file_date           DATE        NOT NULL,
    file_hour           SMALLINT,
    action              TEXT        NOT NULL
        CHECK (action IN ('replace', 'refetch', 'salvage', 'unrecoverable', 'unsupported')),
    outcome             TEXT        NOT NULL
        CHECK (outcome IN ('repaired', 'refused', 'failed', 'deferred',
                           'not_on_receiver', 'unreachable', 'recorded')),
    is_partial          BOOLEAN     NOT NULL DEFAULT FALSE,
    source              TEXT,               -- 'archive' | 'local' | NULL (refetch)
    readable_bytes      BIGINT,             -- decompressed bytes the broken copy yields
    kept_bytes          BIGINT,             -- bytes kept by salvage (CRC-valid prefix)
    blocks              INTEGER,
    orig_archive_size   BIGINT,
    orig_archive_csha   CHAR(64),
    orig_local_size     BIGINT,
    orig_local_csha     CHAR(64),
    new_content_sha256  CHAR(64),
    new_size            BIGINT,
    detail              TEXT,
    applied_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NOT is_partial OR (action = 'salvage' AND outcome = 'repaired'))
);

CREATE INDEX IF NOT EXISTS idx_truncation_repair_rel
    ON truncation_repair (rel_path);
CREATE INDEX IF NOT EXISTS idx_truncation_repair_partial
    ON truncation_repair (station, file_date) WHERE is_partial;

INSERT INTO schema_migrations (migration_name)
VALUES ('076_truncation_repair')
ON CONFLICT DO NOTHING;

COMMIT;
