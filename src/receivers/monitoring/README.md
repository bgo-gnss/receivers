# GPS Receiver Monitoring Integration

Icinga/Nagios plugin for GPS receiver health monitoring.

## Quick Start

```bash
# Basic health check
./check_gps_receiver.py --station ELDC

# With database storage
./check_gps_receiver.py --station ELDC --save-db

# With JSON file storage
./check_gps_receiver.py --station ELDC --save-json
```

## Icinga2 Configuration

### Command Definition

```
object CheckCommand "check_gps_receiver" {
  command = [ "/usr/local/bin/check_gps_receiver.py" ]

  arguments = {
    "--station" = {
      value = "$gps_station$"
      required = true
    }
    "--save-db" = {
      set_if = "$gps_save_db$"
    }
    "--save-json" = {
      set_if = "$gps_save_json$"
    }
    "--timeout" = "$gps_timeout$"
  }

  timeout = 60
}
```

### Service Definition

```
apply Service "gps-receiver-health" {
  import "generic-service"
  check_command = "check_gps_receiver"

  vars.gps_station = host.vars.station_id
  vars.gps_save_db = true
  vars.gps_save_json = true

  assign where host.vars.receiver_type
}
```

### Host Example

```
object Host "gps-eldc" {
  import "generic-host"
  address = "eldc.vedur.is"

  vars.station_id = "ELDC"
  vars.receiver_type = "PolaRX5"

  check_command = "hostalive"
}
```

## Output Format

### Nagios Plugin Format

```
STATUS_MESSAGE | PERFORMANCE_DATA
```

Example outputs:
```
ELDC OK - All checks OK | voltage=12.3V;11.5;11.0;10;15 temperature=45.2C;60;70;0;100 ping_latency=5.2ms;100;500;0;1000
```

```
THOB WARNING - router_ping:warning, temp:65.2C | voltage=12.1V temperature=65.2C ping_latency=120.5ms
```

```
SKFC CRITICAL - protocol:critical | voltage=10.8V;11.5;11.0;10;15 temperature=48.3C
```

### Exit Codes

- **0 (OK)**: All health checks passed
- **1 (WARNING)**: Some checks in warning state
- **2 (CRITICAL)**: Critical issues detected
- **3 (UNKNOWN)**: Unable to determine health status

## Performance Data

Performance metrics with thresholds:

| Metric | Unit | Warning | Critical | Min | Max |
|--------|------|---------|----------|-----|-----|
| voltage | V | <11.5 | <11.0 | 10 | 15 |
| temperature | °C | >60 | >70 | 0 | 100 |
| cpu_load | % | >75 | >90 | 0 | 100 |
| disk_usage | % | >80 | >90 | 0 | 100 |
| ping_latency | ms | >100 | >500 | 0 | 1000 |

## Grafana Integration

Query PostgreSQL checkcomm table for visualization:

```sql
-- Latest health status for all stations
SELECT
    sid,
    timestamp,
    overall_status,
    recv_temp,
    recv_volt,
    recv_metrics->'cpu_load'->>'percent' as cpu_load
FROM checkcomm
WHERE timestamp > NOW() - INTERVAL '24 hours'
ORDER BY timestamp DESC;
```

```sql
-- Station health history
SELECT
    timestamp,
    overall_status,
    recv_volt as voltage,
    recv_temp as temperature,
    rout_stat->>'latency_ms' as ping_latency
FROM checkcomm
WHERE sid = 'ELDC'
  AND timestamp > NOW() - INTERVAL '7 days'
ORDER BY timestamp;
```

## Database Storage

When using `--save-db`, health data is automatically stored in PostgreSQL `checkcomm` table.

Environment variables for database connection:
```bash
export POSTGRES_HOST=localhost
export POSTGRES_PORT=5432
export POSTGRES_DB=gps
export POSTGRES_USER=gpsuser
export POSTGRES_PASSWORD=secret
```

## JSON File Storage

When using `--save-json`, health data is saved to:
```
/data/YYYY/mmm/STATION/status_1hr/json/STATION_YYYYMMDD_HHMMSS.json
```

Latest symlink:
```
/data/YYYY/mmm/STATION/status_1hr/json/latest.json
```

## Troubleshooting

Test plugin manually:
```bash
# Run and check exit code
./check_gps_receiver.py --station ELDC
echo $?

# Verbose mode (via receivers health command)
receivers health ELDC --verbose

# Check database connectivity
python3 -c "import psycopg2; conn = psycopg2.connect('host=localhost dbname=gps'); print('OK')"
```

## Requirements

- Python 3.8+
- psycopg2 (for database storage)
- Configured station in stations.cfg
- Network access to receiver
