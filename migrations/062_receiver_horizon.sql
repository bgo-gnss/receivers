-- Migration 062: receiver_horizon — the REAL per-station fetchable floor
--
-- Part of the Unified File Index (docs/architecture/unified-file-index-plan.md,
-- milestone M2, slice 2b.3). Replaces the conservative STATIC receiver_buffer_depth
-- floor in missing_on_receiver with the ACTUAL oldest file each station still holds
-- on its receiver, observed during the download listing (record_receiver_horizon).
--
-- DUAL-PURPOSE (see the ultrathink / plan §3): this horizon is BOTH
--   * the FETCH floor — below it the receiver has nothing, so a missing slot routes
--     to needs_repull (archive), never a receiver 404 churn; and
--   * the future RETENTION floor for prune — the local ring keeps 15s back to the
--     oldest daily file on the receiver (your rule), so the ring overlaps the
--     receiver's buffer. (Prune consumes this in a later slice.)
--
-- Per-(station, session) because retention/horizon differ by tier (15s vs 1Hz).
-- missing_on_receiver uses coalesce(horizon, current_date - depth_days): the real
-- horizon when known, the static seed as a safe fallback until it is probed.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/062_receiver_horizon.sql

BEGIN;

CREATE TABLE IF NOT EXISTS receiver_horizon (
    sid          VARCHAR(16) NOT NULL,
    session_type VARCHAR(32) NOT NULL,
    -- oldest file of this session still present on the receiver (its buffer floor).
    oldest_date  DATE        NOT NULL,
    -- oldest hour on that oldest date, for hourly sessions; NULL for daily.
    oldest_hour  SMALLINT,
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sid, session_type)
);

COMMENT ON TABLE receiver_horizon IS
    'Oldest file each station still holds on its receiver, per session, observed '
    'from the download listing. The real fetch floor for missing_on_receiver and '
    'the retention floor for prune (dual-purpose). Falls back to '
    'receiver_buffer_depth when a station has not been probed yet.';

-- Recreate missing_on_receiver with the horizon-with-fallback floor. No dependents.
DROP MATERIALIZED VIEW IF EXISTS missing_on_receiver;
CREATE MATERIALIZED VIEW missing_on_receiver AS
WITH station_session AS (
    SELECT s.sid,
           d.session_type,
           d.depth_days,
           s.data_start,
           s.data_end,
           rh.oldest_date AS horizon_date,
           (lower(d.session_type) LIKE '%1hr%') AS is_hourly
    FROM stations s
    JOIN receiver_buffer_depth d ON d.receiver_type = lower(s.receiver_type)
    LEFT JOIN receiver_horizon rh
           ON rh.sid = s.sid AND rh.session_type = d.session_type
    WHERE s.station_status IS NULL
      AND (s.health_check IS DISTINCT FROM 'passive')
      AND s.receiver_type IS NOT NULL
      AND s.data_start IS NOT NULL
),
bounded AS (
    -- the fetch floor: the real receiver horizon when probed, else the static seed
    SELECT ss.*,
           greatest(
               ss.data_start,
               coalesce(ss.horizon_date, current_date - ss.depth_days)
           ) AS floor_date
    FROM station_session ss
),
expected AS (
    SELECT b.sid, b.session_type, gd::date AS file_date, NULL::smallint AS file_hour
    FROM bounded b
    CROSS JOIN generate_series(
        b.floor_date::timestamp,
        least(coalesce(b.data_end, current_date - 1), current_date - 1)::timestamp,
        interval '1 day'
    ) AS gd
    WHERE NOT b.is_hourly

    UNION ALL

    SELECT b.sid, b.session_type, gd::date AS file_date, gh::smallint AS file_hour
    FROM bounded b
    CROSS JOIN generate_series(
        b.floor_date::timestamp,
        least(coalesce(b.data_end, current_date), current_date)::timestamp,
        interval '1 day'
    ) AS gd
    CROSS JOIN generate_series(0, 23) AS gh
    WHERE b.is_hourly
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

COMMENT ON MATERIALIZED VIEW missing_on_receiver IS
    'Expected raw slots minus present-root minus terminal-absent — the fetch-from-'
    'receiver worklist. Floor = the real receiver_horizon when probed, else the '
    'static receiver_buffer_depth seed. Ceiling = last fully-elapsed slot UTC. '
    'Materialized; refresh via refresh_missing_on_receiver().';

INSERT INTO schema_migrations (migration_name)
VALUES ('062_receiver_horizon')
ON CONFLICT DO NOTHING;

COMMIT;
