-- Migration: 019_station_name.sql
-- Description: Add station_name column to stations table
-- Date: 2026-02-10
--
-- Stores the full station name (Icelandic place name) from stations.cfg.
-- Used in station detail dashboard alongside the 4-char SID.
--
-- Usage:
--   psql -h localhost -U bgo -d gps_health -f migrations/019_station_name.sql

BEGIN;

ALTER TABLE stations ADD COLUMN IF NOT EXISTS station_name VARCHAR(100);

COMMENT ON COLUMN stations.station_name IS 'Full station name (e.g., Icelandic place name) from stations.cfg';

COMMIT;
