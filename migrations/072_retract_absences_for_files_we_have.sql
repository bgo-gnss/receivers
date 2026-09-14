-- Migration 072: drop file_absence rows for slots file_tracking says we HAVE
--
-- Companion to the mark_file_downloaded retraction. That stops NEW
-- contradictions; this clears the ones already in the table.
--
-- file_absence was write-only: record_file_absence inserts and promotes to
-- terminal, and until 2026-09-14 nothing ever retracted a row — not even a
-- later successful download of that exact file. So the table accumulated slots
-- that are simultaneously "absent on the receiver" and "downloaded/archived"
-- in file_tracking.
--
-- Measured on rek-d01 2026-09-14:
--
--     session_type | absent_but_we_have_it | of_which_terminal
--     -------------+-----------------------+------------------
--     15s_24hr     |                   719 |                 1
--     1Hz_1hr      |                  7236 |                 0
--     status_1hr   |                 35085 |                 0
--
-- WHY IT MATTERS EVEN THOUGH ALMOST NONE ARE TERMINAL YET. A non-terminal row
-- is not inert: it keeps its `confirmations` counter and its
-- `first_confirmed_at`, so it remains a candidate for promotion. Promote one of
-- these and is_file_missing() starts returning TRUE for a file already in the
-- archive — gap detection and the backfill would then skip a slot we hold. That
-- is the failure this whole line of work exists to prevent, and #174/S2
-- (enabling use_terminal_absence) cannot be reconsidered while 43,040 rows sit
-- one promotion away from it.
--
-- SAFETY. The delete is restricted to slots where file_tracking carries
-- status IN ('archived','downloaded') — i.e. we demonstrably have the file.
-- Rows for genuinely missing data are untouched; 'missing' and 'removed'
-- statuses are deliberately NOT matched.
--
-- file_hour uses IS NOT DISTINCT FROM to mirror the table's NULLS NOT DISTINCT
-- unique constraint. A plain `=` never matches NULL and would silently skip
-- every DAILY slot — the 15s_24hr population, which is where the one terminal
-- row lives.
--
-- REVERSIBLE. Every deleted row is copied to file_absence_retracted_072 first;
-- the rollback re-inserts from it. Do not drop that table until the next
-- migration after this one has bedded in.
--
-- Usage:
--   psql -h localhost -d gps_health -f migrations/072_retract_absences_for_files_we_have.sql

BEGIN;

CREATE TABLE IF NOT EXISTS file_absence_retracted_072 (LIKE file_absence);

COMMENT ON TABLE file_absence_retracted_072 IS
    'Backup of rows deleted by migration 072 (absences for files file_tracking '
    'says we hold). Kept so the delete is reversible; safe to drop once 072 has '
    'bedded in.';

WITH contradicted AS (
    SELECT fa.id
    FROM file_absence fa
    JOIN file_tracking ft
      ON ft.sid = fa.sid
     AND ft.session_type = fa.session_type
     AND ft.file_date = fa.file_date
     AND ft.file_hour IS NOT DISTINCT FROM fa.file_hour
    WHERE fa.source_location = 'receiver'
      AND ft.status IN ('archived', 'downloaded')
)
INSERT INTO file_absence_retracted_072
SELECT fa.* FROM file_absence fa JOIN contradicted c ON c.id = fa.id;

DELETE FROM file_absence fa
USING file_absence_retracted_072 b
WHERE fa.id = b.id;

INSERT INTO schema_migrations (migration_name)
VALUES ('072_retract_absences_for_files_we_have')
ON CONFLICT DO NOTHING;

COMMIT;
