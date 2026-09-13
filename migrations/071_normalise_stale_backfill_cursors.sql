-- Migration 071: normalise completed backfill_progress rows whose cursor never
-- advanced past their own end.
--
-- Companion to the _enqueue_backfill cursor-reset fix. That stops NEW stale
-- cursors from being resurrected; this defuses the ones already sitting in the
-- table.
--
-- Measured on rek-d01 2026-09-13 (543 rows: 534 completed, 9 in_progress).
-- THIS MIGRATION CHANGES 36 ROWS — the ones whose cursor never reached their own
-- end. Do not confuse that with the wider "200 rows with a cursor more than 14
-- days old" figure: most of those have next_date correctly one day past
-- backfill_end and are merely old, which is a much milder shape (a ~16-day
-- re-walk rather than an ~85-day one) and is handled by the code fix on
-- re-activation anyway.
--
-- The 36 are the malformed ones. Example: GRVA/1Hz_1hr had
-- backfill_start 2026-06-14, next_date 2026-05-17, backfill_end 2026-08-10 —
-- a cursor 85 days BEHIND its own end, which should be impossible for a row
-- marked completed.
--
-- Origin: long_term_backfill writes through this shared row
-- (_backfill_station_day_generic sets next_date = gap_date + 1 then
-- status = 'completed'), and it ran 2026-08-08..11 with a 90-day lookback. Every
-- one of these rows was last touched in that window or shortly after; NOTHING
-- has touched them since 2026-08-28, so this is frozen residue, not an active
-- process.
--
-- Why it still matters with long_term_backfill disabled: these are latent, not
-- inert. _enqueue_backfill used LEAST(existing, requested) on re-activation, so
-- the FIRST time one of these stations developed a single fresh gap, gap
-- detection would re-activate it at the May cursor and the worker would walk
-- ~85 days one date at a time — almost all of them beyond what the receiver
-- still holds (the 1Hz ring buffer is ~30 days), so almost all erroring.
--
-- HONEST SCOPE: this migration is hygiene, not a prerequisite. Once the code
-- fix ships, a re-activation resets the cursor regardless of what the row held,
-- and the queue worker only ever selects status IN ('pending','in_progress') —
-- so a completed row's stale cursor is unreachable. What this buys is a table
-- whose state is self-consistent for whoever reads it next, and no reliance on
-- the code fix being present to stay safe.
--
-- The target shape is the one 498 healthy rows already have: next_date exactly
-- one day past backfill_end, i.e. "this range is finished".

UPDATE backfill_progress
   SET next_date  = backfill_end + 1,
       updated_at = NOW()
 WHERE status = 'completed'
   AND next_date <= backfill_end;

INSERT INTO schema_migrations (migration_name)
VALUES ('071_normalise_stale_backfill_cursors')
ON CONFLICT DO NOTHING;
