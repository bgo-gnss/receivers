-- Migration 031: Add reachability_source to station_connectivity
--
-- Problem: station_connectivity.is_online can be true from NTRIP alone,
-- which proves the receiver has internet but NOT that the download server
-- can reach its private 10.x IP.  Consumers (download code, dashboards)
-- need to know WHY a station is considered online.
--
-- Change: Add reachability_source column ('ping', 'port', 'ntrip', NULL)
-- to station_connectivity view.  No dependent view logic changes — just
-- an extra column.

BEGIN;

-- Drop dependent views first (they reference station_connectivity)
DROP VIEW IF EXISTS station_dashboard_data;
DROP VIEW IF EXISTS station_data_flow_status;
DROP VIEW IF EXISTS station_connectivity;

-- Recreate station_connectivity with reachability_source
CREATE VIEW station_connectivity AS
WITH latest_pings AS (
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_ping_status
    WHERE ts > NOW() - INTERVAL '1 hour'
),
ping_debounced AS (
    SELECT sid,
           BOOL_OR(is_online) FILTER (WHERE rn <= 3) AS ping_any_ok
    FROM latest_pings
    WHERE rn <= 3
    GROUP BY sid
),
latest_ping AS (
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message
    FROM latest_pings
    WHERE rn = 1
),
latest_ports AS (
    SELECT sid, ts, download_status,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_port_status
    WHERE ts > NOW() - INTERVAL '1 hour'
),
port_debounced AS (
    SELECT sid,
           BOOL_OR(download_status IN ('open', 'ok')) FILTER (WHERE rn <= 3) AS port_any_ok,
           BOOL_AND(download_status IN ('refused', 'timeout', 'unreachable', 'critical'))
               FILTER (WHERE rn <= 3) AS port_all_fail
    FROM latest_ports
    WHERE rn <= 3
    GROUP BY sid
),
latest_ntrip AS (
    SELECT DISTINCT ON (sid) sid, status AS ntrip_status
    FROM (
        SELECT sid, ts, status FROM block_ntrip_server
        UNION ALL
        SELECT sid, ts, status FROM block_ntrip_client
    ) ntrip_all
    WHERE ts > NOW() - INTERVAL '1 hour'
    ORDER BY sid, ts DESC
),
-- State duration tracking (unchanged from 029)
ping_with_debounced AS (
    SELECT sid, ts, is_online,
           BOOL_OR(is_online) OVER (
               PARTITION BY sid ORDER BY ts
               ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
           ) AS debounced_online
    FROM block_ping_status
    WHERE ts > NOW() - INTERVAL '2 days'
),
debounced_state_changes AS (
    SELECT sid, ts, debounced_online,
           LAG(debounced_online) OVER (PARTITION BY sid ORDER BY ts) AS prev_debounced
    FROM ping_with_debounced
),
debounced_state_start AS (
    SELECT DISTINCT ON (sid) sid, ts AS state_since
    FROM debounced_state_changes
    WHERE debounced_online <> prev_debounced OR prev_debounced IS NULL
    ORDER BY sid, ts DESC
)
SELECT
    lp.sid,
    lp.ts AS last_check,
    -- is_online: unchanged cascade logic
    CASE
        WHEN COALESCE(nt.ntrip_status, '')::text = 'connected' THEN true
        WHEN COALESCE(prd.port_any_ok, false)                  THEN true
        WHEN COALESCE(prd.port_all_fail, false)                 THEN false
        WHEN prd.port_any_ok IS NULL AND COALESCE(pd.ping_any_ok, false) THEN true
        WHEN COALESCE(pd.ping_any_ok, false)                    THEN true
        ELSE false
    END AS is_online,
    -- connection_state: unchanged
    CASE
        WHEN COALESCE(nt.ntrip_status, '')::text = 'connected'  THEN 'online'
        WHEN COALESCE(prd.port_any_ok, false)                    THEN 'online'
        WHEN COALESCE(prd.port_all_fail, false) AND COALESCE(pd.ping_any_ok, false) THEN 'degraded'
        WHEN COALESCE(prd.port_all_fail, false)                  THEN 'offline'
        WHEN prd.port_any_ok IS NULL AND COALESCE(pd.ping_any_ok, false) THEN 'online'
        WHEN COALESCE(pd.ping_any_ok, false)                     THEN 'online'
        ELSE 'offline'
    END AS connection_state,
    -- NEW: reachability_source — why is this station considered online?
    -- Priority matches the is_online cascade: ntrip > port > ping
    CASE
        WHEN COALESCE(nt.ntrip_status, '')::text = 'connected'
             AND NOT COALESCE(prd.port_any_ok, false)
             AND NOT COALESCE(pd.ping_any_ok, false)            THEN 'ntrip'
        WHEN COALESCE(prd.port_any_ok, false)                    THEN 'port'
        WHEN COALESCE(pd.ping_any_ok, false)                     THEN 'ping'
        ELSE NULL
    END AS reachability_source,
    lp.response_time_ms,
    lp.packet_loss,
    lp.error_message,
    COALESCE(dss.state_since, lp.ts) AS state_since,
    NOW() - COALESCE(dss.state_since, lp.ts) AS state_duration
