-- Rollback migration 031: Remove reachability_source from station_connectivity
-- Restores views to their 030 state.
--
-- To rollback: psql -d gps_health -f migrations/031_reachability_source_rollback.sql

BEGIN;

-- Drop dependent views
DROP VIEW IF EXISTS station_dashboard_data;
DROP VIEW IF EXISTS station_data_flow_status;
DROP VIEW IF EXISTS station_connectivity;

-- Restore station_connectivity WITHOUT reachability_source
-- (copy from 029_dashboard_performance.sql)
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
    FROM latest_pings WHERE rn <= 3 GROUP BY sid
),
latest_ping AS (
    SELECT sid, ts, is_online, response_time_ms, packet_loss, error_message
    FROM latest_pings WHERE rn = 1
),
latest_ports AS (
    SELECT sid, ts, download_status,
           ROW_NUMBER() OVER (PARTITION BY sid ORDER BY ts DESC) AS rn
    FROM block_port_status WHERE ts > NOW() - INTERVAL '1 hour'
),
port_debounced AS (
    SELECT sid,
           BOOL_OR(download_status IN ('open','ok')) FILTER (WHERE rn <= 3) AS port_any_ok,
           BOOL_AND(download_status IN ('refused','timeout','unreachable','critical'))
               FILTER (WHERE rn <= 3) AS port_all_fail
    FROM latest_ports WHERE rn <= 3 GROUP BY sid
),
latest_ntrip AS (
    SELECT DISTINCT ON (sid) sid, status AS ntrip_status
    FROM (
        SELECT sid, ts, status FROM block_ntrip_server
        UNION ALL
        SELECT sid, ts, status FROM block_ntrip_client
    ) n WHERE ts > NOW() - INTERVAL '1 hour'
    ORDER BY sid, ts DESC
),
ping_with_debounced AS (
    SELECT sid, ts, is_online,
           BOOL_OR(is_online) OVER (
               PARTITION BY sid ORDER BY ts ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
           ) AS debounced_online
    FROM block_ping_status WHERE ts > NOW() - INTERVAL '2 days'
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
SELECT lp.sid, lp.ts AS last_check,
    CASE
        WHEN COALESCE(nt.ntrip_status,'')::text = 'connected' THEN true
        WHEN COALESCE(prd.port_any_ok, false) THEN true
        WHEN COALESCE(prd.port_all_fail, false) THEN false
        WHEN prd.port_any_ok IS NULL AND COALESCE(pd.ping_any_ok, false) THEN true
        WHEN COALESCE(pd.ping_any_ok, false) THEN true
        ELSE false
    END AS is_online,
    CASE
        WHEN COALESCE(nt.ntrip_status,'')::text = 'connected' THEN 'online'
        WHEN COALESCE(prd.port_any_ok, false) THEN 'online'
        WHEN COALESCE(prd.port_all_fail, false) AND COALESCE(pd.ping_any_ok, false) THEN 'degraded'
        WHEN COALESCE(prd.port_all_fail, false) THEN 'offline'
        WHEN prd.port_any_ok IS NULL AND COALESCE(pd.ping_any_ok, false) THEN 'online'
        WHEN COALESCE(pd.ping_any_ok, false) THEN 'online'
        ELSE 'offline'
    END AS connection_state,
    lp.response_time_ms, lp.packet_loss, lp.error_message,
    COALESCE(dss.state_since, lp.ts) AS state_since,
    NOW() - COALESCE(dss.state_since, lp.ts) AS state_duration
FROM latest_ping lp
LEFT JOIN ping_debounced pd ON pd.sid = lp.sid
LEFT JOIN port_debounced prd ON prd.sid = lp.sid
LEFT JOIN latest_ntrip nt ON nt.sid = lp.sid
LEFT JOIN debounced_state_start dss ON dss.sid = lp.sid;

-- NOTE: station_dashboard_data and station_data_flow_status must be
-- recreated from 030_view_decoupling.sql after this rollback.
-- Run: psql -d gps_health -f migrations/030_view_decoupling.sql

DELETE FROM schema_migrations WHERE migration_name = '031_reachability_source';

COMMIT;
