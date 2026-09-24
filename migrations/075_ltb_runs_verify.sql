-- Verify migration 075 — BEHAVIOURAL, not just "the table exists".
--
-- Checks 3-6 exercise the properties the table is FOR, inside a transaction
-- that is rolled back: the generated cap-usage columns compute (and are NULL,
-- not an error, for an unbounded cap); a bogus stop_reason is refused; the
-- natural key rejects a duplicate run (what makes the mirror fan-out safe);
-- and the writer's exact INSERT shape — ON CONFLICT on that key — is accepted.
-- started_at values are in 1999, before the network existed, so they cannot
-- collide with a real run.

\set ON_ERROR_STOP on

-- 1. the migration is recorded
SELECT CASE WHEN count(*) = 1 THEN 'PASS'
            ELSE 'FAIL: migration row missing' END AS migration_recorded
FROM schema_migrations WHERE migration_name = '075_ltb_runs';

-- 2. the cap indexes exist (the trend query and the stopped-early query)
SELECT CASE WHEN count(*) = 2 THEN 'PASS'
            ELSE 'FAIL: expected 2 indexes, got ' || count(*) END AS indexes_present
FROM pg_indexes
WHERE tablename = 'ltb_runs'
  AND indexname IN ('idx_ltb_runs_kind_started', 'idx_ltb_runs_stopped_early');

BEGIN;

-- 3. THE POINT: the cap question is precomputed. A run that used 1480 of
--    3600 s and 217 of 600 slots must read 41.1% / 36.2%; an unbounded cap
--    must give NULL, not a divide-by-zero.
INSERT INTO ltb_runs (run_kind, started_at, finished_at, duration_seconds,
                      max_run_seconds, slots_used, max_slots, stop_reason,
                      stop_detail, stations, sessions, lookback)
VALUES ('daily', '1999-01-01T04:00:00Z', '1999-01-01T04:24:40Z', 1480,
        3600, 217, 600, NULL, NULL, 180, '{15s_24hr,1Hz_1hr}',
        '15s_24hr=90d 1Hz_1hr=30d'),
       ('daily', '1999-01-02T04:00:00Z', '1999-01-02T05:00:01Z', 3601,
        3600, 288, 600, 'wall-clock', 'wall-clock 3601s >= 3600s', 180,
        '{15s_24hr,1Hz_1hr}', '15s_24hr=90d 1Hz_1hr=30d'),
       ('reconnect', '1999-01-01T04:15:00Z', '1999-01-01T04:16:00Z', 60,
        NULL, 3, NULL, NULL, NULL, 1, '{15s_24hr}', '15s_24hr=90d');

SELECT CASE
         WHEN round(time_used_pct::numeric, 1) = 41.1
          AND round(slots_used_pct::numeric, 1) = 36.2
         THEN 'PASS'
         ELSE 'FAIL: generated pct wrong: ' || time_used_pct || ' / ' || slots_used_pct
       END AS pct_computed
FROM ltb_runs WHERE run_kind = 'daily' AND started_at = '1999-01-01T04:00:00Z';

SELECT CASE WHEN time_used_pct IS NULL AND slots_used_pct IS NULL THEN 'PASS'
            ELSE 'FAIL: unbounded cap must yield NULL pct' END AS unbounded_is_null
FROM ltb_runs WHERE run_kind = 'reconnect' AND started_at = '1999-01-01T04:15:00Z';

-- 4. the stopped-early query returns exactly the capped run
SELECT CASE WHEN count(*) = 1 AND min(stop_reason) = 'wall-clock' THEN 'PASS'
            ELSE 'FAIL: stopped-early query returned ' || count(*) END AS capped_run_found
FROM ltb_runs WHERE stop_reason IS NOT NULL AND started_at < '2000-01-01';

-- 5. a stop_reason outside the vocabulary is REFUSED
DO $$
BEGIN
    INSERT INTO ltb_runs (run_kind, started_at, finished_at, duration_seconds,
                          slots_used, stop_reason, stations)
    VALUES ('daily', '1999-01-03T04:00:00Z', '1999-01-03T04:00:01Z', 1, 0,
            'coffee', 0);
    RAISE EXCEPTION 'FAIL: bogus stop_reason was accepted';
EXCEPTION
    WHEN check_violation THEN RAISE NOTICE 'PASS: bogus stop_reason refused';
END $$;

-- 6. the natural key refuses a duplicate run — and the writer's exact
--    INSERT shape (ON CONFLICT (run_kind, started_at) DO NOTHING) is a
--    no-op on it rather than an error, which is what makes a mirror replay
--    idempotent.
DO $$
BEGIN
    INSERT INTO ltb_runs (run_kind, started_at, finished_at, duration_seconds,
                          slots_used, stations)
    VALUES ('daily', '1999-01-01T04:00:00Z', '1999-01-01T04:00:01Z', 1, 0, 0);
    RAISE EXCEPTION 'FAIL: duplicate (run_kind, started_at) was accepted';
EXCEPTION
    WHEN unique_violation THEN RAISE NOTICE 'PASS: duplicate run refused';
END $$;

INSERT INTO ltb_runs (run_kind, started_at, finished_at, duration_seconds,
                      slots_used, stations)
VALUES ('daily', '1999-01-01T04:00:00Z', '1999-01-01T04:00:01Z', 1, 0, 0)
ON CONFLICT (run_kind, started_at) DO NOTHING;

SELECT CASE WHEN count(*) = 3 THEN 'PASS'
            ELSE 'FAIL: expected 3 synthetic rows after the no-op upsert, got ' || count(*)
       END AS upsert_is_noop
FROM ltb_runs WHERE started_at < '2000-01-01';

ROLLBACK;

-- 7. nothing synthetic leaked
SELECT CASE WHEN count(*) = 0 THEN 'PASS'
            ELSE 'FAIL: synthetic rows survived the rollback' END AS rolled_back
FROM ltb_runs WHERE started_at < '2000-01-01';
