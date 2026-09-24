-- Migration 075: ltb_runs — one row per long-term-backfill run
--
-- The long-term backfill (LTB: the 04:00 daily backstop and the 15-minute
-- reconnection trigger, both in scheduling/long_term_backfill.py) reported
-- itself ONLY as a log line:
--
--   Long-term backfill(daily) done: 38 station/session had queued gaps,
--   budget 217/600 slots, 1480s/3600s
--
-- Nothing consumed it. `backfill_progress` is PER-STATION cursor state, not
-- per-run metrics; no Grafana panel covered the LTB; getting a morning's
-- numbers meant grepping a 182 MB log. Nothing trended the run or would
-- notice a regression.
--
-- The failure this exists to make visible is SILENT: when a run hits its
-- wall-clock cap it just recovers less each night, while every pipeline
-- dashboard stays green because live downloads are unaffected. scheduler.yaml
-- records that the first three fleet runs all stopped on the 3600 s clock
-- while slots peaked at 288. Raising the daily lookback 30 -> 90 days makes
-- the clock the main risk, so "did this run hit a cap, and is that getting
-- worse?" has to be a trivial query:
--
--   SELECT started_at::date, stop_reason, time_used_pct, slots_used_pct,
--          recovered, nothing_on_receiver
--     FROM ltb_runs WHERE run_kind = 'daily' ORDER BY started_at DESC;
--
-- Design:
--   * run_kind — the two jobs share a config section but have DIFFERENT
--     ceilings (max_run_seconds 1800 vs reconnection_max_run_seconds 600), so
--     a row carries its own cap and the kind that owned it.
--   * stop_reason — categorical (NULL = ran to completion), from
--     RunBudget.exhausted_reason's first token; stop_detail keeps the verbatim
--     text ("wall-clock 3601s >= 3600s"). 'other' is allowed so a future third
--     ceiling records a row instead of tripping the CHECK.
--   * time_used_pct / slots_used_pct — GENERATED, so the trend query above
--     needs no arithmetic and a NULL (unbounded) cap yields NULL, not a divide
--     by zero.
--   * UNIQUE (run_kind, started_at) — the natural key. The writer upserts on
--     it (ON CONFLICT DO NOTHING), never on id, which is what keeps the
--     database_factory mirror fan-out to pgdev safe: surrogate ids differ per
--     host, and id-keyed DML is refused by the fan-out.
--   * Outcome columns are the per-station report fields summed over the run.
--     `recovered` and `failed` were LOCALS in the worker until this change —
--     logged, then dropped — so the run could count its queued gaps but never
--     what came of them.
--
-- Retention: NONE, deliberately. One daily row plus a reconnect row only on
-- ticks that actually had candidates (an idle tick never builds a budget and
-- is not recorded) is well under 10k rows/year at ~200 B each. A prune would
-- be more code than the data it saves; the trend is the point, so keep it.
-- Revisit if the reconnect trigger ever fires with candidates every tick.
--
-- Reversible: see 075_ltb_runs_rollback.sql (drops the table — the rows are
-- observations, not state anything depends on).
--
-- Usage:
--   psql -d gps_health -v ON_ERROR_STOP=1 -f migrations/075_ltb_runs.sql
--   (must be applied on BOTH catalog hosts — rek-d01 AND pgdev — for the
--   mirror leg to accept the row; see the memory note on hand-applied pgdev
--   migrations.)

BEGIN;

CREATE TABLE IF NOT EXISTS ltb_runs (
    id                      BIGSERIAL   PRIMARY KEY,
    run_kind                VARCHAR(16) NOT NULL
                            CHECK (run_kind IN ('daily', 'reconnect')),
    started_at              TIMESTAMPTZ NOT NULL,
    finished_at             TIMESTAMPTZ NOT NULL,
    duration_seconds        REAL        NOT NULL CHECK (duration_seconds >= 0),
    max_run_seconds         REAL,                 -- NULL = unbounded
    slots_used              INTEGER     NOT NULL CHECK (slots_used >= 0),
    max_slots               INTEGER,              -- NULL = unbounded
    -- which ceiling stopped the run; NULL = ran to completion
    stop_reason             VARCHAR(16)
                            CHECK (stop_reason IN ('wall-clock', 'slots', 'other')),
    stop_detail             TEXT,                 -- RunBudget.exhausted_reason verbatim
    stations                INTEGER     NOT NULL CHECK (stations >= 0),
    sessions                TEXT[]      NOT NULL DEFAULT '{}',
    lookback                TEXT        NOT NULL DEFAULT '',  -- "15s_24hr=90d 1Hz_1hr=30d"
    -- outcomes, summed over the run's per-station reports
    queued_station_sessions INTEGER     NOT NULL DEFAULT 0,
    recovered               INTEGER     NOT NULL DEFAULT 0,
    nothing_on_receiver     INTEGER     NOT NULL DEFAULT 0,
    failed                  INTEGER     NOT NULL DEFAULT 0,
    unreachable             INTEGER     NOT NULL DEFAULT 0,
    budget_capped           INTEGER     NOT NULL DEFAULT 0,
    skipped_offline         INTEGER     NOT NULL DEFAULT 0,
    errors                  INTEGER     NOT NULL DEFAULT 0,
    -- the cap question, precomputed. NULL cap -> NULL, never a divide by zero.
    time_used_pct           REAL GENERATED ALWAYS AS (
                                CASE WHEN max_run_seconds > 0
                                     THEN 100.0 * duration_seconds / max_run_seconds
                                END) STORED,
    slots_used_pct          REAL GENERATED ALWAYS AS (
                                CASE WHEN max_slots > 0
                                     THEN 100.0 * slots_used / max_slots
                                END) STORED,
    CONSTRAINT uq_ltb_runs_kind_started UNIQUE (run_kind, started_at)
);

-- The trend query: one kind, newest first.
CREATE INDEX IF NOT EXISTS idx_ltb_runs_kind_started
    ON ltb_runs (run_kind, started_at DESC);

-- "Which runs stopped early?" — partial, so it stays tiny however long the
-- table gets and the healthy majority never enters it.
CREATE INDEX IF NOT EXISTS idx_ltb_runs_stopped_early
    ON ltb_runs (started_at DESC)
    WHERE stop_reason IS NOT NULL;

COMMENT ON TABLE ltb_runs IS
    'One row per long-term-backfill run (daily backstop / reconnection trigger): '
    'budget ceilings vs used, which ceiling stopped it (NULL = completed), and '
    'the per-station outcomes summed. Written best-effort by '
    'receivers.scheduling.ltb_run_metrics; never read by the scheduler.';
COMMENT ON COLUMN ltb_runs.stop_reason IS
    'Ceiling that ended the run: wall-clock | slots | other. NULL = ran to completion.';
COMMENT ON COLUMN ltb_runs.nothing_on_receiver IS
    'Slots that reached the receiver and found no file — NOT a recovery.';
COMMENT ON COLUMN ltb_runs.time_used_pct IS
    'duration_seconds / max_run_seconds * 100; NULL when unbounded. >= 100 means the clock stopped the run.';

INSERT INTO schema_migrations (migration_name)
VALUES ('075_ltb_runs')
ON CONFLICT DO NOTHING;

COMMIT;
