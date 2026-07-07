-- Rollback 062: drop receiver_horizon, restore the mig-060 missing_on_receiver
-- (static receiver_buffer_depth floor, no horizon join).

BEGIN;

DROP MATERIALIZED VIEW IF EXISTS missing_on_receiver;
DROP TABLE IF EXISTS receiver_horizon;

CREATE MATERIALIZED VIEW missing_on_receiver AS
WITH station_session AS (
    SELECT s.sid,
           d.session_type,
           d.depth_days,
           s.data_start,
           s.data_end,
           (lower(d.session_type) LIKE '%1hr%') AS is_hourly
    FROM stations s
    JOIN receiver_buffer_depth d ON d.receiver_type = lower(s.receiver_type)
    WHERE s.station_status IS NULL
      AND (s.health_check IS DISTINCT FROM 'passive')
      AND s.receiver_type IS NOT NULL
      AND s.data_start IS NOT NULL
),
expected AS (
    SELECT ss.sid, ss.session_type, gd::date AS file_date, NULL::smallint AS file_hour
    FROM station_session ss
    CROSS JOIN generate_series(
        greatest(ss.data_start, current_date - ss.depth_days)::timestamp,
        least(coalesce(ss.data_end, current_date - 1), current_date - 1)::timestamp,
        interval '1 day'
    ) AS gd
    WHERE NOT ss.is_hourly

    UNION ALL

    SELECT ss.sid, ss.session_type, gd::date AS file_date, gh::smallint AS file_hour
    FROM station_session ss
    CROSS JOIN generate_series(
        greatest(ss.data_start, current_date - ss.depth_days)::timestamp,
        least(coalesce(ss.data_end, current_date), current_date)::timestamp,
        interval '1 day'
    ) AS gd
    CROSS JOIN generate_series(0, 23) AS gh
    WHERE ss.is_hourly
      AND (gd::date + make_interval(hours => gh) + interval '1 hour')
          <= (now() AT TIME ZONE 'UTC')
)
SELECT e.sid AS station, e.session_type, e.file_date, e.file_hour,
       COALESCE(e.file_hour, -1) AS obs_hour
FROM expected e
LEFT JOIN file_coverage fc
       ON fc.station = e.sid
      AND fc.session_type = e.session_type
      AND fc.file_date = e.file_date
      AND fc.file_hour IS NOT DISTINCT FROM e.file_hour
LEFT JOIN file_absence fa
       ON fa.source_location = 'receiver'
      AND fa.sid = e.sid
      AND fa.session_type = e.session_type
      AND fa.file_date = e.file_date
      AND fa.file_hour IS NOT DISTINCT FROM e.file_hour
      AND fa.terminal
WHERE (fc.station IS NULL OR NOT fc.root_any)
  AND fa.sid IS NULL
WITH DATA;

CREATE UNIQUE INDEX idx_missing_on_receiver_obs
    ON missing_on_receiver (station, session_type, file_date, obs_hour);
CREATE INDEX idx_missing_on_receiver_station
    ON missing_on_receiver (station, session_type);

DELETE FROM schema_migrations WHERE migration_name = '062_receiver_horizon';

COMMIT;
