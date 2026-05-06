-- Migration 042: Extend voltage status correction to stored 'warning' states
--
-- Problem: station_dashboard_data corrects overall_status when the stored health
-- record shows a voltage-critical that the current voltage no longer justifies.
-- However, the same correction was NOT applied to voltage-warning states.
--
-- Root cause: the overall_status CASE started with:
--   WHEN lh.overall_status IS DISTINCT FROM 'critical' THEN lh.overall_status
-- This immediately returned any 'warning' state unchanged, bypassing all the
-- power-type-aware voltage correction logic below it.
--
-- Effect: FAGC (dcdc, 15.07 V) kept showing WARNING in the dashboard even though
-- 15.07 V is well within the dcdc normal range (12.0–16.5 V).  The view computed
-- voltage_status = 'ok' correctly, but overall_status was stuck at 'warning'
-- because the extractor stored a stale warning (using battery thresholds).
--
-- Fix: change the guard to NOT IN ('critical', 'warning') so that non-voltage
-- warnings and healthy states still pass through unchanged, but voltage-related
-- 'warning' states go through the same power-type correction as 'critical' ones.
--
-- Non-voltage warnings (e.g. ping packet-loss) are unaffected: the second
-- condition (status_details !~~ '%Voltage%') passes them through immediately.

BEGIN;

