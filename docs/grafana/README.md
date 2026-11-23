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

## Dashboard: GPS Long-term Backfill Runs

**File**: `gps_ltb_runs_dashboard.json` · **UID**: `gps-ltb-runs` · **Refresh**: 15 min ·
**Default range**: last 30 days · **Source table**: `ltb_runs` (migration 075)

A dedicated dashboard rather than a row on Data Delivery: that dashboard is per-station and
"today", this one is per-*run* and a 30-day trend, for a different reader (whoever is tuning
`long_term_backfill.lookback_days` / `max_run_seconds`), and it must be possible to see it
blank without the delivery dashboard looking broken.

What it answers: **did a run hit a cap, and is that getting worse?**

- **Last daily run** stat row — stopped on (`completed` / `wall-clock` / `slots`), time used %,
  slots used %, recovered, nothing-on-receiver, and "daily runs stopped early in 30 d"
- **Seconds used vs cap** and **Slots used vs cap** — the run against the dashed cap line
- **Outcomes per run** (stacked bars) — recovered / nothing on receiver / failed / unreachable /
  budget capped — and **Yield %** = recovered / (recovered + nothing on receiver)
- **Stopped early** table — empty is the healthy state
- **Reconnection trigger** — the same, against ITS ceiling (`reconnection_max_run_seconds`, 600 s)

Rows are written best-effort by `receivers.scheduling.ltb_run_metrics` at the end of each run;
a DB failure there is a WARNING in `receivers.log`, never a failed run.

**Production caveat (pgdev)**: the LTB runs on rek-d01 and writes rek-d01's `gps_health`;
grafana.vedur.is reads pgdev. The row reaches pgdev only through the `mirror_host` dual-write
in `database_factory` (the writer deliberately does NOT use `single_host=True`), and only once
migration 075 has been applied on pgdev as well — pgdev migrations are hand-applied. Until
both hold, treat the dashboard as dev-only.

## Dashboard: GPS Daily Solutions

**File**: `gps_daily_solutions_dashboard.json` · **UID**: `gps-daily-solutions` · **Refresh**: 15 min ·
**Default range**: last 45 days (UTC) · **Source**: `solution_day_grid()` (migration 077)

Daily POS solution availability per station and UTC day. This is slice 3 of
`docs/architecture/daily-solutions-implementation-plan.md`. Every panel reads the
states from `solution_day_grid` and never re-derives them; the dashboard only turns
state and reason into a colour.

- **Stat row:** last successful check, latest check outcome, problem days (pending
  before the deadline is normal and not counted), confirmed absences, unexpected files.
- **Stations with missing solutions:** the list for the monitoring room, modelled on the
  "GPS Station Health — 24h" table. One row per station: status of the latest *due* day
  (Missing = confirmed; Late = not yet confirmed; Invalid; Unknown; OK), **Since** and
  **Days** of the current gap (`≤` means the gap starts before the time range), last good
  day, a plain-language reason, a **plot ↗** menu with the plate-fixed plots from yang's gpsplot tree (90 days, 1 year, full series; base URL in the hidden `plot_base` constant, empty for stations not in `stations`), and
  last **Total**: all problem days in the range, including gaps that have since recovered
  (so Total ≥ Days; it shows stations that keep dropping out). **Area** is one short label, the volcano area before the region, with `+N` for
  the other areas the station belongs to. Column widths are fixed so the table fits beside
  the map without scrolling sideways. Click the SID for its grid row, data delivery and
  receiver health.
- **Map:** the same status per station, with problem stations drawn larger. Coordinates
  come from `stations`.
- **Area** filter, the same `station_areas` variable the other GPS boards use.
- **Stations** filter: `local` (default) is every station in Iceland that is not inactive,
  whoever runs it (HOFN, REYK, BJAC included); `all` adds the global reference stations the
  roster carries because their solutions arrive, and lists their files in the
  unexpected-file table. **Agency** is the stations.cfg `station_owner`, the same filter as
  on the other GPS boards. Both read `solution_station` (migration 078), which every check
  run refreshes from stations.cfg; stations without an owner show under `all` only.
- **Station × UTC day grid:** a table with the *grouping to matrix* transform. The
  `Rows` variable switches between `problems` (only stations with a problem day) and
  `all`. Click a station for its data-delivery and receiver dashboards.
- **Problem days, counts by state and reason, recent check runs, and unexpected
  files/other products.**

Grid cell legend:

| Cell | Colour | State / reason | Meaning |
|---|---|---|---|
| ✓ | green | available / verified | valid row, re-checked in the 45-day window |
| ❄ | blue-grey | available / frozen | valid when last verified, now older than the window |
| … | light blue | pending / before_deadline | not due yet (D+1 09:15 UTC) |
| ↻ | blue | pending / awaiting_recheck | absent, but only seen before the deadline |
| late | orange | overdue / no_completion_record | late; not confirmed missing |
| ✗ | red | overdue / absent_after_completion | confirmed missing |
| rej | dark red | overdue / producer_rejected, producer_failed | the producer says so |
| inv | purple | invalid | a bad row or file (see Problem days for the code) |
| – | light grey | not_expected | excluded, or outside the station's roster intervals |
| ? | dark grey | unknown | stale or failed check, unreadable, never checked |

Unknown and frozen cells are never plain green.

**Gotcha (Grafana 12.3.1), found while building this:** the matrix transform sorts its
columns as text, and the table matches columns by *display name*. The column key is
therefore the full ISO date, and the header is renamed to `MM-DD`. A bare `DD` made
October and November share columns and silently showed November's cells under
October's dates. `tests/test_solution_dashboard.py` pins both properties.

**Not on grafana.vedur.is yet.** pgdev needs migration 077 (applied as bgo), and a
real check must be writing to it (slice 5). Only then add it to the `vedur` target in
`scripts/grafana_sync.py`'s `grafana_targets.yaml`.

## Navigation between the boards

Every station board has the same top-bar buttons (Health Overview, Map, Station
Detail, Data Delivery, LTB Runs where relevant, Daily Solutions). Links into and out
of **Daily Solutions** pass only the area (`?var-area=${area}`), never the time range
or the other variables: each board keeps its own range, because 45 days on the
`block_*` boards is a heavy query. All boards share one `area` variable (`'all'` is a
real option, not Grafana's All) so `var-area=` works in both directions.

`tests/test_grafana_links.py` fails when a link points to a uid that no board in
`docs/grafana` has, or when a board provisioned on rek-d01 or the laptop
(`deployment/*/docker-compose.yml`) links to one that is not provisioned there.
Add a new board to both compose files when another board links to it.

## Syncing to grafana.vedur.is

The `scripts/grafana_sync.py` tool pushes local dashboard JSON to the production Grafana instance. It remaps datasource UIDs and inter-dashboard link UIDs automatically. Top-bar links to boards the target does not carry (for now LTB Runs) are dropped on the way, instead of showing "Dashboard not found".

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