FROM latest_ping lp
LEFT JOIN ping_debounced pd   ON pd.sid = lp.sid
LEFT JOIN port_debounced prd  ON prd.sid = lp.sid
LEFT JOIN latest_ntrip nt     ON nt.sid = lp.sid
LEFT JOIN debounced_state_start dss ON dss.sid = lp.sid;

-- Recreate station_dashboard_data (unchanged — just adding back)
CREATE VIEW station_dashboard_data AS
WITH health_ranked AS (
    SELECT sid, ts, overall_status, status_details,
           ftp_open, http_open, control_open,
           ftp_port, http_port, control_port,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_health_summary
    WHERE ts > NOW() - INTERVAL '1 day'
),
health_debounced_ports AS (
    SELECT sid,
           BOOL_OR(ftp_open)     FILTER (WHERE rn <= 3) AS ftp_open_db,
           BOOL_OR(http_open)    FILTER (WHERE rn <= 3) AS http_open_db,
           BOOL_OR(control_open) FILTER (WHERE rn <= 3) AS control_open_db
    FROM health_ranked
    WHERE rn <= 3
    GROUP BY sid
),
health_good AS (
    SELECT DISTINCT ON (h.sid)
           h.sid, h.overall_status, h.status_details,
           h.ftp_port, h.http_port, h.control_port
    FROM health_ranked h
    JOIN health_debounced_ports dp ON dp.sid = h.sid
    WHERE h.rn <= 3
      AND (NOT dp.control_open_db OR h.control_open)
      AND (NOT dp.ftp_open_db     OR h.ftp_open)
      AND (NOT dp.http_open_db    OR h.http_open)
    ORDER BY h.sid, h.ts DESC
),
latest_health AS (
    SELECT DISTINCT ON (r.sid)
           r.sid,
           COALESCE(good.overall_status, r.overall_status) AS overall_status,
           COALESCE(good.status_details, r.status_details) AS status_details,
           dp.ftp_open_db     AS ftp_open,
           dp.http_open_db    AS http_open,
           dp.control_open_db AS control_open,
           COALESCE(r.ftp_port,     good.ftp_port)     AS ftp_port,
           COALESCE(r.http_port,    good.http_port)     AS http_port,
           COALESCE(r.control_port, good.control_port)  AS control_port,
           r.ts AS health_ts
    FROM health_ranked r
    JOIN health_debounced_ports dp ON dp.sid = r.sid
    LEFT JOIN health_good good     ON good.sid = r.sid
    WHERE r.rn = 1
    ORDER BY r.sid
),
latest_ntrip AS (
    SELECT DISTINCT ON (sid) sid, status AS ntrip_status
    FROM (
        SELECT sid, ts, status FROM block_ntrip_server
        UNION ALL
        SELECT sid, ts, status FROM block_ntrip_client
    ) ntrip_all
    WHERE ts > NOW() - INTERVAL '1 hour'
    ORDER BY sid, ts DESC
),
latest_sat_breakdown AS (
    SELECT DISTINCT ON (sid) sid,
           gps AS gps_sats, glonass AS glonass_sats,
           galileo AS galileo_sats, beidou AS beidou_sats
    FROM block_satellite_tracking
    WHERE ts > NOW() - INTERVAL '1 day'
    ORDER BY sid, ts DESC
)
SELECT
    m.station_id,
    m.station_name,
    s.receiver_type,
    s.antenna_type,
    s.ip_address,
    s.power_type,
    s.http_port AS station_http_port,
    m.voltage, m.power_source, m.power_ts,
    m.cpu_load, m.temperature, m.uptime_seconds,
    m.rx_status, m.rx_error, m.receiver_ts,
    m.latitude AS metrics_latitude,
    m.longitude AS metrics_longitude,
    m.height AS metrics_height,
    m.satellites_used, m.h_accuracy, m.v_accuracy, m.position_ts,
    COALESCE(s.latitude, m.latitude)   AS latitude,
    COALESCE(s.longitude, m.longitude) AS longitude,
    m.satellites_tracked, m.sat_ts,
    lsb.gps_sats, lsb.glonass_sats, lsb.galileo_sats, lsb.beidou_sats,
    m.disk_usage_pct, m.free_space_mb, m.disk_ts,
    COALESCE(m.seconds_since_update,
             EXTRACT(EPOCH FROM NOW() - sc.last_check)::integer) AS seconds_since_update,
    COALESCE(m.last_update, sc.last_check) AS last_update,
    sc.is_online,
    sc.connection_state,
    sc.last_check,
    sc.state_since,
    sc.state_duration,
    sc.response_time_ms AS ping_response_ms,
    sc.packet_loss,
    sc.reachability_source,
    -- Voltage-aware overall_status override
    CASE
        WHEN lh.overall_status IS DISTINCT FROM 'critical' THEN lh.overall_status
        WHEN lh.status_details IS NULL OR lh.status_details NOT LIKE '%Voltage%' THEN lh.overall_status
        WHEN m.voltage IS NULL THEN lh.overall_status
        WHEN s.power_type = 'dcdc24' AND m.voltage >= 18.0 AND m.voltage <= 30.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage >= 20.0 AND m.voltage <= 28.0
                 THEN 'healthy' ELSE 'warning' END::varchar
        WHEN s.power_type = 'mains'  AND m.voltage >= 15.0 AND m.voltage <= 30.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage >= 18.0 AND m.voltage <= 28.0
                 THEN 'healthy' ELSE 'warning' END::varchar
        WHEN s.power_type = 'dcdc'   AND m.voltage >= 11.0 AND m.voltage <= 18.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage >= 12.0 AND m.voltage <= 16.5
                 THEN 'healthy' ELSE 'warning' END::varchar
        WHEN COALESCE(s.power_type, 'battery') = 'battery' AND m.voltage >= 11.0 AND m.voltage <= 16.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage >= 11.8 AND m.voltage <= 15.0
                 THEN 'healthy' ELSE 'warning' END::varchar
        ELSE lh.overall_status
    END AS overall_status,
    CASE
        WHEN lh.status_details = 'Voltage' AND lh.overall_status = 'critical'
             AND m.voltage IS NOT NULL
             AND (   (s.power_type = 'dcdc24'  AND m.voltage >= 20.0 AND m.voltage <= 28.0)
                  OR (s.power_type = 'mains'   AND m.voltage >= 18.0 AND m.voltage <= 28.0)
                  OR (s.power_type = 'dcdc'    AND m.voltage >= 12.0 AND m.voltage <= 16.5)
                  OR (COALESCE(s.power_type, 'battery') = 'battery'
                      AND m.voltage >= 11.8 AND m.voltage <= 15.0))
        THEN NULL
        ELSE lh.status_details
    END AS status_details,
    lh.ftp_open, lh.http_open, lh.control_open,
    lh.ftp_port, lh.http_port AS health_http_port, lh.control_port,
    ln.ntrip_status,
    sp.download_status,
    sp.health_status AS port_health_status,
    ds.avg_speed_bps, ds.completions, ds.stalls,
    ds.failures AS download_failures, ds.avg_stall_duration_s,
    ds.last_download_at, ds.last_stall_at,
    s.stall_timeout_override,
    -- Derived status columns
    CASE
        WHEN m.seconds_since_update IS NULL   THEN 'unknown'
        WHEN m.seconds_since_update > 3600    THEN 'offline'
        WHEN m.seconds_since_update > 300     THEN 'stale'
        ELSE 'online'
    END AS connection_status,
    CASE
        WHEN m.voltage IS NULL THEN 'unknown'
        WHEN s.power_type = 'dcdc24' THEN
            CASE WHEN m.voltage < 18.0 OR m.voltage > 30.0 THEN 'critical'
                 WHEN m.voltage < 20.0 OR m.voltage > 28.0 THEN 'warning' ELSE 'ok' END
        WHEN s.power_type = 'mains' THEN
            CASE WHEN m.voltage < 15.0 OR m.voltage > 30.0 THEN 'critical'
                 WHEN m.voltage < 18.0 OR m.voltage > 28.0 THEN 'warning' ELSE 'ok' END
        WHEN s.power_type = 'dcdc' THEN
            CASE WHEN m.voltage < 11.0 OR m.voltage > 18.0 THEN 'critical'
                 WHEN m.voltage < 12.0 OR m.voltage > 16.5 THEN 'warning' ELSE 'ok' END
        ELSE
            CASE WHEN m.voltage < 11.0 OR m.voltage > 16.0 THEN 'critical'
                 WHEN m.voltage < 11.8 OR m.voltage > 15.0 THEN 'warning' ELSE 'ok' END
    END AS voltage_status,
    CASE
        WHEN m.temperature IS NULL    THEN 'unknown'
        WHEN m.temperature > 60       THEN 'critical'
        WHEN m.temperature > 50       THEN 'warning'
        ELSE 'ok'
    END AS temperature_status,
    CASE
        WHEN m.cpu_load IS NULL       THEN 'unknown'
        WHEN m.cpu_load > 90          THEN 'critical'
        WHEN m.cpu_load > 75          THEN 'warning'
        ELSE 'ok'
    END AS cpu_status,
    CASE
        WHEN m.satellites_used IS NULL THEN 'unknown'
        WHEN m.satellites_used < 4     THEN 'critical'
        WHEN m.satellites_used < 8     THEN 'warning'
        ELSE 'ok'
    END AS satellite_status,
    s.station_status,
    s.health_check
