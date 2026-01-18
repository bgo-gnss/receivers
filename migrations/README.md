# Database Migrations

This directory contains SQL migrations for the GPS health database.

## Schema Overview

The database schema is **block-aligned** - each table maps to a Septentrio SBF block type:

```
stations                    # Station metadata
├── block_power_status      # SBF 4101 - PowerStatus
├── block_receiver_status   # SBF 4014 - ReceiverStatus2
├── block_disk_status       # SBF 4105 - DiskStatus
├── block_pvt_geodetic      # SBF 4007 - PVTGeodetic2
├── block_pos_covariance    # SBF 5905 - PosCovGeodetic1
├── block_sat_visibility    # SBF 4012 - SatVisibility1
├── block_ntrip_server      # SBF 4043 - NTRIPServerStatus
├── block_ntrip_client      # SBF 4053 - NTRIPClientStatus
├── block_wifi_status       # SBF 4051 - WiFiAPStatus
├── block_receiver_time     # SBF 5914 - ReceiverTime
├── block_receiver_setup    # SBF 5902 - ReceiverSetup1
├── agg_hourly              # Hourly aggregates
├── agg_daily               # Daily aggregates
└── checkcomm (VIEW)        # Backward compatibility
```

## Running Migrations

### Initial Setup

```bash
# Create database (if needed)
createdb -h localhost -U bgo gps_health

# Run migration
psql -h localhost -U bgo -d gps_health -f migrations/001_block_aligned_schema.sql
```

### Rollback

```bash
# WARNING: This deletes all data!
psql -h localhost -U bgo -d gps_health -f migrations/001_block_aligned_schema_rollback.sql
```

## Adding New Blocks

To add support for a new SBF block:

1. Create the table following the naming convention `block_<name>`:

```sql
CREATE TABLE block_new_block (
    sid VARCHAR(4) NOT NULL REFERENCES stations(sid) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    tow DOUBLE PRECISION,
    wnc INTEGER,
    -- block-specific fields here
    PRIMARY KEY (sid, ts)
);

COMMENT ON TABLE block_new_block IS 'SBF Block XXXX - NewBlockName';
```

2. Add appropriate indexes:

```sql
CREATE INDEX idx_new_block_sid_ts ON block_new_block(sid, ts DESC);
```

3. Update the extraction code in `receivers/health/` to populate the table.

## Backward Compatibility

The `checkcomm` view provides backward compatibility with existing code that
expects the old flat table structure. It joins data from multiple block tables.

## Aggregation

Hourly and daily aggregates are stored in `agg_hourly` and `agg_daily` tables.
Use the `compute_hourly_aggregate()` function to populate them:

```sql
SELECT compute_hourly_aggregate('ISFS', '2026-01-18 09:00:00+00');
```
