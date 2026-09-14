-- Verify 072. Every check must report PASS.

\echo '== 1. no absence row remains for a slot file_tracking says we HAVE =='
SELECT CASE WHEN count(*) = 0 THEN 'PASS'
            ELSE 'FAIL: ' || count(*) || ' contradictions remain' END AS contradiction_check
FROM file_absence fa
JOIN file_tracking ft
  ON ft.sid = fa.sid AND ft.session_type = fa.session_type
 AND ft.file_date = fa.file_date
 AND ft.file_hour IS NOT DISTINCT FROM fa.file_hour
WHERE fa.source_location = 'receiver' AND ft.status IN ('archived','downloaded');

\echo '== 2. DAILY slots were retracted too (the NULL-file_hour trap) =='
-- A plain `=` on file_hour matches no NULL, so a broken version of this
-- migration leaves every daily row behind while looking successful overall.
SELECT CASE WHEN count(*) FILTER (WHERE file_hour IS NULL) > 0 THEN 'PASS'
            ELSE 'FAIL: no daily rows in the backup — NULL handling is wrong' END
       AS daily_check
FROM file_absence_retracted_072;

\echo '== 3. deletions are accounted for, and reversible =='
SELECT count(*) AS rows_backed_up,
       count(*) FILTER (WHERE terminal) AS of_which_terminal,
       count(DISTINCT sid) AS stations
FROM file_absence_retracted_072;

\echo '== 4. rows for genuinely missing data were NOT touched =='
SELECT session_type, count(*) AS absences_kept,
       count(*) FILTER (WHERE terminal) AS terminal_kept
FROM file_absence GROUP BY 1 ORDER BY 1;

\echo '== 5. recorded =='
SELECT CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END AS recorded_check
FROM schema_migrations WHERE migration_name = '072_retract_absences_for_files_we_have';
