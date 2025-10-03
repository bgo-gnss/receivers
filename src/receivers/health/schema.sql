-- GPS Receiver Health Monitoring Schema
-- checkcomm table for storing receiver health data

CREATE TABLE IF NOT EXISTS checkcomm (
    id SERIAL PRIMARY KEY,
    sid VARCHAR(4) NOT NULL,              -- Station ID
    timestamp TIMESTAMP NOT NULL,          -- Health check timestamp

    -- Legacy columns (backward compatibility)
    recv_temp FLOAT,                       -- Temperature (°C)
    recv_volt FLOAT,                       -- Voltage (V)

    -- New JSONB columns for comprehensive health data
    rout_stat JSONB,                       -- Router/network health (ping data)
    recv_stat JSONB,                       -- Receiver connection health (HTTP + protocol)
    recv_metrics JSONB,                    -- Full health metrics (power, temp, CPU, disk, etc.)
    data_quality JSONB,                    -- Data quality metrics (logging, tracking, sessions)

    -- Overall status
    overall_status VARCHAR(20),            -- healthy, warning, critical, unknown

    -- Constraints
    UNIQUE(sid, timestamp)
);

-- Index for efficient queries
CREATE INDEX IF NOT EXISTS idx_checkcomm_sid_timestamp ON checkcomm(sid, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_checkcomm_timestamp ON checkcomm(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_checkcomm_overall_status ON checkcomm(overall_status);

-- Example queries:

-- Get latest health status for all stations
-- SELECT DISTINCT ON (sid) sid, timestamp, overall_status, recv_temp, recv_volt
-- FROM checkcomm
-- ORDER BY sid, timestamp DESC;

-- Get health history for specific station
-- SELECT timestamp, overall_status, recv_temp, recv_volt, recv_metrics
-- FROM checkcomm
-- WHERE sid = 'ELDC'
-- ORDER BY timestamp DESC
-- LIMIT 24;

-- Find stations with critical status
-- SELECT sid, timestamp, overall_status, recv_metrics->'power'->>'voltage' as voltage
-- FROM checkcomm
-- WHERE overall_status = 'critical'
-- ORDER BY timestamp DESC;

-- Get connection health details
-- SELECT sid, timestamp,
--        rout_stat->>'status' as ping_status,
--        recv_stat->'http_port'->>'status' as http_status,
--        recv_stat->'protocol'->>'status' as protocol_status
-- FROM checkcomm
-- WHERE timestamp > NOW() - INTERVAL '1 hour'
-- ORDER BY timestamp DESC;
