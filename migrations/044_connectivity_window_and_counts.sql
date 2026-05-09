-- Migration 044: Widen station_connectivity window to 2h; fix Online/Offline stat panels
--
-- Problem 1: station_connectivity uses a 1-hour look-back for latest_pings and
-- latest_ports. Stations whose most recent ping is 61+ minutes old fall out of
-- the view entirely and disappear from the dashboard counts (shows ~105 instead
-- of ~178 online stations).
--
-- Fix 1: Extend latest_pings and latest_ports time bounds from 01:00:00 to
-- 02:00:00. The debounce logic (rn <= 3) still uses only the three most recent
-- pings so debounce behaviour is unchanged. The ping_with_debounced CTE already
-- uses a 2-day window for state-duration tracking — no change needed there.
--
-- Problem 2: map Online/Offline stat panels exclude passive and inactive stations
-- (via NOT IN WHERE station_status IS NOT NULL OR health_check IS NOT NULL), so
-- Online + Offline ≠ Total − Discontinued.
--
-- Fix 2: Online = is_online = true, not discontinued.
--        Offline = all non-discontinued − online (includes passive, inactive,
--        and active stations with no recent ping).

BEGIN;

CREATE OR REPLACE VIEW station_connectivity AS
 WITH latest_pings AS (
         SELECT block_ping_status.sid,
            block_ping_status.ts,
            block_ping_status.is_online,
            block_ping_status.response_time_ms,
            block_ping_status.packet_loss,
            block_ping_status.error_message,
            row_number() OVER (PARTITION BY block_ping_status.sid ORDER BY block_ping_status.ts DESC) AS rn
           FROM block_ping_status
          WHERE block_ping_status.ts > (now() - '02:00:00'::interval)
        ), ping_debounced AS (
         SELECT latest_pings.sid,
            bool_or(latest_pings.is_online) FILTER (WHERE latest_pings.rn <= 3) AS ping_any_ok
           FROM latest_pings
          WHERE latest_pings.rn <= 3
          GROUP BY latest_pings.sid
        ), latest_ping AS (
         SELECT latest_pings.sid,
            latest_pings.ts,
            latest_pings.is_online,
            latest_pings.response_time_ms,
            latest_pings.packet_loss,
            latest_pings.error_message
           FROM latest_pings
          WHERE latest_pings.rn = 1
        ), latest_ports AS (
         SELECT block_port_status.sid,
            block_port_status.ts,
            block_port_status.download_status,
            row_number() OVER (PARTITION BY block_port_status.sid ORDER BY block_port_status.ts DESC) AS rn
           FROM block_port_status
          WHERE block_port_status.ts > (now() - '02:00:00'::interval)
        ), port_debounced AS (
         SELECT latest_ports.sid,
            bool_or(latest_ports.download_status::text = ANY (ARRAY['open'::character varying, 'ok'::character varying]::text[])) FILTER (WHERE latest_ports.rn <= 3) AS port_any_ok,
            bool_and(latest_ports.download_status::text = ANY (ARRAY['refused'::character varying, 'timeout'::character varying, 'unreachable'::character varying, 'critical'::character varying]::text[])) FILTER (WHERE latest_ports.rn <= 3) AS port_all_fail
           FROM latest_ports
          WHERE latest_ports.rn <= 3
          GROUP BY latest_ports.sid
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
        ), ping_with_debounced AS (
         SELECT block_ping_status.sid,
            block_ping_status.ts,
            block_ping_status.is_online,
            bool_or(block_ping_status.is_online) OVER (PARTITION BY block_ping_status.sid ORDER BY block_ping_status.ts ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS debounced_online
           FROM block_ping_status
          WHERE block_ping_status.ts > (now() - '2 days'::interval)
        ), debounced_state_changes AS (
         SELECT ping_with_debounced.sid,
            ping_with_debounced.ts,
            ping_with_debounced.debounced_online,
            lag(ping_with_debounced.debounced_online) OVER (PARTITION BY ping_with_debounced.sid ORDER BY ping_with_debounced.ts) AS prev_debounced
           FROM ping_with_debounced
        ), debounced_state_start AS (
         SELECT DISTINCT ON (debounced_state_changes.sid) debounced_state_changes.sid,
            debounced_state_changes.ts AS state_since
           FROM debounced_state_changes
          WHERE debounced_state_changes.debounced_online <> debounced_state_changes.prev_debounced OR debounced_state_changes.prev_debounced IS NULL
          ORDER BY debounced_state_changes.sid, debounced_state_changes.ts DESC
        )
 SELECT lp.sid,
    lp.ts AS last_check,
        CASE
            WHEN COALESCE(nt.ntrip_status, ''::character varying)::text = 'connected'::text THEN true
            WHEN COALESCE(prd.port_any_ok, false) THEN true
            WHEN COALESCE(prd.port_all_fail, false) THEN false
            WHEN prd.port_any_ok IS NULL AND COALESCE(pd.ping_any_ok, false) THEN true
            WHEN COALESCE(pd.ping_any_ok, false) THEN true
            ELSE false
        END AS is_online,
        CASE
            WHEN COALESCE(nt.ntrip_status, ''::character varying)::text = 'connected'::text THEN 'online'::text
            WHEN COALESCE(prd.port_any_ok, false) THEN 'online'::text
            WHEN COALESCE(prd.port_all_fail, false) AND COALESCE(pd.ping_any_ok, false) THEN 'degraded'::text
            WHEN COALESCE(prd.port_all_fail, false) THEN 'offline'::text
            WHEN prd.port_any_ok IS NULL AND COALESCE(pd.ping_any_ok, false) THEN 'online'::text
            WHEN COALESCE(pd.ping_any_ok, false) THEN 'online'::text
            ELSE 'offline'::text
        END AS connection_state,
        CASE
            WHEN COALESCE(nt.ntrip_status, ''::character varying)::text = 'connected'::text AND NOT COALESCE(prd.port_any_ok, false) AND NOT COALESCE(pd.ping_any_ok, false) THEN 'ntrip'::text
            WHEN COALESCE(prd.port_any_ok, false) THEN 'port'::text
            WHEN COALESCE(pd.ping_any_ok, false) THEN 'ping'::text
            ELSE NULL::text
        END AS reachability_source,
    lp.response_time_ms,
    lp.packet_loss,
    lp.error_message,
    COALESCE(dss.state_since, lp.ts) AS state_since,
    now() - COALESCE(dss.state_since, lp.ts) AS state_duration
   FROM latest_ping lp
     LEFT JOIN ping_debounced pd ON pd.sid::text = lp.sid::text
     LEFT JOIN port_debounced prd ON prd.sid::text = lp.sid::text
     LEFT JOIN latest_ntrip nt ON nt.sid::text = lp.sid::text
     LEFT JOIN debounced_state_start dss ON dss.sid::text = lp.sid::text;

INSERT INTO schema_migrations (migration_name) VALUES ('044_connectivity_window_and_counts') ON CONFLICT DO NOTHING;

COMMIT;
