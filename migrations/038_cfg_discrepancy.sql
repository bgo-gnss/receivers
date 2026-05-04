-- Migration 038: cfg discrepancy event log
--
-- Append-only audit log for stations.cfg vs receiver vs TOS discrepancies.
--
-- One open row at a time per (station_id, cfg_key); when the same drift is
-- re-detected the existing open row is reused (idempotent). When values
-- change while the row is still open the old row is marked superseded and a
-- new open row is inserted, preserving the value-over-time history.
--
-- Resolution writes to the same row: resolved_at/by/action/value/note.
-- Possible resolved_action values:
--   'cfg_updated'    — operator wrote the value into stations.cfg via cfg reconcile
--   'tos_updated'    — value pushed to TOS (future --push-tos work)
--   'auto-resolved'  — values converged without operator action (e.g. cfg fixed manually)
--   'superseded'     — replaced by a fresher detection with different values
--   'ignored'        — operator suppressed via cfg ignore (future)

BEGIN;

CREATE TABLE IF NOT EXISTS cfg_discrepancy (
    id              BIGSERIAL    PRIMARY KEY,
    station_id      VARCHAR(8)   NOT NULL,
    cfg_key         VARCHAR(64)  NOT NULL,
    cfg_value       TEXT,
    receiver_value  TEXT,
    tos_value       TEXT,
    verdict         VARCHAR(32)  NOT NULL,
    detected_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    detected_by     VARCHAR(32)  NOT NULL,
    resolved_at     TIMESTAMPTZ,
    resolved_by     VARCHAR(64),
    resolved_action VARCHAR(32),
    resolved_value  TEXT,
    resolution_note TEXT
);

COMMENT ON TABLE cfg_discrepancy IS
  'Audit log of cfg/receiver/TOS discrepancies. One open row per (station, field); '
  'resolution updates the same row in place.';

COMMENT ON COLUMN cfg_discrepancy.detected_by IS
  'Origin of the detection: health_probe, cfg_reconcile, scheduler.';
COMMENT ON COLUMN cfg_discrepancy.verdict IS
  'Verdict at detection time: missing, conflict, sources_disagree.';
COMMENT ON COLUMN cfg_discrepancy.resolved_action IS
  'How the row was closed: cfg_updated, tos_updated, auto-resolved, superseded, ignored.';

-- Partial unique index: at most one open row per (station, field).
-- Lets writers use ON CONFLICT to upsert against the open row.
CREATE UNIQUE INDEX IF NOT EXISTS cfg_discrepancy_open_unique
    ON cfg_discrepancy (station_id, cfg_key)
    WHERE resolved_at IS NULL;

-- Per-station history scan (for `cfg history <SID>`).
CREATE INDEX IF NOT EXISTS cfg_discrepancy_station_time
    ON cfg_discrepancy (station_id, detected_at DESC);

-- Field-wide history (for `cfg history --field <KEY>`).
CREATE INDEX IF NOT EXISTS cfg_discrepancy_key_time
    ON cfg_discrepancy (cfg_key, detected_at DESC);

INSERT INTO schema_migrations (migration_name)
VALUES ('038_cfg_discrepancy')
ON CONFLICT DO NOTHING;

COMMIT;
