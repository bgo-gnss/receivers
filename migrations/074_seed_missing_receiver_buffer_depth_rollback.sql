-- Rollback 074 — remove the two seeded rows and the migration record.
--
-- Data-only. Removing them restores the previous behaviour: netrs 1Hz and netr5
-- daily fall back out of the session-map, the probe scope and the static floor,
-- so the LTB returns to queueing those unfloored. Deletes ONLY the two exact
-- rows this migration added — never the whole table.

BEGIN;

DELETE FROM receiver_buffer_depth
 WHERE (receiver_type, session_type) IN (('netrs', '1Hz_1hr'), ('netr5', '15s_24hr'), ('g10', '1Hz_1hr'));

DELETE FROM schema_migrations
 WHERE migration_name = '074_seed_missing_receiver_buffer_depth';

COMMIT;
