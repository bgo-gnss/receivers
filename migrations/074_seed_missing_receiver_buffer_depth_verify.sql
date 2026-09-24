-- Verify migration 074 — BEHAVIOURAL, not just "the rows exist".
--
-- Checking the two rows are present would pass even if they were wrong in the
-- ways that matter. Check 3 asserts the netr5 1Hz row is ABSENT (seeding it
-- would re-create the futile queueing this migration exists to stop), and
-- check 4 asserts every (receiver_type, session_type) the ACTIVE fleet actually
-- produces now has a floor — which is the property the migration is for.

\set ON_ERROR_STOP on

-- 1. the migration is recorded
SELECT CASE WHEN count(*) = 1 THEN 'PASS'
            ELSE 'FAIL: migration row missing' END AS migration_recorded
FROM schema_migrations WHERE migration_name = '074_seed_missing_receiver_buffer_depth';

-- 2. both intended rows exist with the intended depths
SELECT CASE WHEN count(*) = 3 THEN 'PASS'
            ELSE 'FAIL: expected 3 seeded rows, got ' || count(*) END AS rows_seeded
FROM receiver_buffer_depth
WHERE (receiver_type, session_type, depth_days)
   IN (('netrs', '1Hz_1hr', 3), ('netr5', '15s_24hr', 14), ('g10', '1Hz_1hr', 3));

-- 3. netr5 1Hz must NOT be seeded — neither AKUR nor ISAF produces 1Hz, and a
--    row would put them back into futile queueing.
SELECT CASE WHEN count(*) = 0 THEN 'PASS'
            ELSE 'FAIL: netr5/1Hz_1hr seeded — it must not be' END AS netr5_1hz_absent
FROM receiver_buffer_depth WHERE receiver_type = 'netr5' AND session_type = '1Hz_1hr';

-- 4. THE POINT: every (type, session) the active fleet actually produced in the
--    last 120 days now has a floor. Passive stations are excluded — they have no
--    receiver, so a buffer depth is meaningless for them.
SELECT CASE WHEN count(*) = 0 THEN 'PASS'
            ELSE 'FAIL: unfloored type/session still produced by the fleet: '
                 || string_agg(rtype || '/' || sess, ', ') END AS fleet_fully_floored
FROM (
    SELECT DISTINCT lower(s.receiver_type) AS rtype, c.session_type AS sess
      FROM stations s
      JOIN archive_catalog c
        ON c.station = s.sid
       AND c.storage_location = 'imo_archive'
       AND c.file_date > CURRENT_DATE - 120
       AND c.file_date <= CURRENT_DATE
     WHERE s.station_status IS NULL
       AND coalesce(s.health_check, '') <> 'passive'
       AND s.receiver_type IS NOT NULL AND s.receiver_type <> ''
       AND c.session_type IN ('15s_24hr', '1Hz_1hr')
) produced
WHERE NOT EXISTS (
    SELECT 1 FROM receiver_buffer_depth b
     WHERE lower(b.receiver_type) = produced.rtype
       AND b.session_type = produced.sess
);
