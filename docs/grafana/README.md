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

## Troubleshooting

### "context canceled" / "Grafana has failed to load its application files" after restart

**Symptom**: After `docker restart gps-grafana` or `docker compose up --force-recreate`, the browser shows either a blank page, an HTTP 500, or "Grafana has failed to load its application files". `curl http://localhost:3000/` works fine.

**Root cause**: The browser's HTTP keep-alive connection pool holds TCP connections that were open to the old Grafana container. The new container does not recognise these connections. When the browser sends a new request on a stale socket:
1. Grafana's TCP stack receives the request bytes but the socket is already half-dead.
2. Go's `net/http` server cancels the request context as soon as it detects the dead connection.
3. Any downstream operation (secrets-service DEK lookup, datasource password decryption) immediately fails with `"context canceled"`.
4. Grafana tries to render an error page, but Grafana 11.x has a broken error template (`.Assets.Dark` field missing) — returns 343 bytes of partial HTML.
5. The browser renders the partial HTML as "failed to load application files".

The error log pattern looks severe but is entirely caused by the stale connection:
```
logger=secrets msg="Failed to get current data key" error="context canceled"
logger=context  msg="Failed to get settings"        error="context canceled"
logger=context  msg="Request error" error="Context.HTML - Error rendering template: error …
                     template: error:16:42: … can't evaluate field Assets …"
logger=context  msg="Request Completed" status=500 remote_addr=… size=343
```
A tell-tale additional line when the response is larger (200 OK full page):
```
logger=context  msg="Request error" error="Context.HTML - Error rendering template: index …
                     write tcp …:3000->…: write: connection reset by peer"
```

**Fix**: Hard-refresh the browser (**Ctrl+Shift+R**) or open an incognito/private window. Either clears the stale connection pool. No Grafana configuration change is needed.

**Note**: This affects access via VPN more visibly than local access because VPN keep-alive behaviour is more aggressive. The underlying cause is the same regardless of network path.

---

## Syncing to grafana.vedur.is

The `scripts/grafana_sync.py` tool pushes local dashboard JSON to the production Grafana instance. It remaps datasource UIDs and inter-dashboard link UIDs automatically.

```bash
# Preview changes
python scripts/grafana_sync.py diff --target vedur -v

# Push all dashboards
python scripts/grafana_sync.py push --target vedur

# Check version status
python scripts/grafana_sync.py status --target vedur
```

### Authentication

**Preferred**: Service account token (once available):
```bash
export GRAFANA_VEDUR_TOKEN="glsa_..."
# Or save to ~/.config/gpsconfig/grafana_tokens.yaml:
#   vedur: "glsa_..."
```

**Temporary**: Session cookie (Grafana has token rotation enabled — cookie is invalidated after each request):

1. Log into grafana.vedur.is
2. DevTools → Application → Cookies → copy `grafana_session` and `grafana_session_expiry`
3. Update `~/.config/gpsconfig/grafana_cookies.yaml`:
   ```yaml
   vedur: "grafana_session=<value>; grafana_session_expiry=<value>"
   ```
4. Run `push` **immediately** — the cookie rotates on the next browser request

### Dashboard UIDs on vedur

| Dashboard | UID |
|-----------|-----|
| GPS Receiver Health Overview | `bgp9jh6` |
| GPS Station Map | `45d42ce0-48b5-4ff6-a58c-a36d3ced2e69` |
| GPS Station Detail | `bgqb686` |

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
