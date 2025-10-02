# Health Data Database Schema

PostgreSQL database schema for storing GPS receiver health data.

## Overview

The `checkcomm` table stores health data with backward compatibility to the legacy system while supporting new JSON-based metrics.

## Schema Definition

```sql
CREATE TABLE IF NOT EXISTS checkcomm (
    id SERIAL PRIMARY KEY,
    sid VARCHAR(4) NOT NULL,              -- Station ID (e.g., 'ELDC')
    timestamp TIMESTAMP NOT NULL,          -- Health measurement time

    -- Legacy columns (backward compatible)
    rout_stat JSONB,                       -- Router/network health
    recv_stat JSONB,                       -- Receiver connection health
    recv_temp FLOAT,                       -- Temperature (°C)
    recv_volt FLOAT,                       -- Voltage (V)

    -- New enhanced columns
    recv_metrics JSONB,                    -- Full health metrics JSON
    data_quality JSONB,                    -- Data quality metrics
    overall_status VARCHAR(20),            -- healthy, warning, critical, unknown

    -- Metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Constraints
    UNIQUE(sid, timestamp),
    CHECK (overall_status IN ('healthy', 'warning', 'critical', 'unknown'))
);

-- Indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_checkcomm_sid_time ON checkcomm(sid, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_checkcomm_status ON checkcomm(overall_status);
CREATE INDEX IF NOT EXISTS idx_checkcomm_created ON checkcomm(created_at DESC);

-- Index for JSONB queries
CREATE INDEX IF NOT EXISTS idx_checkcomm_metrics ON checkcomm USING GIN (recv_metrics);
CREATE INDEX IF NOT EXISTS idx_checkcomm_quality ON checkcomm USING GIN (data_quality);
```

## Column Details

### Primary Identification
- **id**: Auto-increment primary key
- **sid**: Station identifier (4-character code)
- **timestamp**: When health data was measured

### Legacy Columns
Maintained for backward compatibility with existing tools (gps2influx.py, Grafana dashboards):

- **rout_stat**: Router/network health status (JSONB)
- **recv_stat**: Receiver connection status (JSONB)
- **recv_temp**: Receiver temperature in Celsius (FLOAT)
- **recv_volt**: Receiver voltage in Volts (FLOAT)

### Enhanced Columns
New columns supporting full health monitoring:

- **recv_metrics**: Complete health metrics JSON following [health-data-spec.md](health-data-spec.md)
- **data_quality**: Data logging and quality metrics
- **overall_status**: Summary status for quick filtering

## Data Mapping

From health JSON to database:

```python
# From health-data-spec.md
health_json = {
    "station_id": "ELDC",
    "timestamp": "2025-10-02T12:00:00Z",
    "connection": {...},
    "metrics": {
        "power": {"voltage": 12.3, "status": "ok"},
        "temperature": {"value": 45.2, "status": "ok"},
        ...
    },
    "overall_status": "healthy"
}

# Maps to database
INSERT INTO checkcomm (
    sid,
    timestamp,
    recv_temp,          # From metrics.temperature.value
    recv_volt,          # From metrics.power.voltage
    rout_stat,          # From connection.router_ping
    recv_stat,          # From connection.protocol
    recv_metrics,       # Full metrics object
    data_quality,       # From data_quality object
    overall_status      # From overall_status
) VALUES (
    'ELDC',
    '2025-10-02 12:00:00',
    45.2,
    12.3,
    '{"status": "ok", "latency_ms": 5.2}',
    '{"status": "ok", "type": "ftp"}',
    '{"power": {"voltage": 12.3}, "temperature": {"value": 45.2}, ...}',
    '{"logging_status": "active", "sessions": {...}}',
    'healthy'
);
```

## Query Examples

### Recent Health for Station
```sql
SELECT
    timestamp,
    recv_temp,
    recv_volt,
    overall_status,
    recv_metrics->>'cpu_load' AS cpu_load,
    recv_metrics->'satellites'->>'tracking' AS satellites
FROM checkcomm
WHERE sid = 'ELDC'
    AND timestamp > NOW() - INTERVAL '24 hours'
ORDER BY timestamp DESC;
```

### Critical Status Summary
```sql
SELECT
    sid,
    COUNT(*) AS critical_count,
    MAX(timestamp) AS last_critical
FROM checkcomm
WHERE overall_status = 'critical'
    AND timestamp > NOW() - INTERVAL '7 days'
GROUP BY sid
ORDER BY critical_count DESC;
```

### Temperature Trends
```sql
SELECT
    DATE_TRUNC('hour', timestamp) AS hour,
    AVG(recv_temp) AS avg_temp,
    MAX(recv_temp) AS max_temp,
    MIN(recv_temp) AS min_temp
FROM checkcomm
WHERE sid = 'ELDC'
    AND timestamp > NOW() - INTERVAL '7 days'
GROUP BY hour
ORDER BY hour;
```

### Satellite Tracking Analysis
```sql
SELECT
    sid,
    AVG((recv_metrics->'satellites'->>'tracking')::int) AS avg_satellites,
    MIN((recv_metrics->'satellites'->>'tracking')::int) AS min_satellites
FROM checkcomm
WHERE timestamp > NOW() - INTERVAL '24 hours'
    AND recv_metrics->'satellites' IS NOT NULL
GROUP BY sid
HAVING AVG((recv_metrics->'satellites'->>'tracking')::int) < 8
ORDER BY avg_satellites;
```

## Migration from Legacy Schema

If updating existing checkcomm table:

```sql
-- Add new columns
ALTER TABLE checkcomm
    ADD COLUMN IF NOT EXISTS recv_metrics JSONB,
    ADD COLUMN IF NOT EXISTS data_quality JSONB,
    ADD COLUMN IF NOT EXISTS overall_status VARCHAR(20);

-- Add indexes
CREATE INDEX IF NOT EXISTS idx_checkcomm_metrics ON checkcomm USING GIN (recv_metrics);
CREATE INDEX IF NOT EXISTS idx_checkcomm_quality ON checkcomm USING GIN (data_quality);
CREATE INDEX IF NOT EXISTS idx_checkcomm_status ON checkcomm(overall_status);
```

## Retention Policy

```sql
-- Delete data older than 1 year (adjust as needed)
DELETE FROM checkcomm
WHERE timestamp < NOW() - INTERVAL '1 year';

-- Or partition by date for better performance
CREATE TABLE checkcomm_2025_10 PARTITION OF checkcomm
FOR VALUES FROM ('2025-10-01') TO ('2025-11-01');
```

---

**Status**: Development
**Last Updated**: 2025-10-02
