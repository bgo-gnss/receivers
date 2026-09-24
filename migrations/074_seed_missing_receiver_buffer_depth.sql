-- Migration 074: seed the receiver_buffer_depth rows the fleet actually needs
--
-- `receiver_buffer_depth` does THREE jobs, and a missing row silently disables
-- all three for that (receiver_type, session_type):
--
--   1. SESSION-MAP — migration 060: "a station is only expected to produce the
--      sessions its receiver_type has floors for". No row => absences for that
--      session are never generated, so the gap is invisible to
--      `missing_on_receiver`.
--   2. PROBE SCOPE — `receiver_horizon_probe._sessions_by_receiver_type` reads
--      this table to decide what to walk. No row => never probed => no
--      `receiver_horizon`.
--   3. STATIC FLOOR — the fallback when no measured horizon exists. With none,
--      `long_term_backfill.py` computes `horizon_floor = horizon or date.min`,
--      i.e. NO floor: the LTB queues the entire lookback window and spends its
--      slot budget asking receivers for days they discarded long ago.
--
-- MEASURED on rek-d01, 2026-09-24:
--   * The 04:00 LTB pass: 217/600 slots, recovered=14, nothing_on_receiver=195.
--     A 6.5% yield; the bulk of the budget went to unfloored stations.
--   * 36 active stations had no 1Hz horizon at all — 24 of them netrs.
--
-- (a) netrs / 1Hz_1hr — the 054 seed says "Trimble NetRS (15s only), validate".
--     That assumption was never validated and is WRONG: 23 of 24 active netrs
--     stations produced 1Hz in the last 120 days (archive_catalog, ~5,000 files
--     per station, continuous from 2026-05-28). So 23 stations' 1Hz data is
--     currently outside the session-map and unfloored.
--
--     This floor is AUTHORITATIVE and cannot be refined later: the horizon probe
--     deliberately cannot reach a NetRS — its HTTP API has no `directory` verb
--     ("Invalid verb/object combination"), documented in
--     `receiver_horizon_probe._probe_trimble`. Everywhere else the probe
--     overrides the static value; here the static value is all there is. Set to
--     3 days to match its NetR9 sibling (NetRS is the older, smaller unit, so
--     this should not over-claim). STILL WORTH VALIDATING against a live NetRS —
--     but unlike the 054 note, being wrong here now fails toward under-queueing
--     (missed recovery) rather than unbounded futile requests.
--
-- (b) netr5 / 15s_24hr — AKUR and ISAF have NO rows at all, so they are outside
--     all three mechanisms. Both produce daily data; NEITHER produced any 1Hz in
--     the last 120 days, so only the daily row is added — seeding 1Hz would
--     re-create the same futile queueing this migration exists to stop. netr5 IS
--     in the probe's `_TRIMBLE_TYPES`, so 14 is a seed the probe can refine,
--     matching its netr9/netrs siblings.
--
-- (c) g10 / 1Hz_1hr — SKFC, the single Leica G10, produces 1Hz (5,143 files
--     since 2026-06-01) but the 054 seed gave it a daily row only. Found by this
--     migration's own verify query, NOT by inspection — the same "one session
--     assumed" mistake as netrs, in a second place. Note SKFC currently has NO
--     measured horizon for EITHER session despite having a daily row, so the
--     Leica probe path may not be yielding either; the static floor is doing the
--     work here regardless. Seeded at 3 (the most conservative 1Hz value in the
--     table) rather than polarx5's 7, since nothing has measured it.
--
-- DELIBERATELY NOT INCLUDED: the 4 stations with a NULL receiver_type (KRAC,
-- MYVA, THRC, TORK). All are `health_check = passive` — their data arrives
-- externally and there is no receiver to probe or fetch from. A buffer depth
-- would be meaningless; excluding them from the LTB/probe is a separate change.
--
-- Reversible: data-only, see 074_..._rollback.sql.

BEGIN;

INSERT INTO receiver_buffer_depth (receiver_type, session_type, depth_days, notes) VALUES
    ('netrs', '1Hz_1hr',  3,  'NetRS 1Hz — 23/24 stations produce it (measured 2026-09-24). '
                              'AUTHORITATIVE: the horizon probe cannot reach a NetRS (no HTTP '
                              'directory verb), so this static floor is never refined. Matches '
                              'netr9 1Hz; validate against a live NetRS.'),
    ('netr5', '15s_24hr', 14, 'Trimble NetR5 (AKUR, ISAF) — daily only; neither produced 1Hz in '
                              '120 days. Probe-refinable (netr5 is in _TRIMBLE_TYPES). Matches '
                              'the netr9/netrs daily seed; validate.'),
    ('g10',   '1Hz_1hr',  3,  'Leica G10 (SKFC) 1Hz — 5,143 files since 2026-06-01; the 054 seed '
                              'gave g10 a daily row only. SKFC has no measured horizon for either '
                              'session, so this floor is load-bearing. Conservative 3; validate.')
ON CONFLICT (receiver_type, session_type) DO NOTHING;

INSERT INTO schema_migrations (migration_name)
VALUES ('074_seed_missing_receiver_buffer_depth')
ON CONFLICT DO NOTHING;

COMMIT;
