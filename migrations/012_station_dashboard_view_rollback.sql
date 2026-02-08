-- Rollback: 012_station_dashboard_view_rollback.sql
-- Description: Remove the station_dashboard_data view
-- Date: 2026-02-08
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/012_station_dashboard_view_rollback.sql

BEGIN;

DROP VIEW IF EXISTS station_dashboard_data;

COMMIT;
