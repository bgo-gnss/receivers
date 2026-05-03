-- Rollback for migration 036: Receiver model mismatch detection
BEGIN;

DROP VIEW IF EXISTS station_dashboard_data;

-- Recreate view without model_mismatch and identity columns
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
      AND (dp.control_open_db IS NOT TRUE OR h.control_open IS TRUE)
      AND (dp.ftp_open_db     IS NOT TRUE OR h.ftp_open     IS TRUE)
      AND (dp.http_open_db    IS NOT TRUE OR h.http_open    IS TRUE)
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
    m.station_id, m.station_name,
    s.receiver_type, s.antenna_type, s.ip_address, s.power_type,
    s.http_port AS station_http_port,
    m.voltage, m.power_source, m.power_ts,
    m.cpu_load, m.temperature, m.uptime_seconds,
    m.rx_status, m.rx_error, m.receiver_ts,
    m.latitude AS metrics_latitude, m.longitude AS metrics_longitude, m.height AS metrics_height,
    m.satellites_used, m.h_accuracy, m.v_accuracy, m.position_ts,
    COALESCE(s.latitude, m.latitude) AS latitude, COALESCE(s.longitude, m.longitude) AS longitude,
    m.satellites_tracked, m.sat_ts,
    lsb.gps_sats, lsb.glonass_sats, lsb.galileo_sats, lsb.beidou_sats,
    m.disk_usage_pct, m.free_space_mb, m.disk_ts,
    COALESCE(m.seconds_since_update, EXTRACT(EPOCH FROM NOW() - sc.last_check)::integer) AS seconds_since_update,
    COALESCE(m.last_update, sc.last_check) AS last_update,
    sc.is_online, sc.connection_state, sc.last_check, sc.state_since, sc.state_duration,
    sc.response_time_ms AS ping_response_ms, sc.packet_loss, sc.reachability_source,
    CASE
        WHEN lh.overall_status IS DISTINCT FROM 'critical' THEN lh.overall_status
        WHEN lh.status_details IS NULL OR lh.status_details NOT LIKE '%Voltage%' THEN lh.overall_status
        WHEN m.voltage IS NULL THEN lh.overall_status
        WHEN s.power_type = 'dcdc24' AND m.voltage >= 18.0 AND m.voltage <= 30.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage >= 20.0 AND m.voltage <= 28.0 THEN 'healthy' ELSE 'warning' END::varchar
        WHEN s.power_type = 'mains'  AND m.voltage >= 15.0 AND m.voltage <= 30.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage >= 18.0 AND m.voltage <= 28.0 THEN 'healthy' ELSE 'warning' END::varchar
        WHEN s.power_type = 'dcdc'   AND m.voltage >= 11.0 AND m.voltage <= 18.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage >= 12.0 AND m.voltage <= 16.5 THEN 'healthy' ELSE 'warning' END::varchar
        WHEN COALESCE(s.power_type, 'battery') = 'battery' AND m.voltage >= 11.0 AND m.voltage <= 16.0 THEN
            CASE WHEN lh.status_details = 'Voltage' AND m.voltage >= 11.8 AND m.voltage <= 15.0 THEN 'healthy' ELSE 'warning' END::varchar
        ELSE lh.overall_status
    END AS overall_status,
    CASE
        WHEN lh.status_details = 'Voltage' AND lh.overall_status = 'critical' AND m.voltage IS NOT NULL
             AND ((s.power_type = 'dcdc24' AND m.voltage >= 20.0 AND m.voltage <= 28.0)
                  OR (s.power_type = 'mains' AND m.voltage >= 18.0 AND m.voltage <= 28.0)
                  OR (s.power_type = 'dcdc' AND m.voltage >= 12.0 AND m.voltage <= 16.5)
                  OR (COALESCE(s.power_type, 'battery') = 'battery' AND m.voltage >= 11.8 AND m.voltage <= 15.0))
        THEN NULL ELSE lh.status_details
    END AS status_details,
    lh.ftp_open, lh.http_open, lh.control_open,
    lh.ftp_port, lh.http_port AS health_http_port, lh.control_port,
    ln.ntrip_status,
    sp.download_status, sp.health_status AS port_health_status,
    ds.avg_speed_bps, ds.completions, ds.stalls,
    ds.failures AS download_failures, ds.avg_stall_duration_s,
    ds.last_download_at, ds.last_stall_at,
    s.stall_timeout_override,
    CASE WHEN m.seconds_since_update IS NULL THEN 'unknown' WHEN m.seconds_since_update > 3600 THEN 'offline' WHEN m.seconds_since_update > 300 THEN 'stale' ELSE 'online' END AS connection_status,
    CASE WHEN m.voltage IS NULL THEN 'unknown'
         WHEN s.power_type = 'dcdc24' THEN CASE WHEN m.voltage < 18.0 OR m.voltage > 30.0 THEN 'critical' WHEN m.voltage < 20.0 OR m.voltage > 28.0 THEN 'warning' ELSE 'ok' END
         WHEN s.power_type = 'mains'  THEN CASE WHEN m.voltage < 15.0 OR m.voltage > 30.0 THEN 'critical' WHEN m.voltage < 18.0 OR m.voltage > 28.0 THEN 'warning' ELSE 'ok' END
         WHEN s.power_type = 'dcdc'   THEN CASE WHEN m.voltage < 11.0 OR m.voltage > 18.0 THEN 'critical' WHEN m.voltage < 12.0 OR m.voltage > 16.5 THEN 'warning' ELSE 'ok' END
         ELSE CASE WHEN m.voltage < 11.0 OR m.voltage > 16.0 THEN 'critical' WHEN m.voltage < 11.8 OR m.voltage > 15.0 THEN 'warning' ELSE 'ok' END
    END AS voltage_status,
    CASE WHEN m.temperature IS NULL THEN 'unknown' WHEN m.temperature > 60 THEN 'critical' WHEN m.temperature > 50 THEN 'warning' ELSE 'ok' END AS temperature_status,
    CASE WHEN m.cpu_load IS NULL THEN 'unknown' WHEN m.cpu_load > 90 THEN 'critical' WHEN m.cpu_load > 75 THEN 'warning' ELSE 'ok' END AS cpu_status,
    CASE WHEN COALESCE(m.satellites_used, m.satellites_tracked) IS NULL THEN 'unknown'
         WHEN COALESCE(m.satellites_used, m.satellites_tracked) < 4 THEN 'critical'
         WHEN COALESCE(m.satellites_used, m.satellites_tracked) < 8 THEN 'warning'
         ELSE 'ok' END AS satellite_status,
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

ALTER TABLE stations DROP COLUMN IF EXISTS model_mismatch;

DELETE FROM schema_migrations WHERE migration_name = '036_sn_mismatch_detection';

COMMIT;