FROM station_latest_metrics m
JOIN stations s ON s.sid = m.station_id
LEFT JOIN latest_health lh          ON lh.sid = m.station_id
LEFT JOIN latest_ntrip ln           ON ln.sid = m.station_id
LEFT JOIN latest_sat_breakdown lsb  ON lsb.sid = m.station_id
LEFT JOIN station_connectivity sc   ON sc.sid = m.station_id
LEFT JOIN station_port_status sp    ON sp.sid = m.station_id
LEFT JOIN station_download_summary ds ON ds.sid = m.station_id;

-- Recreate station_data_flow_status (unchanged — just adding back)
CREATE VIEW station_data_flow_status AS
WITH latest_raw_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking
    WHERE session_type = '15s_24hr'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_raw_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour::integer, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking
    WHERE session_type = '1Hz_1hr'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
latest_rinex_24h AS (
    SELECT DISTINCT ON (sid) sid, file_date
    FROM file_tracking
    WHERE session_type = '15s_24hr_rinex'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC
),
latest_rinex_1hz AS (
    SELECT DISTINCT ON (sid) sid,
           file_date + COALESCE(file_hour::integer, 0) * INTERVAL '1 hour' AS latest_ts
    FROM file_tracking
    WHERE session_type = '1Hz_1hr_rinex'
      AND status IN ('downloaded', 'archived')
    ORDER BY sid, file_date DESC, file_hour DESC NULLS LAST
),
health_streak AS (
    SELECT recent.sid,
           COALESCE(MIN(recent.rn) FILTER (WHERE recent.overall_status <> 'critical'), 7) - 1
               AS consecutive_critical
    FROM (
        SELECT sid, overall_status,
               ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
        FROM block_health_summary
        WHERE ts > NOW() - INTERVAL '1 day'
    ) recent
    WHERE recent.rn <= 6
    GROUP BY recent.sid
),
ever_checked AS (
    SELECT s.sid
    FROM stations s
    WHERE EXISTS (
        SELECT 1 FROM block_health_summary bhs
        WHERE bhs.sid = s.sid LIMIT 1
    )
),
flow_health AS (
    SELECT s.sid,
           h.overall_status, h.status_details,
           sc.is_online, sc.connection_state,
           s.station_status, s.health_check, s.receiver_type
    FROM stations s
    LEFT JOIN LATERAL (
        SELECT overall_status, status_details
        FROM block_health_summary
        WHERE sid = s.sid AND ts > NOW() - INTERVAL '1 day'
        ORDER BY ts DESC LIMIT 1
    ) h ON true
    LEFT JOIN station_connectivity sc ON sc.sid = s.sid
),
base AS (
    SELECT fh.sid,
        CASE
            WHEN fh.station_status IS NOT NULL OR fh.health_check IS NOT NULL THEN -2
            WHEN fh.is_online = false THEN -1
            WHEN fh.overall_status = 'healthy'  THEN 0
            WHEN fh.overall_status = 'warning'  THEN 1
            WHEN fh.overall_status = 'critical'
                 AND fh.connection_state = 'online'
                 AND (fh.status_details IS NULL
                      OR fh.status_details !~* '(Voltage|Temperature|Disk|Satellite)') THEN 1
            WHEN fh.overall_status = 'critical'
                 AND COALESCE(hs.consecutive_critical, 1) >= 2 THEN 2
            WHEN fh.overall_status = 'critical' THEN 1
            ELSE -1
        END AS health_status,
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN fh.receiver_type IS NULL
                 AND NOT COALESCE(l.session_15s_24hr, false)
                 AND r24.file_date IS NULL THEN -2
            WHEN ec.sid IS NULL AND r24.file_date IS NULL THEN -1
            WHEN r24.file_date IS NULL OR r24.file_date < CURRENT_DATE - 1 THEN 2
            WHEN x24.file_date IS NULL OR x24.file_date < r24.file_date   THEN 1
            ELSE 0
        END AS status_24h,
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN NOT COALESCE(l.session_1hz_1hr, false)
                 AND r1h.latest_ts IS NULL THEN -2
            WHEN ec.sid IS NULL AND r1h.latest_ts IS NULL THEN -1
            WHEN r1h.latest_ts >= NOW() - INTERVAL '1.5 hours' THEN 0
            WHEN r1h.latest_ts >= NOW() - INTERVAL '6 hours'   THEN 1
            WHEN r1h.latest_ts IS NOT NULL                      THEN 2
            ELSE 2
        END AS status_1hz,
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN fh.receiver_type IS NULL
                 AND NOT COALESCE(l.session_15s_24hr, false)
                 AND r24.file_date IS NULL THEN -2
            WHEN ec.sid IS NULL AND x24.file_date IS NULL AND r24.file_date IS NULL THEN -1
            WHEN x24.file_date >= CURRENT_DATE - 1 THEN 0
            WHEN x24.file_date IS NULL AND r24.file_date IS NOT NULL THEN 2
            WHEN x24.file_date IS NULL THEN -1
            WHEN EXTRACT(HOUR FROM NOW()) >= 12 THEN 2
            WHEN EXTRACT(HOUR FROM NOW()) >= 2  THEN 1
            ELSE 0
        END AS rinex_24h_status,
        CASE
            WHEN fh.station_status IS NOT NULL THEN -2
            WHEN NOT COALESCE(l.session_1hz_1hr, false)
                 AND r1h.latest_ts IS NULL THEN -2
            WHEN ec.sid IS NULL AND x1h.latest_ts IS NULL AND r1h.latest_ts IS NULL THEN -1
            WHEN x1h.latest_ts >= NOW() - INTERVAL '1.5 hours' THEN 0
            WHEN x1h.latest_ts IS NULL THEN -1
            WHEN x1h.latest_ts >= NOW() - INTERVAL '6 hours'   THEN 1
            ELSE 2
        END AS rinex_1hz_status,
        r24.file_date AS raw_24h_date,
        r1h.latest_ts AS raw_1hz_ts,
        x24.file_date AS rinex_24h_date,
        x1h.latest_ts AS rinex_1hz_ts,
        (fh.receiver_type IS NOT NULL
         OR COALESCE(l.session_15s_24hr, false)
         OR r24.file_date IS NOT NULL) AS logging_15s,
        (COALESCE(l.session_1hz_1hr, false)
         OR r1h.latest_ts IS NOT NULL) AS logging_1hz
    FROM flow_health fh
    LEFT JOIN station_logging_status l  ON l.sid = fh.sid
    LEFT JOIN health_streak hs          ON hs.sid = fh.sid
    LEFT JOIN ever_checked ec           ON ec.sid = fh.sid
    LEFT JOIN latest_raw_24h r24        ON r24.sid = fh.sid
    LEFT JOIN latest_raw_1hz r1h        ON r1h.sid = fh.sid
    LEFT JOIN latest_rinex_24h x24      ON x24.sid = fh.sid
    LEFT JOIN latest_rinex_1hz x1h      ON x1h.sid = fh.sid
)
SELECT sid,
    health_status, status_24h, status_1hz,
    rinex_24h_status, rinex_1hz_status,
    raw_24h_date, raw_1hz_ts,
    rinex_24h_date, rinex_1hz_ts,
    logging_15s, logging_1hz,
    CASE
        WHEN health_status < 0 AND status_24h < 0 THEN -1
        WHEN health_status = 2 AND status_24h = 2 THEN 2
        WHEN GREATEST(
            CASE WHEN health_status < 0 THEN 0 ELSE health_status END,
            CASE WHEN status_24h   < 0 THEN 0 ELSE status_24h   END
        ) = 2 THEN 1
        WHEN GREATEST(
            CASE WHEN health_status < 0 THEN 0 ELSE health_status END,
            CASE WHEN status_24h   < 0 THEN 0 ELSE status_24h   END
        ) = 1 THEN 1
        ELSE 0
    END AS combined_status
FROM base;

-- Record migration
INSERT INTO schema_migrations (migration_name)
VALUES ('031_reachability_source')
ON CONFLICT DO NOTHING;

COMMIT;
