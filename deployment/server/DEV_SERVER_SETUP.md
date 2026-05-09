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

# Production install (URL-pinned gtimes/gps_parser/tostools — default):
cd ~/git/receivers
sudo bash deployment/server/install.sh

# Dev install (editable sibling packages — `git pull` is live):
sudo bash deployment/server/install.sh --dev
```

The script is idempotent — running it again updates everything without breaking existing state.

### Dependency mode

`pyproject.toml` pins `gtimes`, `gps_parser`, and `tostools` via direct git URLs (e.g. `git+https://github.com/bennigo/gtimes@v0.5.0`). Two install modes:

| Mode | Behaviour | When |
|------|-----------|------|
| **Default** (URL-pinned) | `pip install -e receivers` resolves siblings from the URL pins. Siblings are *not* cloned to `~/git/`. | Production — reproducible, tied to release tags. |
| **`--dev`** | Siblings cloned to `~/git/{gtimes,gps_parser,tostools}` and installed editable. `git pull` in any sibling is immediately live (just restart the service). | Hacking on `gtimes` / `gps_parser` / `tostools` against receivers without release cadence. |

Switching modes later: re-run the install script with the opposite flag. The editable install force-replaces the URL-resolved install (and vice versa).

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
├── gtimes/                  # GPS time library       (only with --dev; otherwise URL-pinned)
├── gps_parser/              # Config management     (only with --dev; otherwise URL-pinned)
├── tostools/                # RINEX + archive utils (only with --dev; otherwise URL-pinned)
├── gps-config-data/         # Station configs (from git.vedur.is)
└── gps-tools/               # Proprietary binaries (from git.vedur.is)

/home/gpsops/
├── .config/gpsconfig/       # Config files (gpsops:gpsops 660, group-writable for admin)
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
All repos cloned as `bgo` via HTTPS into `~/git/`. Internal repos (gps-config-data, gps-tools from git.vedur.is) need bgo's LDAP credentials. Public sibling repos (gtimes, gps_parser, tostools) are cloned only in `--dev` mode; the default (URL-pinned) path lets pip fetch them directly from github at install time. All repos are made world-readable.

### Phase 4: Python virtual environment
Creates `~/git/receivers/venv/` owned by bgo. Always installs `receivers` editable — that's the package the scheduler runs. Sibling packages (`gtimes`, `gps_parser`, `tostools`) are either resolved from the pyproject.toml git-URL pins (default) or cloned + editable-installed (`--dev`). Symlinks `receivers` CLI to `/usr/local/bin/`. Verifies gpsops can execute it.

### Phase 5: Configuration
Copies configs from gps-config-data to `/home/gpsops/.config/gpsconfig/`, patches:
- `database.cfg`: host=localhost, user=gpsops, mirror_host=pgdev.vedur.is, mirror_user=bgo
- `receivers.cfg`: data_prepath=/mnt/data/gpsdata/

Config files are owned `gpsops:gpsops` with mode 660 (service user owns its own config; admin — via membership in the `gpsops` group — has group-write access for direct edits without `sudo -u gpsops`). This removes any hardcoded admin-user assumption from the software: only the service user name matters at install time.

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
# As bgo (owns code/venv) — no sudo needed:
cd ~/git/receivers && git pull
~/git/receivers/venv/bin/pip install -e .

# As gpsops (owns the user systemd unit) — restart to pick up the new code:
ssh gpsops@host 'systemctl --user restart gps-receivers-scheduler'
```

### Updating Configuration

`scheduler.yaml`, `stations.cfg`, `receivers.cfg` and friends are propagated to
`/home/gpsops/.config/gpsconfig/` automatically by `gps-config-sync.timer`
(runs every 10 min — see `deployment/server/sync-config.sh`). After a push to
`gps-config-data`, allow up to ~10 min for the file to land on the host, then:

```bash
# Station config changes are auto-detected (no restart needed)
# For scheduler.yaml changes:
ssh gpsops@host 'systemctl --user restart gps-receivers-scheduler'
```

### Running Migrations

```bash
# As bgo:
cd ~/git/receivers
psql -d gps_health -f migrations/NNN_whatever.sql
psql -d gps_health -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO gpsops"

# As gpsops:
ssh gpsops@host 'systemctl --user restart gps-receivers-scheduler'
```

Or re-run the install script (auto-detects pending migrations and restarts the
user unit via `gpsops_systemctl` wrapper):
```bash
sudo bash ~/git/receivers/deployment/server/install.sh
```

### Manual Downloads

```bash
# Run as gpsops directly (no sudo needed when SSH'd in as gpsops):
ssh gpsops@host 'receivers download ELDC --sync --archive'

# Test connection:
ssh gpsops@host 'receivers download ELDC --test-connection'

# Health check:
ssh gpsops@host 'receivers health THOB --verbose'
```

### Viewing Logs

```bash
# Systemd journal (live) — note --user-unit, no sudo
ssh gpsops@host 'journalctl --user-unit gps-receivers-scheduler -f'

# JSON log file (gpsops owns the cache dir; bgo can read via gpsops group)
tail -f /home/gpsops/.cache/gps_receivers/logs/receivers.log | jq .

# Audit trail
tail -f /home/gpsops/.cache/gps_receivers/logs/download_audit.jsonl | jq .
```

### Service Management

The scheduler is a **user-level systemd unit owned by `gpsops`** — never
`sudo systemctl ...`. Run as gpsops:

```bash
systemctl --user start   gps-receivers-scheduler
systemctl --user stop    gps-receivers-scheduler
systemctl --user restart gps-receivers-scheduler
systemctl --user status  gps-receivers-scheduler

# Check scheduler state
receivers scheduler status --show-jobs
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
# As gpsops (user unit logs are not visible to bgo without --user):
journalctl --user-unit gps-receivers-scheduler -n 50 --no-pager

# Common causes:
# - PostgreSQL not running: sudo systemctl start postgresql
# - Config missing: ls -la /home/gpsops/.config/gpsconfig/
# - Permission denied: check gpsops can read venv and config
# - Linger not enabled: loginctl enable-linger gpsops
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
