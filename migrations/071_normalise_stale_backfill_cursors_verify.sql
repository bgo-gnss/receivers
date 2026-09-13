-- Verify migration 071. Every check must report PASS.

\echo === 1. no completed row still has a cursor at or before its end ===
SELECT CASE WHEN count(*) = 0 THEN 'PASS'
            ELSE 'FAIL: ' || count(*) || ' rows still stale' END AS cursor_check
FROM backfill_progress
WHERE status = 'completed' AND next_date <= backfill_end;

\echo === 2. in-flight rows were NOT touched (their worker owns the cursor) ===
SELECT CASE WHEN count(*) = 0 THEN 'PASS'
            ELSE 'FAIL: ' || count(*) || ' in-flight rows updated by this migration' END
       AS inflight_check
FROM backfill_progress
WHERE status IN ('pending','in_progress')
  AND updated_at > now() - interval '5 minutes';

\echo === 3. row count unchanged (this migration must not insert or delete) ===
SELECT count(*) AS total_rows, count(*) FILTER (WHERE status='completed') AS completed,
       count(*) FILTER (WHERE status IN ('pending','in_progress')) AS in_flight
FROM backfill_progress;

\echo === 4. recorded ===
SELECT CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END AS recorded_check
FROM schema_migrations WHERE migration_name = '071_normalise_stale_backfill_cursors';
