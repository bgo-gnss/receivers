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
# Clone the receivers repo (as bgo)
mkdir -p ~/git
git clone https://github.com/bennigo/receivers.git ~/git/receivers

# Run the install script
cd ~/git/receivers
sudo bash deployment/server/install.sh
```

The script is idempotent — running it again updates everything without breaking existing state.

## Architecture

### User Model

| User | Role | How |
|------|------|-----|
| `bgo` | Admin | SSH login, runs install script, `sudo su - gpsops` for admin tasks |
| `gpsops` | Service | Owns everything: repos, venv, config, data. Runs scheduler via systemd |

**Key principle:** `bgo` administers by becoming `gpsops` via `sudo su - gpsops`. The scheduler runs as `gpsops` and reads config from its own home directory — no special environment variables needed.

### File Layout

```
/home/gpsops/
├── git/
│   ├── receivers/           # Main package (public, cloned as gpsops)
│   ├── gtimes/              # GPS time library (public)
│   ├── gps_parser/          # Config management (public)
│   ├── gps-config-data/     # Station configs (internal, from git.vedur.is)
│   └── gps-tools/           # Proprietary binaries (internal)
├── venv/                    # Python virtual environment
├── .config/gpsconfig/       # Configuration (stations.cfg, receivers.cfg, etc.)
├── .cache/gps_receivers/    # Logs, scheduler DB, tmp
│   ├── logs/
│   │   ├── receivers.log    # JSON rotating log
│   │   └── download_audit.jsonl
│   └── tmp/
└── .ssh/                    # SSH key for rsync to production archive

/usr/local/bin/receivers     # Symlink — CLI available to all users
/mnt/gpsdata/                # Local working data (owned by gpsops)
/mnt/rawgpsdata/             # NFS mount to production archive (read-only)
```

## What the Install Does

### Phase 1: System packages
PostgreSQL, Python 3, Git, NFS client, Docker.

### Phase 2: Users + directories
- Creates `gpsops` user with home directory `/home/gpsops/`
- Creates `gpsops` group (if not from AD/LDAP), adds `bgo` to it
- Creates directory structure under `/home/gpsops/`
- Creates `/mnt/gpsdata/` (local data), `/mnt/rawgpsdata/` (NFS archive)
- Adds NFS fstab entry for production archive
- Generates SSH key for `gpsops` (for rsync to production archive)

### Phase 3: Git repositories
Public repos (receivers, gtimes, gps_parser) are cloned as `gpsops` via HTTPS (no auth needed). Internal repos (gps-config-data from git.vedur.is) are cloned as `bgo` then ownership is transferred to `gpsops`.

### Phase 4: Python virtual environment
Creates `/home/gpsops/venv/`, installs all packages in editable mode. Symlinks `receivers` CLI to `/usr/local/bin/` so all users can run it.

### Phase 5: Configuration
Copies configs from gps-config-data to `/home/gpsops/.config/gpsconfig/`, patches:
- `database.cfg`: host=localhost, user=gpsops, mirror_host=pgdev.vedur.is, mirror_user=bgo
- `receivers.cfg`: data_prepath=/mnt/gpsdata/

### Phase 6: PostgreSQL
Creates roles (`bgo` superuser, `gpsops` owner), database `gps_health`, configures auth (peer + trust for localhost), disables JIT.

### Phase 7: Migrations
Runs `000_consolidated_schema.sql` on fresh DB, then applies pending migrations. Grants `gpsops` full access to all tables (migrations run as `bgo`).

### Phase 8: External tools
Clones `gps/gps-tools` from git.vedur.is (RxTools, teqc, gfzrnx). Symlinks binaries to `/usr/local/bin/`.

### Phase 9: Docker + Grafana + Trimble converter
Starts Grafana on port 3000 with auto-provisioned dashboards and PostgreSQL datasource. Pulls the `trm2rinex:cli-light` Docker image (~2.4 GB) for native Trimble RINEX 3 conversion.

### Phase 10: systemd
Installs and enables `gps-receivers-scheduler.service`, configures logrotate. Patches paths in the service file to match the installation.

### Phase 11: Verification
Checks CLI, config files, database, tools, Grafana. Prints summary.

## Day-to-Day Operations

### Becoming gpsops

All admin tasks are done as `gpsops`:

```bash
sudo su - gpsops
# Now you're gpsops — receivers, pip, git all work naturally
```

### Updating Code

```bash
sudo su - gpsops -c 'cd ~/git/receivers && git pull && ~/venv/bin/pip install -e .'
sudo systemctl restart gps-receivers-scheduler
```

### Updating Configuration

```bash
# Pull latest config (may need bgo credentials for git.vedur.is)
sudo su - gpsops -c 'cd ~/git/gps-config-data && git pull && cp stations.cfg receivers.cfg database.cfg scheduler.yaml ~/.config/gpsconfig/'