CREATE OR REPLACE VIEW station_dashboard_data AS
 WITH health_ranked AS (
         SELECT block_health_summary.sid,
            block_health_summary.ts,
            block_health_summary.overall_status,
            block_health_summary.status_details,
            block_health_summary.ftp_open,
            block_health_summary.http_open,
            block_health_summary.control_open,
            block_health_summary.ftp_port,
            block_health_summary.http_port,
            block_health_summary.control_port,
            row_number() OVER (PARTITION BY block_health_summary.sid ORDER BY block_health_summary.ts DESC) AS rn
           FROM block_health_summary
          WHERE block_health_summary.ts > (now() - '1 day'::interval)
        ), health_debounced_ports AS (
         SELECT health_ranked.sid,
            bool_or(health_ranked.ftp_open) FILTER (WHERE health_ranked.rn <= 3) AS ftp_open_db,
            bool_or(health_ranked.http_open) FILTER (WHERE health_ranked.rn <= 3) AS http_open_db,
            bool_or(health_ranked.control_open) FILTER (WHERE health_ranked.rn <= 3) AS control_open_db
           FROM health_ranked
          WHERE health_ranked.rn <= 3
          GROUP BY health_ranked.sid
        ), health_good AS (
         SELECT DISTINCT ON (h.sid) h.sid,
            h.overall_status,
            h.status_details,
            h.ftp_port,
            h.http_port,
            h.control_port
           FROM health_ranked h
             JOIN health_debounced_ports dp ON dp.sid::text = h.sid::text
          WHERE h.rn <= 3 AND (dp.control_open_db IS NOT TRUE OR h.control_open IS TRUE) AND (dp.ftp_open_db IS NOT TRUE OR h.ftp_open IS TRUE) AND (dp.http_open_db IS NOT TRUE OR h.http_open IS TRUE)
          ORDER BY h.sid, h.ts DESC
        ), latest_health AS (
         SELECT DISTINCT ON (r.sid) r.sid,
            COALESCE(good.overall_status, r.overall_status) AS overall_status,
            COALESCE(good.status_details, r.status_details) AS status_details,
            dp.ftp_open_db AS ftp_open,
            dp.http_open_db AS http_open,
            dp.control_open_db AS control_open,
            COALESCE(r.ftp_port, good.ftp_port) AS ftp_port,
            COALESCE(r.http_port, good.http_port) AS http_port,
            COALESCE(r.control_port, good.control_port) AS control_port,
            r.ts AS health_ts
           FROM health_ranked r
             JOIN health_debounced_ports dp ON dp.sid::text = r.sid::text
             LEFT JOIN health_good good ON good.sid::text = r.sid::text
          WHERE r.rn = 1
          ORDER BY r.sid
        ), latest_ntrip AS (
         SELECT DISTINCT ON (ntrip_all.sid) ntrip_all.sid,
            ntrip_all.status AS ntrip_status
           FROM ( SELECT block_ntrip_server.sid,
                    block_ntrip_server.ts,
                    block_ntrip_server.status
                   FROM block_ntrip_server
                UNION ALL
                 SELECT block_ntrip_client.sid,
                    block_ntrip_client.ts,
                    block_ntrip_client.status
                   FROM block_ntrip_client) ntrip_all
          WHERE ntrip_all.ts > (now() - '01:00:00'::interval)
          ORDER BY ntrip_all.sid, ntrip_all.ts DESC
        ), latest_sat_breakdown AS (
         SELECT DISTINCT ON (block_satellite_tracking.sid) block_satellite_tracking.sid,
            block_satellite_tracking.gps AS gps_sats,
            block_satellite_tracking.glonass AS glonass_sats,
            block_satellite_tracking.galileo AS galileo_sats,
            block_satellite_tracking.beidou AS beidou_sats
           FROM block_satellite_tracking
          WHERE block_satellite_tracking.ts > (now() - '1 day'::interval)
          ORDER BY block_satellite_tracking.sid, block_satellite_tracking.ts DESC
        )
 SELECT m.station_id,
    m.station_name,
    s.receiver_type,
    s.antenna_type,
    s.ip_address,
    s.power_type,
    s.http_port AS station_http_port,
    m.voltage,
    m.power_source,
    m.power_ts,
    m.cpu_load,
    m.temperature,
    m.uptime_seconds,
    m.rx_status,
    m.rx_error,
    m.receiver_ts,
    m.latitude AS metrics_latitude,
    m.longitude AS metrics_longitude,
    m.height AS metrics_height,
    m.satellites_used,
    m.h_accuracy,
    m.v_accuracy,
    m.position_ts,
    COALESCE(s.latitude, m.latitude) AS latitude,
    COALESCE(s.longitude, m.longitude) AS longitude,
    m.satellites_tracked,
    m.sat_ts,
    lsb.gps_sats,
    lsb.glonass_sats,
    lsb.galileo_sats,
    lsb.beidou_sats,
    m.disk_usage_pct,
    m.free_space_mb,
    m.disk_ts,
    COALESCE(m.seconds_since_update, EXTRACT(epoch FROM now() - sc.last_check)::integer) AS seconds_since_update,
    COALESCE(m.last_update, sc.last_check) AS last_update,
    sc.is_online,
    sc.connection_state,
    sc.last_check,
    sc.state_since,
    sc.state_duration,
    sc.response_time_ms AS ping_response_ms,
    sc.packet_loss,
    sc.reachability_source,
        CASE
            -- Pass through any status that is not a correctable voltage-driven alert
            WHEN lh.overall_status::text NOT IN ('critical'::text, 'warning'::text) THEN lh.overall_status
            WHEN lh.status_details IS NULL OR lh.status_details !~~ '%Voltage%'::text THEN lh.overall_status
            WHEN m.voltage IS NULL THEN lh.overall_status
            WHEN s.power_type::text = 'dcdc24'::text AND m.voltage >= 18.0::double precision AND m.voltage <= 30.0::double precision THEN
            CASE
                WHEN lh.status_details = 'Voltage'::text AND m.voltage >= 20.0::double precision AND m.voltage <= 28.0::double precision THEN 'healthy'::text
                ELSE 'warning'::text
            END::character varying
            WHEN s.power_type::text = 'mains'::text AND m.voltage >= 15.0::double precision AND m.voltage <= 30.0::double precision THEN
            CASE
                WHEN lh.status_details = 'Voltage'::text AND m.voltage >= 18.0::double precision AND m.voltage <= 28.0::double precision THEN 'healthy'::text
                ELSE 'warning'::text
            END::character varying
            WHEN s.power_type::text = 'dcdc'::text AND m.voltage >= 11.0::double precision AND m.voltage <= 18.0::double precision THEN
            CASE
                WHEN lh.status_details = 'Voltage'::text AND m.voltage >= 12.0::double precision AND m.voltage <= 16.5::double precision THEN 'healthy'::text
                ELSE 'warning'::text
            END::character varying
            WHEN COALESCE(s.power_type, 'battery'::character varying)::text = 'battery'::text AND m.voltage >= 11.0::double precision AND m.voltage <= 16.0::double precision THEN
            CASE
                WHEN lh.status_details = 'Voltage'::text AND m.voltage >= 11.8::double precision AND m.voltage <= 15.0::double precision THEN 'healthy'::text
                ELSE 'warning'::text
            END::character varying
            ELSE lh.overall_status
        END AS overall_status,
        CASE
            WHEN lh.status_details = 'Voltage'::text AND lh.overall_status::text = 'critical'::text AND m.voltage IS NOT NULL AND (s.power_type::text = 'dcdc24'::text AND m.voltage >= 20.0::double precision AND m.voltage <= 28.0::double precision OR s.power_type::text = 'mains'::text AND m.voltage >= 18.0::double precision AND m.voltage <= 28.0::double precision OR s.power_type::text = 'dcdc'::text AND m.voltage >= 12.0::double precision AND m.voltage <= 16.5::double precision OR COALESCE(s.power_type, 'battery'::character varying)::text = 'battery'::text AND m.voltage >= 11.8::double precision AND m.voltage <= 15.0::double precision) THEN NULL::text
            ELSE lh.status_details
        END AS status_details,
    lh.ftp_open,
    lh.http_open,
    lh.control_open,
    lh.ftp_port,
    lh.http_port AS health_http_port,
    lh.control_port,
    ln.ntrip_status,
    sp.download_status,
    sp.health_status AS port_health_status,
    ds.avg_speed_bps,
    ds.completions,
    ds.stalls,
    ds.failures AS download_failures,
    ds.avg_stall_duration_s,
    ds.last_download_at,
    ds.last_stall_at,
    s.stall_timeout_override,
        CASE
            WHEN m.seconds_since_update IS NULL THEN 'unknown'::text
            WHEN m.seconds_since_update > 3600 THEN 'offline'::text
            WHEN m.seconds_since_update > 300 THEN 'stale'::text
            ELSE 'online'::text
        END AS connection_status,
        CASE
            WHEN m.voltage IS NULL THEN 'unknown'::text
            WHEN s.power_type::text = 'dcdc24'::text THEN
            CASE
                WHEN m.voltage < 18.0::double precision OR m.voltage > 30.0::double precision THEN 'critical'::text
                WHEN m.voltage < 20.0::double precision OR m.voltage > 28.0::double precision THEN 'warning'::text
                ELSE 'ok'::text
            END
            WHEN s.power_type::text = 'mains'::text THEN
            CASE
                WHEN m.voltage < 15.0::double precision OR m.voltage > 30.0::double precision THEN 'critical'::text
                WHEN m.voltage < 18.0::double precision OR m.voltage > 28.0::double precision THEN 'warning'::text
                ELSE 'ok'::text
            END
            WHEN s.power_type::text = 'dcdc'::text THEN
            CASE
                WHEN m.voltage < 11.0::double precision OR m.voltage > 18.0::double precision THEN 'critical'::text
                WHEN m.voltage < 12.0::double precision OR m.voltage > 16.5::double precision THEN 'warning'::text
                ELSE 'ok'::text
            END
            ELSE
            CASE
                WHEN m.voltage < 11.0::double precision OR m.voltage > 16.0::double precision THEN 'critical'::text
                WHEN m.voltage < 11.8::double precision OR m.voltage > 15.0::double precision THEN 'warning'::text
                ELSE 'ok'::text
            END
        END AS voltage_status,
        CASE
            WHEN m.temperature IS NULL THEN 'unknown'::text
            WHEN m.temperature > 60::double precision THEN 'critical'::text
            WHEN m.temperature > 50::double precision THEN 'warning'::text
            ELSE 'ok'::text
        END AS temperature_status,
        CASE
            WHEN m.cpu_load IS NULL THEN 'unknown'::text
            WHEN m.cpu_load > 90::double precision THEN 'critical'::text
            WHEN m.cpu_load > 75::double precision THEN 'warning'::text
            ELSE 'ok'::text
        END AS cpu_status,
        CASE
            WHEN COALESCE(m.satellites_used::bigint, m.satellites_tracked) IS NULL THEN 'unknown'::text
            WHEN COALESCE(m.satellites_used::bigint, m.satellites_tracked) < 4 THEN 'critical'::text
            WHEN COALESCE(m.satellites_used::bigint, m.satellites_tracked) < 8 THEN 'warning'::text
            ELSE 'ok'::text
        END AS satellite_status,
    s.station_status,
    s.health_check,
    s.serial_number,
    s.firmware_version,
    s.detected_model,
    s.identity_last_checked,
    s.model_mismatch,
    s.configured_serial,
    s.configured_firmware
   FROM station_latest_metrics m
     JOIN stations s ON s.sid::text = m.station_id::text
     LEFT JOIN latest_health lh ON lh.sid::text = m.station_id::text
     LEFT JOIN latest_ntrip ln ON ln.sid::text = m.station_id::text
     LEFT JOIN latest_sat_breakdown lsb ON lsb.sid::text = m.station_id::text
     LEFT JOIN station_connectivity sc ON sc.sid::text = m.station_id::text
     LEFT JOIN station_port_status sp ON sp.sid::text = m.station_id::text
     LEFT JOIN station_download_summary ds ON ds.sid::text = m.station_id::text;

INSERT INTO schema_migrations (migration_name) VALUES ('042_voltage_warning_correction');

COMMIT;
