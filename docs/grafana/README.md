# GPS Health Grafana Dashboard

Grafana dashboard for monitoring the Icelandic Met Office GNSS receiver network health, voltage, and data quality.

## Quick Start

```bash
# Start Grafana (and scheduler) via Docker Compose
cd deployment/docker-dev
docker compose up -d grafana

# Access at http://localhost:3001
# Default credentials: admin / admin
```

## Architecture

```
docs/grafana/
├── gps_health_dashboard.json          # Main dashboard definition
├── provisioning/
│   ├── dashboards/dashboards.yaml     # Dashboard auto-provisioning config
│   └── datasources/datasources.yaml   # PostgreSQL datasource config
└── README.md                          # This file

deployment/docker-dev/
└── docker-compose.yml                 # Grafana service definition
```

## Dashboard: GPS Receiver Health - Voltage

**UID**: `gps-health-voltage`
**Refresh**: 5 minutes
**Default time range**: Last 7 days

### Sections

#### Overview (top)
- **Active Stations** - Count of stations reporting in time window
- **Critical Alerts** - Stations with critical health status
- **Warnings** - Stations with warning status
- **Stations with Issues** - Table of stations with warning/critical voltage
- **Latest Voltage - All Stations** - Table with per-station voltage and status

#### Voltage Trends - All Stations
- **Voltage Over Time** - Time series of all station voltages with threshold lines (11V warning, 15V warning, 11.8V-15V green)

#### Station Detail (per-station, uses `$station` variable)
- **Current Voltage** - Gauge (8-18V range)
- **Overall Status** - Healthy/Warning/Critical indicator
- **Last Observation** - Time since last data point (age)
- **Satellite Tracking** - Total count + per-constellation bar gauge (GPS, GLO, GAL, BDS)
- **Temperature** - Receiver temperature (C)
- **CPU Load** - Receiver CPU usage (%)
- **Disk Usage** - Internal storage (%)
- **Uptime** - Receiver uptime (seconds)
- **Station Location** - Lat/Lon/Fix type table
- **Ports** - FTP/HTTP/Control port status (open/closed)
- **File Status** - Age of latest downloaded file per session (24h, 1Hz, Status)
- **Logging** - Active session tracking (15s, 1Hz, Status)
- **NTRIP Status** - RTK connection state
- **Voltage History** - Single-station voltage time series with legend stats
- **Recent Data** - Table of latest 100 records with all metrics

## Datasource

- **Type**: PostgreSQL (`grafana-postgresql-datasource`)
- **UID**: `gps_health`
- **Database**: `gps_health`
- **Port**: 5432 (localhost)
- **Authentication**: Environment variables `GF_DATASOURCE_USER` / `GF_DATASOURCE_PASSWORD`

### Required Database Tables

| Table | Purpose |
|-------|---------|
| `block_power_status` | Voltage readings (sid, ts, voltage) |
| `block_health_summary` | Overall health status per station |
| `block_receiver_status` | Temperature, CPU, uptime |
| `block_disk_status` | Disk usage percentage |
| `block_satellite_tracking` | Satellite counts per constellation |
| `block_pvt_geodetic` | Position/fix information |
| `block_ntrip_client` | NTRIP client connection status |
| `block_ntrip_server` | NTRIP server connection status |
| `file_tracking` | Download tracking per session type |
| `stations` | Station list for dropdown variable |

## Docker Deployment

The Grafana service is defined in `deployment/docker-dev/docker-compose.yml`:

- **Image**: `grafana/grafana:10.4.2`
- **Port**: 3001 (host network mode)
- **Dashboard provisioning**: Auto-loads from JSON via bind mount
- **Datasource provisioning**: Auto-configured via YAML
- **Persistent storage**: `grafana-data` Docker volume

### Updating the Dashboard

1. Edit `docs/grafana/gps_health_dashboard.json` directly or export from Grafana UI
2. Restart the Grafana container to pick up changes:
   ```bash
   docker compose restart grafana
   ```
3. The provisioning config (`updateIntervalSeconds: 30`) also auto-reloads periodically

### Environment Variables

```bash
# Set PostgreSQL credentials before starting
export GF_DATASOURCE_USER=bgo
export GF_DATASOURCE_PASSWORD=your_password

docker compose up -d grafana
```

## Voltage Thresholds

The dashboard uses consistent voltage thresholds across all panels:

| Range | Color | Meaning |
|-------|-------|---------|
| < 11.0V | Red | Critical - power supply failing |
| 11.0 - 11.8V | Yellow | Warning - battery low |
| 11.8 - 15.0V | Green | Normal operating range |
| 15.0 - 16.0V | Yellow | Warning - overvoltage |
| > 16.0V | Red | Critical - overvoltage |

---

**Last updated**: 2026-02-01
**Grafana version**: 10.4.2
**Dashboard version**: GPS Receiver Health - Voltage v1
