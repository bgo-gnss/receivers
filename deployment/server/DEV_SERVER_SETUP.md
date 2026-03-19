# GPS Receivers — Dev Server Setup

## Prerequisites

- Ubuntu 25.04+ (or 24.04 LTS) with sudo access
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

## Architecture

### User Model

| User | Role | Owns |
|------|------|------|
| `bgo` | Admin | Repos, venv, config files — does git pull, pip install, edits config |
| `gpsops` | Service | Cache/logs, data — runs the scheduler, reads config via group perms |

**Key principle:** bgo owns all code and config. gpsops only owns what it writes to (logs, data, scheduler DB). No `sudo su - gpsops` needed for code updates — just `git pull && pip install -e .` as bgo.

### File Layout

```
/home/bgo/git/
├── receivers/               # Main package + venv
│   └── venv/                # Python virtual environment
├── gtimes/                  # GPS time library
├── gps_parser/              # Config management
├── gps-config-data/         # Station configs (from git.vedur.is)
└── gps-tools/               # Proprietary binaries (from git.vedur.is)

/home/gpsops/
├── .config/gpsconfig/       # Config files (bgo:gpsops 640, group-readable)
└── .cache/gps_receivers/    # Logs, scheduler DB, tmp (owned by gpsops)
    ├── logs/
    │   ├── receivers.log
    │   └── download_audit.jsonl
    └── tmp/

/usr/local/bin/receivers     # Symlink — CLI available to all users
/mnt/data/gpsdata/                # Local working data (owned by gpsops)
/mnt/rawgpsdata/             # NFS mount to production archive (read-only)
```

## What the Install Does

### Phase 1: System packages
PostgreSQL, Python 3, Git, NFS client, Docker.

### Phase 2: Users + directories
- Creates `gpsops` user with home directory `/home/gpsops/`
- Creates `gpsops` group (if not from AD/LDAP), adds `bgo` to it
- Makes bgo's home + git dir world-traversable (`o+x`) so gpsops can access venv
- Creates `/home/gpsops/.config/gpsconfig/` and `/home/gpsops/.cache/gps_receivers/`
- Creates `/mnt/data/gpsdata/` (local data), `/mnt/rawgpsdata/` (NFS archive)
- Adds NFS fstab entry for production archive
- Generates SSH key for `gpsops` (for rsync to production archive)

### Phase 3: Git repositories
All repos cloned as `bgo` via HTTPS into `~/git/`. Public repos (receivers, gtimes, gps_parser) need no auth. Internal repos (gps-config-data from git.vedur.is) need bgo's LDAP credentials. All repos are made world-readable.

### Phase 4: Python virtual environment
Creates `~/git/receivers/venv/` owned by bgo, installs all packages in editable mode. Symlinks `receivers` CLI to `/usr/local/bin/`. Verifies gpsops can execute it.

### Phase 5: Configuration
Copies configs from gps-config-data to `/home/gpsops/.config/gpsconfig/`, patches:
- `database.cfg`: host=localhost, user=gpsops, mirror_host=pgdev.vedur.is, mirror_user=bgo
- `receivers.cfg`: data_prepath=/mnt/data/gpsdata/

Config files are owned `bgo:gpsops` with mode 640 (bgo edits, gpsops reads).

### Phase 6: PostgreSQL
Creates roles (`bgo` superuser, `gpsops`), database `gps_health`, configures auth (peer + trust for localhost), disables JIT.

### Phase 7: Migrations
Runs `000_consolidated_schema.sql` on fresh DB, then applies pending migrations. Grants `gpsops` full access to all tables.

### Phase 8: External tools
Clones `gps/gps-tools` from git.vedur.is (RxTools, teqc, gfzrnx). Symlinks binaries to `/usr/local/bin/`.

### Phase 9: Docker + Grafana + Trimble converter
Starts Grafana on port 3000 with auto-provisioned dashboards and PostgreSQL datasource. Pulls the `trm2rinex:cli-light` Docker image (~2.4 GB) for native Trimble RINEX 3 conversion.

### Phase 10: systemd
Installs and enables `gps-receivers-scheduler.service`, configures logrotate. Patches paths in the service file to match the installation.

### Phase 11: Verification
Checks CLI, config files, database, tools, Grafana. Prints summary.

## Day-to-Day Operations

### Updating Code

```bash
# As bgo — no sudo needed for code updates:
cd ~/git/receivers && git pull
~/git/receivers/venv/bin/pip install -e .
sudo systemctl restart gps-receivers-scheduler
```

### Updating Configuration

