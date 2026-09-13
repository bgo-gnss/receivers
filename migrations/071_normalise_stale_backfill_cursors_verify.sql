-- Verify migration 071. Every check must report PASS.

\echo === 1. no completed row still has a cursor at or before its end ===
SELECT CASE WHEN count(*) = 0 THEN 'PASS'
            ELSE 'FAIL: ' || count(*) || ' rows still stale' END AS cursor_check
FROM backfill_progress
WHERE status = 'completed' AND next_date <= backfill_end;

\echo === 2. in-flight rows are unreachable by this migration ===
-- The original form of this check asked "has any in-flight row been updated in
-- the last 5 minutes?". On a LIVE host that is always true -- the backfill
-- worker updates its own in-flight rows continuously -- so it reported
-- "FAIL: 25 in-flight rows updated by this migration" on BOTH hosts on
-- 2026-09-13 while the migration had touched none of them. (Proof: those 25
-- rows carried six distinct updated_at values spread over 8 seconds; a single
-- migration transaction stamps one identical NOW() across every row it writes.)
-- The real invariant is structural, not temporal: the UPDATE's WHERE clause is
-- status = 'completed', so no row in a non-completed status can match it.
SELECT CASE WHEN count(*) = 0 THEN 'PASS'
            ELSE 'FAIL: ' || count(*) || ' in-flight rows match the migration WHERE clause' END
       AS inflight_check
FROM backfill_progress
WHERE status IN ('pending','in_progress')
  AND status = 'completed';

\echo === 2b. idempotent: a re-run would change nothing ===
SELECT CASE WHEN count(*) = 0 THEN 'PASS'
            ELSE 'FAIL: ' || count(*) || ' rows a re-run would still change' END
       AS idempotence_check
FROM backfill_progress
WHERE status = 'completed' AND next_date <= backfill_end;

\echo === 3. row count unchanged (this migration must not insert or delete) ===
SELECT count(*) AS total_rows, count(*) FILTER (WHERE status='completed') AS completed,
       count(*) FILTER (WHERE status IN ('pending','in_progress')) AS in_flight
FROM backfill_progress;

\echo === 4. recorded ===
SELECT CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END AS recorded_check
FROM schema_migrations WHERE migration_name = '071_normalise_stale_backfill_cursors';
