-- Rollback migration 030: Restore previous view definitions
-- Run 029_dashboard_performance.sql after this to restore the pre-030 state

BEGIN;

ALTER DATABASE gps_health RESET jit;

-- Restore station_logging_status (unbounded DISTINCT ON)
CREATE OR REPLACE VIEW station_logging_status AS
SELECT DISTINCT ON (sid)
       sid,
       ts AS last_check,
       active_sessions,
       session_15s_24hr,
       session_1hz_1hr,
       session_status_1hr,
       status
  FROM block_logging_status
 ORDER BY sid, ts DESC;

-- To fully rollback station_dashboard_data and station_data_flow_status,
-- re-apply migration 029_dashboard_performance.sql which contains the
-- previous definitions of both views.

DELETE FROM schema_migrations WHERE migration_name = '030_view_decoupling';

COMMIT;
