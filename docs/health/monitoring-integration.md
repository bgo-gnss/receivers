# Monitoring System Integration

Integration of GPS receiver health monitoring with Icinga and Grafana.

## Overview

Health data flows from receivers → PostgreSQL → Monitoring systems:

```
GPS Receivers → Health Extraction → PostgreSQL (checkcomm)
                                         ↓
                                    ┌────┴────┐
                                    │         │
                                Icinga    Grafana
                                (Alerts) (Dashboards)
```

## Icinga/Nagios Integration

### Nagios Plugin Format

The `receivers health` command can output in Nagios plugin format:

```bash
receivers health ELDC --format icinga --warn-temp 60 --crit-temp 70

# Exit codes:
# 0 = OK
# 1 = WARNING
# 2 = CRITICAL
# 3 = UNKNOWN

# Output format:
# STATUS: message | performance_data
```

### Example Check Command

```bash
#!/bin/bash
# /usr/lib/nagios/plugins/check_gps_receiver.sh

STATION=$1
WARN_TEMP=${2:-60}
CRIT_TEMP=${3:-70}

/opt/receivers/venv/bin/receivers health "$STATION" \
    --format icinga \
    --warn-temp "$WARN_TEMP" \
    --crit-temp "$CRIT_TEMP" \
    --warn-volt 11.5 \
    --crit-volt 10.0 \
    --warn-cpu 80 \
    --crit-cpu 95

exit $?
```

### Icinga2 Configuration

**Command definition**:
```
# /etc/icinga2/conf.d/commands/gps-receiver.conf

object CheckCommand "gps-receiver-health" {
  import "plugin-check-command"

  command = [ "/usr/lib/nagios/plugins/check_gps_receiver.sh" ]

  arguments = {
    "--station" = {
      value = "$gps_station$"
      required = true
    }
    "--warn-temp" = "$gps_warn_temp$"
    "--crit-temp" = "$gps_crit_temp$"
  }
}
```

**Service definition**:
```
# /etc/icinga2/conf.d/services/gps-receivers.conf

apply Service "gps-receiver-health" {
  import "generic-service"

  check_command = "gps-receiver-health"

  vars.gps_station = host.vars.station_id
  vars.gps_warn_temp = 60
  vars.gps_crit_temp = 70

  assign where host.vars.receiver_type in ["PolaRX5", "NetR9", "NetRS", "G10"]
}
```

**Host definition**:
```
# /etc/icinga2/conf.d/hosts/gps-stations.conf

object Host "gps-eldc" {
  import "generic-host"

  address = "10.170.x.x"

  vars.station_id = "ELDC"
  vars.receiver_type = "PolaRX5"
  vars.notification["mail"] = {
    groups = [ "gps-ops" ]
  }
}
```

### Performance Data Output

```bash
receivers health ELDC --format icinga

# Output:
# OK: ELDC PolaRX5 healthy | temp=45.2C;60;70 volt=12.3V;11.5;10.0 cpu=25%;80;95 sats=12;4;2
#
# Format: metric=value[unit];warn;crit;min;max
```

## Grafana Integration

### Data Source: PostgreSQL

**Connection configuration**:
```yaml
# /etc/grafana/provisioning/datasources/postgres.yaml

apiVersion: 1

datasources:
  - name: GPS Health Database
    type: postgres
    url: rek2.vedur.is:5432
    database: gps
    user: grafana_reader
    secureJsonData:
      password: '<password>'
    jsonData:
      sslmode: require
      postgresVersion: 1400
      timescaledb: false
```

### Dashboard Panels

#### 1. Temperature Trend
```sql
SELECT
  timestamp AS time,
  recv_temp AS "Temperature (°C)"
FROM checkcomm
WHERE
  sid = '$station'
  AND timestamp > $__timeFrom()
  AND timestamp < $__timeTo()
ORDER BY timestamp
```

#### 2. Voltage Monitoring
```sql
SELECT
  timestamp AS time,
  recv_volt AS "Voltage (V)"
FROM checkcomm
WHERE
  sid = '$station'
  AND timestamp > $__timeFrom()
  AND timestamp < $__timeTo()
ORDER BY timestamp
```

#### 3. Satellite Tracking
```sql
SELECT
  timestamp AS time,
  (recv_metrics->'satellites'->>'tracking')::int AS "Satellites"
FROM checkcomm
WHERE
  sid = '$station'
  AND recv_metrics->'satellites' IS NOT NULL
  AND timestamp > $__timeFrom()
  AND timestamp < $__timeTo()
ORDER BY timestamp
```

#### 4. Station Status Overview
```sql
SELECT
  sid AS station,
  overall_status AS status,
  recv_temp AS temp,
  recv_volt AS volt,
  (recv_metrics->'satellites'->>'tracking')::int AS sats,
  timestamp AS last_check
FROM checkcomm
WHERE timestamp > NOW() - INTERVAL '1 hour'
ORDER BY sid
```

