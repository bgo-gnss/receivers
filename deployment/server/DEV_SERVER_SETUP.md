# GPS Receivers — Dev Server Setup

## Prerequisites

- Ubuntu 25.10 (or 24.04 LTS) with sudo access
- SSH access as `bgo` user
- Network access to:
  - `github.com` (receivers, gtimes, gps_parser repos)
  - `git.vedur.is` (gps-config-data, gps-tools repos)
  - `pgdev.vedur.is:5432` (mirror database)
  - `ananas.vedur.is` (NFS archive mount)
  - GPS receiver network (stations)

## Quick Install

```bash
# Clone the receivers repo
mkdir -p ~/git
git clone https://github.com/bennigo/receivers.git ~/git/receivers

# Run the install script
cd ~/git/receivers
sudo bash deployment/server/install.sh
```

The script is idempotent — running it again updates everything without breaking existing state.

## What the Install Does

### Phase 1: System packages
PostgreSQL, Python 3, Git, NFS client, Docker.

### Phase 2: Users + directories
- Creates `gpsops` system user (service identity)
- Adds `bgo` to `gpsops` group
- Creates `/mnt/gpsdata/` (local data), `/mnt/rawgpsdata/` (NFS archive mount)
- Adds NFS fstab entry for production archive
- Generates SSH key for `gpsops` (for rsync to production archive)

### Phase 3: Git repositories
Clones/updates four repos to `/opt/`:
- `~/git/receivers` — main package
- `~/git/gtimes` — GPS time library
- `/opt/gps_parser` — config management
- `~/git/gps-config-data` — station configs from git.vedur.is

### Phase 4: Python virtual environment
Creates `~/git/receivers/venv/` owned by `gpsops`, installs all packages in editable mode.

### Phase 5: Configuration
Copies configs from gps-config-data to `/etc/gpsconfig/`, patches:
- `database.cfg`: host=localhost, user=gpsops, mirror_host=pgdev.vedur.is
- `receivers.cfg`: data_prepath=/mnt/gpsdata/

### Phase 6: PostgreSQL
Creates roles (`bgo` superuser, `gpsops` owner), database `gps_health`, configures auth (peer + trust for localhost), disables JIT.

### Phase 7: Migrations
Runs `000_consolidated_schema.sql` on fresh DB, then applies any pending migrations (029+). Tracks via `schema_migrations` table.

### Phase 8: External tools
Clones `gps/gps-tools` from git.vedur.is (RxTools, teqc, gfzrnx). Symlinks binaries to `/usr/local/bin/`.

### Phase 9: Docker + Grafana + Trimble converter
Starts Grafana on port 3000 with auto-provisioned dashboards and PostgreSQL datasource.
Pulls the `trm2rinex:cli-light` Docker image (~2.4 GB) for native Trimble RINEX 3 conversion. This is the primary method for converting Trimble T02/T00 files to RINEX 3 — `runpkr00 + teqc + gfzrnx` is only a fallback.

### Phase 10: systemd
Installs and enables `gps-receivers-scheduler.service`, configures logrotate.

### Phase 11: Verification
Checks CLI, config files, database, tools, Grafana. Prints summary.

## Day-to-Day Operations

### Updating Code

```bash
# As bgo:
cd ~/git/receivers && git pull
sudo -u gpsops ~/git/receivers/venv/bin/pip install -e .
sudo systemctl restart gps-receivers-scheduler
```

### Updating Configuration

```bash
cd ~/git/gps-config-data && git pull
sudo cp stations.cfg receivers.cfg database.cfg scheduler.yaml /etc/gpsconfig/
sudo chown root:gpsops /etc/gpsconfig/*
sudo chmod 640 /etc/gpsconfig/*

# Station config changes are auto-detected (no restart needed)
# For scheduler.yaml changes:
sudo systemctl restart gps-receivers-scheduler
```

### Running Migrations

```bash
cd ~/git/receivers
psql -d gps_health -f migrations/NNN_whatever.sql
sudo systemctl restart gps-receivers-scheduler
```

Or re-run the install script (it auto-detects pending migrations):
```bash
sudo ./deployment/server/install.sh
```

### Manual Downloads

```bash
# Run as gpsops with config path set
sudo -u gpsops GPS_CONFIG_PATH=/etc/gpsconfig \
  ~/git/receivers/venv/bin/receivers download ELDC --sync --archive

# Test connection
sudo -u gpsops GPS_CONFIG_PATH=/etc/gpsconfig \
  ~/git/receivers/venv/bin/receivers download ELDC --test-connection

# Health check
sudo -u gpsops GPS_CONFIG_PATH=/etc/gpsconfig \
  ~/git/receivers/venv/bin/receivers health THOB --verbose
```

### Viewing Logs

```bash
# Systemd journal (live)
journalctl -u gps-receivers-scheduler -f

# JSON log file
tail -f /var/cache/gps_receivers/logs/receivers.log | jq .

# Audit trail
tail -f /var/cache/gps_receivers/logs/download_audit.jsonl | jq .
```

### Service Management

```bash
sudo systemctl start gps-receivers-scheduler
sudo systemctl stop gps-receivers-scheduler
sudo systemctl restart gps-receivers-scheduler
sudo systemctl status gps-receivers-scheduler

# Check scheduler state
sudo -u gpsops GPS_CONFIG_PATH=/etc/gpsconfig \
  ~/git/receivers/venv/bin/receivers scheduler status --show-jobs
```

