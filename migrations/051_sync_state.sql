-- Migration 051: sync_state — per-target high-water mark for archive/dissemination sync
--
-- The archive-sync engine (receivers.archive, `receivers archive-sync`) pushes
-- only files newer than a MOVING watermark, not a fixed cutover. With a fixed
-- floor every :45 run would re-scan and re-hash the entire post-cutover corpus
-- (~8k files/day, content_sha256 = decompress+hash each) — unbounded, blows the
-- hourly window within days. The watermark advances each successful run, so each
-- run only touches ~the last interval's files.
--
-- Per design 1781867391 (decision 2): floor = max(last_success_ts - overlap, cutover).
--   - bootstrap: last_success_ts NULL on first run => floor = cutover (config), so
--     legacy-era files (mtime < cutover) never enter the delta (collision-avoidance).
--   - overlap: a few minutes of re-scan guards the mtime-boundary / clock-skew race;
--     harmless given idempotent archive_catalog upsert + rsync --ignore-existing.
--     For an archive feed, silently dropping a boundary file is the worst failure,
--     so the overlap is load-bearing, not polish.
--
-- last_success_ts advances ONLY on a fully-successful run. It is a dedicated
-- column, NOT derived from MAX(archive_catalog.indexed_at): a partial failure
-- would leave that max ahead of the truly-synced frontier and silently skip the
-- gap on the next run.

BEGIN;

CREATE TABLE IF NOT EXISTS sync_state (
    -- target name from sync.yaml (e.g. 'imo_archive'). One row per target.
    target           TEXT        PRIMARY KEY,
    -- frontier: the scan-start of the last fully-synced run. NULL until the
    -- first success. WITHOUT TIME ZONE on purpose: this is a watermark in the
    -- FILE-MTIME domain (compared against `find -newermt` / os.path.getmtime,
    -- which are naive local time on the collection host), NOT a wall-clock
    -- event. A TIMESTAMPTZ would return tz-aware values and break the
    -- naive-vs-aware comparison in compute_floor().
    last_success_ts  TIMESTAMP,
    -- last attempt's scan-start (same naive-local domain) for the freshness monitor.
    last_run_at      TIMESTAMP,
    last_run_files   INTEGER,
    last_run_ok      BOOLEAN,
    -- true wall-clock audit field — tz-aware is correct here.
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (migration_name)
VALUES ('051_sync_state')
ON CONFLICT DO NOTHING;

COMMIT;
