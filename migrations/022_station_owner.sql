-- Migration 022: Station Owner
-- Adds station_owner column to stations table for tracking which agency/organization
-- owns/operates each station. Populated from stations.cfg station_owner field,
-- falling back to rinex_agency for non-IMO stations.

ALTER TABLE stations ADD COLUMN IF NOT EXISTS station_owner VARCHAR(60);

COMMENT ON COLUMN stations.station_owner IS 'Organization that owns/operates the station (e.g., IMO, NATT, UI). Synced from stations.cfg.';

-- Create index for filtering by owner in dashboards
CREATE INDEX IF NOT EXISTS idx_stations_owner ON stations(station_owner);