### Grafana

```bash
# Restart (picks up dashboard JSON changes)
docker restart gps-grafana

# Logs
docker logs gps-grafana -f

# Full restart
cd ~/git/receivers/deployment/server
docker compose down && docker compose up -d
```

Access at `http://<server-ip>:3000` (anonymous viewer access enabled).

## Wiping / Reinstalling

```bash
# Wipe venv + redeploy config (keep data and database)
sudo ./deployment/server/install.sh --wipe

# Drop database only (keeps data files)
sudo ./deployment/server/install.sh --wipe-db

# Full wipe: drop DB + delete data + reinstall everything
sudo ./deployment/server/install.sh --wipe-all
```

## Trimble RINEX 3 Conversion

The primary method for converting Trimble T02/T00 files to RINEX 3 is the Docker-based **trm2rinex** converter, which runs the official Trimble `convertToRinex.exe` under Wine.

The install script automatically pulls the image (`geodesyewsp/trm2rinex:cli-light`, ~2.4 GB).

```bash
# Check status
tools/trimble-native/setup.sh --check

# Manual install if needed
docker pull geodesyewsp/trm2rinex:cli-light
docker tag geodesyewsp/trm2rinex:cli-light trm2rinex:cli-light

# Convert Trimble files (native RINEX 3)
sudo -u gpsops GPS_CONFIG_PATH=/etc/gpsconfig \
  ~/git/receivers/venv/bin/receivers rinex MANA --native-trimble -d 1
```

The fallback chain (`runpkr00` → `teqc` → `gfzrnx`) is available via the gps-tools repo but produces reformatted RINEX 3 (not native observation codes). Use only when Docker is unavailable.

## External Tools (RxTools, gfzrnx)

Proprietary tools are managed via the `gps/gps-tools` repo on git.vedur.is:

```
~/git/gps-tools/
├── rxtools/
│   ├── bin/        # bin2asc, sbf2rin, sbfanalyzer
│   └── lib/        # Qt6, libcomms, libgeod shared libraries
└── bin/            # teqc, gfzrnx, RNX2CRX, runpkr00, mdb2rinex
```

The install script symlinks these to `/usr/local/bin/` and configures `ld.so.conf` for RxTools shared libraries.

If the gps-tools repo is not available, install manually:
```bash
# Copy RxTools from laptop
scp -r /usr/local/rxtools/ server:~/git/gps-tools/rxtools/

# Open-source tools
# teqc: https://www.unavco.org/software/data-processing/teqc/teqc.html
# gfzrnx: https://gnss.gfz-potsdam.de/gfzrnx
```

## Data Storage

| Location | Purpose | Access |
|----------|---------|--------|
| `/mnt/gpsdata/` | Local working data (downloads, processing) | Read/Write, local disk |
| `/mnt/rawgpsdata/` | Production archive (read-only reference) | Read-only, NFS from ananas.vedur.is |
| `rawdata.vedur.is` | Production archive (write target) | rsync over SSH as gpsops |

## User Model

| User | Role | Actions |
|------|------|---------|
| `bgo` | Admin | SSH login, git pull, pip install, run migrations, restart service |
| `gpsops` | Service | Runs scheduler, owns data and venv, never used interactively |

## Dual-Database Write

The scheduler writes to two databases simultaneously:
- **Primary**: localhost (local PostgreSQL on dev server)
- **Mirror**: pgdev.vedur.is (external DB that grafana.vedur.is reads)

Mirror failures are logged but don't affect the primary. Configured via `mirror_host` in `database.cfg`.

## Troubleshooting

### Service won't start

```bash
# Check logs
journalctl -u gps-receivers-scheduler -n 50 --no-pager

# Common causes:
# - PostgreSQL not running: sudo systemctl start postgresql
# - Config missing: ls -la /etc/gpsconfig/
# - Permission denied: check file ownership (root:gpsops, 640)
```

### Database connection fails

```bash
# Test as gpsops
sudo -u gpsops psql -d gps_health -c "SELECT 1"

# Check pg_hba.conf
sudo -u postgres psql -c "SHOW hba_file"
sudo cat $(sudo -u postgres psql -tAc "SHOW hba_file") | grep gps_health
```

### NFS mount issues

```bash
# Check mount
mountpoint /mnt/rawgpsdata

# Manual mount
sudo mount /mnt/rawgpsdata

# Check connectivity
ping -c 1 ananas.vedur.is
```

### Grafana shows no data

1. Check scheduler is running and health data appears in DB:
   ```bash
   psql -d gps_health -c "SELECT count(*) FROM block_health_summary WHERE ts > now() - interval '10 minutes'"
   ```
2. Check Grafana datasource: Settings → Data Sources → gps_health → Test
3. Restart Grafana: `docker restart gps-grafana`

### External tools missing

```bash
# Check tool paths
which bin2asc sbf2rin teqc gfzrnx RNX2CRX

# If RxTools fails with shared library errors
ldd ~/git/gps-tools/rxtools/bin/bin2asc
sudo ldconfig
```

---

**Maintainer**: Veðurstofa Íslands GPS Team
**Last updated**: 2026-03-18