# Station config changes are auto-detected (no restart needed)
# For scheduler.yaml changes:
sudo systemctl restart gps-receivers-scheduler
```

### Running Migrations

```bash
cd /home/gpsops/git/receivers
psql -d gps_health -f migrations/NNN_whatever.sql
# Grant gpsops access to any new tables:
psql -d gps_health -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO gpsops"
sudo systemctl restart gps-receivers-scheduler
```

Or re-run the install script (auto-detects pending migrations):
```bash
sudo bash ~/git/receivers/deployment/server/install.sh
```

### Manual Downloads

```bash
# As gpsops:
sudo su - gpsops -c 'receivers download ELDC --sync --archive'

# Test connection:
sudo su - gpsops -c 'receivers download ELDC --test-connection'

# Health check:
sudo su - gpsops -c 'receivers health THOB --verbose'

# Or become gpsops interactively:
sudo su - gpsops
receivers download ELDC --sync --archive
receivers health THOB --verbose
exit
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
sudo su - gpsops -c 'receivers scheduler status --show-jobs'
```

### Grafana

```bash
# Restart (picks up dashboard JSON changes)
docker restart gps-grafana

# Full restart
cd /home/gpsops/git/receivers/deployment/server
docker compose down && docker compose up -d
```

Access at `http://<server-ip>:3000` (anonymous viewer access enabled).

## Wiping / Reinstalling

```bash
# Wipe venv + redeploy config (keep data and database)
sudo bash /home/gpsops/git/receivers/deployment/server/install.sh --wipe

# Drop database only (keeps data files)
sudo bash /home/gpsops/git/receivers/deployment/server/install.sh --wipe-db

# Full wipe: drop DB + delete data + reinstall everything
sudo bash /home/gpsops/git/receivers/deployment/server/install.sh --wipe-all
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
sudo su - gpsops -c 'receivers rinex MANA --native-trimble -d 1'
```

The fallback chain (`runpkr00` → `teqc` → `gfzrnx`) produces reformatted RINEX 3 (not native observation codes). Use only when Docker is unavailable.

## External Tools (RxTools, gfzrnx)

Proprietary tools are managed via the `gps/gps-tools` repo on git.vedur.is:

```
/home/gpsops/git/gps-tools/
├── rxtools/
│   ├── bin/        # bin2asc, sbf2rin, sbfanalyzer
│   └── lib/        # Qt6, libcomms, libgeod shared libraries
└── bin/            # teqc, gfzrnx, RNX2CRX, runpkr00, mdb2rinex
```

The install script symlinks these to `/usr/local/bin/` and configures `ld.so.conf` for RxTools shared libraries.

## Data Storage

| Location | Purpose | Access |
|----------|---------|--------|
| `/mnt/gpsdata/` | Local working data (downloads, processing) | Read/Write, local disk |
| `/mnt/rawgpsdata/` | Production archive (read-only reference) | Read-only, NFS from ananas.vedur.is |
| `rawdata.vedur.is` | Production archive (write target) | rsync over SSH as gpsops |

## Dual-Database Write

The scheduler writes to two databases simultaneously:
- **Primary**: localhost (local PostgreSQL on dev server)
- **Mirror**: pgdev.vedur.is (external DB that grafana.vedur.is reads)

The mirror authenticates as `bgo` (LDAP) since `gpsops` doesn't exist on pgdev. Mirror failures are logged but don't affect the primary. Configured via `mirror_host` and `mirror_user` in `database.cfg`.

## Troubleshooting

### Service won't start

```bash
journalctl -u gps-receivers-scheduler -n 50 --no-pager

# Common causes:
# - PostgreSQL not running: sudo systemctl start postgresql
# - Config missing: ls -la /home/gpsops/.config/gpsconfig/
# - Permission denied: check file ownership
```

### Database connection fails

```bash
# Test as gpsops
sudo -u gpsops psql -d gps_health -c "SELECT 1"

# Check pg_hba.conf
sudo -u postgres psql -c "SHOW hba_file"
```

### NFS mount issues

```bash
mountpoint /mnt/rawgpsdata
sudo mount /mnt/rawgpsdata
ping -c 1 ananas.vedur.is
```

### Grafana shows no data

1. Check scheduler is running and data appears in DB:
   ```bash
   psql -d gps_health -c "SELECT count(*) FROM block_health_summary WHERE ts > now() - interval '10 minutes'"
   ```
2. Check Grafana datasource: Settings → Data Sources → Test
3. Restart: `docker restart gps-grafana`

### External tools missing

```bash
which bin2asc sbf2rin teqc gfzrnx RNX2CRX

# If RxTools fails with shared library errors
ldd /home/gpsops/git/gps-tools/rxtools/bin/bin2asc
sudo ldconfig
```

---

**Maintainer**: Veðurstofa Íslands GPS Team
**Last updated**: 2026-03-19