```bash
cd ~/git/gps-config-data && git pull
cp stations.cfg receivers.cfg database.cfg scheduler.yaml /home/gpsops/.config/gpsconfig/

# Station config changes are auto-detected (no restart needed)
# For scheduler.yaml changes:
sudo systemctl restart gps-receivers-scheduler
```

### Running Migrations

```bash
cd ~/git/receivers
psql -d gps_health -f migrations/NNN_whatever.sql
psql -d gps_health -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO gpsops"
sudo systemctl restart gps-receivers-scheduler
```

Or re-run the install script (auto-detects pending migrations):
```bash
sudo bash ~/git/receivers/deployment/server/install.sh
```

### Manual Downloads

```bash
# Run as gpsops:
sudo -u gpsops receivers download ELDC --sync --archive

# Test connection:
sudo -u gpsops receivers download ELDC --test-connection

# Health check:
sudo -u gpsops receivers health THOB --verbose
```

### Viewing Logs

```bash
# Systemd journal (live)
journalctl -u gps-receivers-scheduler -f

# JSON log file
tail -f /home/gpsops/.cache/gps_receivers/logs/receivers.log | jq .

# Audit trail
tail -f /home/gpsops/.cache/gps_receivers/logs/download_audit.jsonl | jq .
```

### Service Management

```bash
sudo systemctl start gps-receivers-scheduler
sudo systemctl stop gps-receivers-scheduler
sudo systemctl restart gps-receivers-scheduler
sudo systemctl status gps-receivers-scheduler

# Check scheduler state
sudo -u gpsops receivers scheduler status --show-jobs
```

### Grafana

```bash
# Restart (picks up dashboard JSON changes)
docker restart gps-grafana

# Full restart
cd ~/git/receivers/deployment/server
docker compose down && docker compose up -d
```

Access at `http://<server-ip>:3000` (anonymous viewer access enabled).

## Wiping / Reinstalling

```bash
# Wipe venv + redeploy config (keep data and database)
sudo bash ~/git/receivers/deployment/server/install.sh --wipe

# Drop database only (keeps data files)
sudo bash ~/git/receivers/deployment/server/install.sh --wipe-db

# Full wipe: drop DB + delete data + reinstall everything
sudo bash ~/git/receivers/deployment/server/install.sh --wipe-all
```

## Trimble RINEX 3 Conversion

The primary method for converting Trimble T02/T00 files to RINEX 3 is the Docker-based **trm2rinex** converter, which runs the official Trimble `convertToRinex.exe` under Wine.

```bash
# Check status
tools/trimble-native/setup.sh --check

# Manual install if needed
docker pull geodesyewsp/trm2rinex:cli-light
docker tag geodesyewsp/trm2rinex:cli-light trm2rinex:cli-light

# Convert Trimble files (native RINEX 3)
sudo -u gpsops receivers rinex MANA --native-trimble -d 1
```

The fallback chain (`runpkr00` → `teqc` → `gfzrnx`) produces reformatted RINEX 3 (not native observation codes). Use only when Docker is unavailable.

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

## Data Storage

| Location | Purpose | Access |
|----------|---------|--------|
| `/mnt/data/gpsdata/` | Local working data (downloads, processing) | Read/Write, owned by gpsops |
| `/mnt/rawgpsdata/` | Production archive (read-only reference) | Read-only, NFS from ananas.vedur.is |
| `rawdata.vedur.is` | Production archive (write target) | rsync over SSH as gpsops |

## Dual-Database Write

The scheduler writes to two databases simultaneously:
- **Primary**: localhost (local PostgreSQL, authenticates as `gpsops` via peer)
- **Mirror**: pgdev.vedur.is (authenticates as `bgo` via LDAP — `mirror_user` in database.cfg)

Mirror failures are logged but don't affect the primary.

## Troubleshooting

### Service won't start

```bash
journalctl -u gps-receivers-scheduler -n 50 --no-pager

# Common causes:
# - PostgreSQL not running: sudo systemctl start postgresql
# - Config missing: ls -la /home/gpsops/.config/gpsconfig/
# - Permission denied: check gpsops can read venv and config
```

### Database connection fails

```bash
sudo -u gpsops psql -d gps_health -c "SELECT 1"
sudo -u postgres psql -c "SHOW hba_file"
```

### Grafana shows no data

1. Check data: `psql -d gps_health -c "SELECT count(*) FROM block_health_summary WHERE ts > now() - interval '10 minutes'"`
2. Check datasource in Grafana UI
3. Restart: `docker restart gps-grafana`

### External tools missing

```bash
which bin2asc sbf2rin teqc gfzrnx RNX2CRX
# If shared library errors:
sudo ldconfig
```

---

**Maintainer**: Veðurstofa Íslands GPS Team
**Last updated**: 2026-03-19