#### 5. Alert History
```sql
SELECT
  timestamp AS time,
  sid AS station,
  overall_status AS status,
  recv_metrics AS metrics
FROM checkcomm
WHERE
  overall_status IN ('warning', 'critical')
  AND timestamp > $__timeFrom()
  AND timestamp < $__timeTo()
ORDER BY timestamp DESC
```

### Alert Rules

**High Temperature Alert**:
```sql
-- Query
SELECT
  sid,
  recv_temp
FROM checkcomm
WHERE
  timestamp > NOW() - INTERVAL '5 minutes'
  AND recv_temp > 65
GROUP BY sid, recv_temp
```

**Low Voltage Alert**:
```sql
-- Query
SELECT
  sid,
  recv_volt
FROM checkcomm
WHERE
  timestamp > NOW() - INTERVAL '5 minutes'
  AND recv_volt < 11.0
GROUP BY sid, recv_volt
```

**Poor Satellite Tracking**:
```sql
-- Query
SELECT
  sid,
  (recv_metrics->'satellites'->>'tracking')::int AS satellites
FROM checkcomm
WHERE
  timestamp > NOW() - INTERVAL '5 minutes'
  AND (recv_metrics->'satellites'->>'tracking')::int < 6
GROUP BY sid, satellites
```

## Legacy System Compatibility

### gps2influx.py Migration

The existing `gps2influx.py` script continues to work with enhanced schema:

```python
# Updated gps2influx.py to handle new columns
def readFromPsql(unit, value):
    query = '''
        SELECT
            timestamp,
            rout_stat,
            recv_stat,
            recv_temp,
            recv_volt,
            recv_metrics  -- NEW: Additional metrics
        FROM checkcomm
        WHERE sid = %s
        AND timestamp > NOW() - INTERVAL '%s %s'
    '''
    # ... process and send to InfluxDB
```

### InfluxDB Schema

```python
json_body = [{
    "measurement": "gps_receiver_health",
    "tags": {
        "station": station_id,
        "receiver_type": receiver_type
    },
    "time": timestamp,
    "fields": {
        "temperature": recv_temp,
        "voltage": recv_volt,
        "cpu_load": cpu_load,
        "satellites": satellite_count,
        "overall_status": overall_status
    }
}]

influx_client.write_points(json_body)
```

## Email Alerts

### Icinga Email Notifications

```
# /etc/icinga2/conf.d/notifications.conf

apply Notification "gps-health-alert" to Service {
  import "mail-service-notification"

  users = [ "gps-ops" ]

  vars.notification_logtosyslog = true

  assign where service.name == "gps-receiver-health"
}
```

### Email Template

```
Subject: [ALERT] GPS Receiver $station$ - $status$

Station: $station$ ($receiver_type$)
Status: $status$
Time: $timestamp$

Details:
- Temperature: $temperature$°C (threshold: $temp_threshold$)
- Voltage: $voltage$V (threshold: $volt_threshold$)
- CPU Load: $cpu_load$%
- Satellites: $satellites$ tracking

Connection:
- Router: $router_status$
- Receiver: $receiver_status$

Action Required: Please investigate receiver health

Dashboard: https://grafana.vedur.is/d/gps-health?var-station=$station$
```

## Scheduled Health Collection

### Systemd Timer

```ini
# /etc/systemd/system/gps-health-collector.timer

[Unit]
Description=GPS Receiver Health Collection Timer
Requires=gps-health-collector.service

[Timer]
OnCalendar=hourly
OnCalendar=*:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/gps-health-collector.service

[Unit]
Description=GPS Receiver Health Collection
After=network.target postgresql.service

[Service]
Type=oneshot
User=gpsops
ExecStart=/opt/receivers/venv/bin/receivers health --all --extract --save-db
StandardOutput=journal
StandardError=journal
```

### Cron Alternative

```cron
# /etc/cron.d/gps-health-collection

# Collect health data every hour at :30
30 * * * * gpsops /opt/receivers/venv/bin/receivers health --all --extract --save-db >> /var/log/gps/health-collection.log 2>&1
```

## Monitoring Best Practices

### Alert Fatigue Prevention
- Set appropriate thresholds
- Use warning/critical levels
- Implement alert grouping
- Add quiet periods for known maintenance

### Data Retention
- Keep high-resolution data for 30 days
- Aggregate to hourly averages for 1 year
- Keep daily averages indefinitely
- Archive raw JSON files

### Performance Optimization
- Index database queries
- Use read replicas for Grafana
- Cache dashboard queries
- Batch health collections

## Resources

- [Icinga2 Documentation](https://icinga.com/docs/icinga-2/latest/)
- [Grafana PostgreSQL Data Source](https://grafana.com/docs/grafana/latest/datasources/postgres/)
- [Nagios Plugin Development](https://nagios-plugins.org/doc/guidelines.html)

---

**Status**: Development
**Last Updated**: 2025-10-02
