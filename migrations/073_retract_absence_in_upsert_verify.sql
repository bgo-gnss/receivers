-- Verify migration 073 — STRUCTURAL, and it exercises the function for real.
--
-- Checking the function text alone would pass on a function that contains the
-- DELETE but never reaches it. Check 3 actually calls it against a synthetic
-- slot, inside a transaction that is rolled back, so the assertion is about
-- BEHAVIOUR. The station id is deliberately one that cannot collide with a real
-- 4-char SID.

\set ON_ERROR_STOP on

-- 1. the migration is recorded
SELECT CASE WHEN count(*) = 1 THEN 'PASS'
            ELSE 'FAIL: migration row missing' END AS migration_recorded
FROM schema_migrations WHERE migration_name = '073_retract_absence_in_upsert';

-- 2. the guard is present AND scoped (not firing on every status)
SELECT CASE
         WHEN prosrc LIKE '%DELETE FROM file_absence%'
          AND prosrc LIKE '%p_status IN (''archived'', ''downloaded'')%'
          AND prosrc LIKE '%IS NOT DISTINCT FROM p_hour%'
         THEN 'PASS'
         ELSE 'FAIL: retraction block absent, unscoped, or using a plain = on file_hour'
       END AS guard_present
FROM pg_proc WHERE proname = 'upsert_file_tracking';

-- 3. it actually retracts — and ONLY for a present status.
--
-- `file_tracking.sid` is FK-constrained to `stations`, so this borrows two REAL
-- station ids and uses an impossible file_date (1999-01-01, before the network
-- existed) so it cannot collide with data. The whole block is rolled back.
BEGIN;
CREATE TEMP TABLE _v AS
SELECT sid, row_number() OVER (ORDER BY sid) AS n FROM stations ORDER BY sid LIMIT 2;

INSERT INTO file_absence (source_location, sid, session_type, file_date, file_hour,
                          confirmations, terminal, first_confirmed_at)
SELECT 'receiver', sid, '15s_24hr', DATE '1999-01-01', NULL, 1, false, NOW() FROM _v;

-- a 'missing' write must NOT retract
SELECT upsert_file_tracking((SELECT sid FROM _v WHERE n = 2), '15s_24hr',
                            DATE '1999-01-01', NULL::smallint, 'v.dat', 'missing');
SELECT CASE WHEN count(*) = 1 THEN 'PASS'
            ELSE 'FAIL: a missing-status write retracted an absence' END
       AS missing_does_not_retract
FROM file_absence
WHERE sid = (SELECT sid FROM _v WHERE n = 2) AND file_date = DATE '1999-01-01';

-- an 'archived' write MUST retract (the stream / integrity / push path)
SELECT upsert_file_tracking((SELECT sid FROM _v WHERE n = 1), '15s_24hr',
                            DATE '1999-01-01', NULL::smallint, 'v.dat', 'archived');
SELECT CASE WHEN count(*) = 0 THEN 'PASS'
            ELSE 'FAIL: archived write left a contradicting absence' END
       AS archived_retracts
FROM file_absence
WHERE sid = (SELECT sid FROM _v WHERE n = 1) AND file_date = DATE '1999-01-01';
ROLLBACK;

-- 4. no contradictions remain fleet-wide
SELECT CASE WHEN count(*) = 0 THEN 'PASS'
            ELSE 'FAIL: ' || count(*) || ' absence row(s) contradict file_tracking' END
       AS no_contradictions
FROM file_absence fa
JOIN file_tracking ft
  ON ft.sid = fa.sid AND ft.session_type = fa.session_type
 AND ft.file_date = fa.file_date
 AND ft.file_hour IS NOT DISTINCT FROM fa.file_hour
WHERE fa.source_location = 'receiver'
  AND ft.status IN ('archived', 'downloaded');
