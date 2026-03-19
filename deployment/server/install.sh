#!/bin/bash
# ===========================================================================
# GPS Receivers Scheduler — Dev/Production Server Installation
# Veðurstofa Íslands
#
# Usage:
#   sudo ./deployment/server/install.sh                  # Fresh install or update
#   sudo ./deployment/server/install.sh --wipe           # Wipe venv + redeploy config
#   sudo ./deployment/server/install.sh --wipe-all       # Drop DB + delete data + reinstall
#   sudo ./deployment/server/install.sh --wipe-db        # Drop and recreate database only
#   sudo ./deployment/server/install.sh --skip-tools     # Skip external tool setup
#   sudo ./deployment/server/install.sh --skip-db        # Skip database setup
#   sudo ./deployment/server/install.sh --skip-docker    # Skip Docker/Grafana setup
#
# Run from the receivers repository root: cd /opt/receivers
# ===========================================================================

set -euo pipefail

# ── Constants ──────────────────────────────────────────────────────────────
readonly SERVICE_USER="gpsops"
readonly SERVICE_GROUP="gpsops"
readonly ADMIN_USER="bgo"

readonly GIT_BASE="/home/${ADMIN_USER}/git"
readonly INSTALL_DIR="${GIT_BASE}/receivers"
readonly GTIMES_DIR="${GIT_BASE}/gtimes"
readonly GPS_PARSER_DIR="${GIT_BASE}/gps_parser"
readonly CONFIG_REPO_DIR="${GIT_BASE}/gps-config-data"
readonly TOOLS_DIR="${GIT_BASE}/gps-tools"
readonly CONFIG_DIR="/etc/gpsconfig"
readonly CACHE_DIR="/var/cache/gps_receivers"
readonly DATA_DIR="/mnt/gpsdata"
readonly NFS_MOUNT="/mnt/rawgpsdata"
readonly VENV_DIR="$INSTALL_DIR/venv"
readonly DB_NAME="gps_health"

readonly NFS_SOURCE="ananas.vedur.is:/gps/gpsdata"
readonly NFS_OPTS="mountvers=3,auto,nofail,nolock,tcp,ro"

# Git repositories
readonly REPO_RECEIVERS="https://github.com/bennigo/receivers.git"
readonly REPO_GTIMES="https://github.com/bennigo/gtimes.git"
readonly REPO_GPS_PARSER="https://github.com/bennigo/gps_parser.git"
readonly REPO_CONFIG="https://git.vedur.is/bgo/gps-config-data.git"
readonly REPO_TOOLS="https://git.vedur.is/gps/gps-tools.git"

# ── Flags ──────────────────────────────────────────────────────────────────
FLAG_WIPE=false
FLAG_WIPE_ALL=false
FLAG_WIPE_DB=false
FLAG_SKIP_TOOLS=false
FLAG_SKIP_DB=false
FLAG_SKIP_DOCKER=false

# ── Color helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $*"; }
err()  { echo -e "  ${RED}✗${NC} $*"; }
phase(){ echo -e "\n${BLUE}━━━ Phase $1: $2 ━━━${NC}"; }

# ── Parse arguments ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --wipe)         FLAG_WIPE=true ;;
        --wipe-all)     FLAG_WIPE_ALL=true; FLAG_WIPE=true ;;
        --wipe-db)      FLAG_WIPE_DB=true ;;
        --skip-tools)   FLAG_SKIP_TOOLS=true ;;
        --skip-db)      FLAG_SKIP_DB=true ;;
        --skip-docker)  FLAG_SKIP_DOCKER=true ;;
        -h|--help)
            echo "Usage: $0 [--wipe] [--wipe-all] [--wipe-db] [--skip-tools] [--skip-db] [--skip-docker]"
            echo ""
            echo "  --wipe         Wipe venv + redeploy config + re-run migrations (keep data)"
            echo "  --wipe-all     Drop DB + delete data + full reinstall"
            echo "  --wipe-db      Drop and recreate database only"
            echo "  --skip-tools   Skip external tool installation (RxTools, teqc)"
            echo "  --skip-db      Skip database setup (for remote DB)"
            echo "  --skip-docker  Skip Docker/Grafana setup"
            exit 0
            ;;
        *) err "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# ── Pre-flight checks ────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (or with sudo)"
    exit 1
fi

echo -e "${BLUE}=== GPS Receivers Scheduler — Server Installation ===${NC}"
echo "  Host:     $(hostname)"
echo "  Date:     $(date -Iseconds)"
echo "  OS:       $(lsb_release -ds 2>/dev/null || cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2)"
echo "  Wipe:     $FLAG_WIPE  Wipe-all: $FLAG_WIPE_ALL  Wipe-db: $FLAG_WIPE_DB"

# ── Handle wipe modes ────────────────────────────────────────────────────
if $FLAG_WIPE_ALL; then
    echo ""
    echo -e "${RED}WARNING: --wipe-all will DROP the database and delete /mnt/gpsdata/*${NC}"
    read -p "  Type 'yes' to confirm: " confirm
    if [[ "$confirm" != "yes" ]]; then
        echo "Aborted."
        exit 1
    fi
    echo "  Stopping service..."
    systemctl stop gps-receivers-scheduler 2>/dev/null || true
    echo "  Dropping database..."
    sudo -u postgres dropdb --if-exists "$DB_NAME"
    echo "  Wiping venv..."
    rm -rf "$VENV_DIR"
    echo "  Wiping data..."
    rm -rf "$DATA_DIR"/*
    ok "Wipe-all complete, proceeding with fresh install"
elif $FLAG_WIPE_DB; then
    echo ""
    echo -e "${RED}WARNING: --wipe-db will DROP and recreate the $DB_NAME database${NC}"
    read -p "  Type 'yes' to confirm: " confirm
    if [[ "$confirm" != "yes" ]]; then
        echo "Aborted."
        exit 1
    fi
    systemctl stop gps-receivers-scheduler 2>/dev/null || true
    sudo -u postgres dropdb --if-exists "$DB_NAME"
    ok "Database dropped, will be recreated in Phase 6"
elif $FLAG_WIPE; then
    echo "  Stopping service..."
    systemctl stop gps-receivers-scheduler 2>/dev/null || true
    echo "  Wiping venv..."
    rm -rf "$VENV_DIR"
    ok "Venv wiped, will be recreated in Phase 4"
fi

# ===========================================================================
# Phase 1: System packages
# ===========================================================================
phase 1 "System packages"

# Build package list (only install what's missing)
PACKAGES=(
    postgresql postgresql-contrib libpq-dev
    python3 python3-pip python3-venv python3-dev
    git jq curl wget
    nfs-common
)

# Check which packages need installing
MISSING=()
for pkg in "${PACKAGES[@]}"; do
    if ! dpkg -l "$pkg" 2>/dev/null | grep -q '^ii'; then
        MISSING+=("$pkg")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "  Installing: ${MISSING[*]}"
    # Temporarily disable third-party PostgreSQL repos that may not support this Ubuntu release
    for f in /etc/apt/sources.list.d/*pgdg* /etc/apt/sources.list.d/*postgresql*; do
        [[ -f "$f" ]] && mv "$f" "${f}.disabled" && warn "Disabled $(basename "$f") (unsupported Ubuntu release)"
    done
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING[@]}"
    ok "Installed ${#MISSING[@]} packages"
else
    ok "All system packages already installed"
fi

# Docker — install if not present and not skipped
if ! $FLAG_SKIP_DOCKER; then
    if ! command -v docker &>/dev/null; then
        echo "  Installing Docker..."
        if [[ -f /etc/apt/sources.list.d/docker.list ]]; then
            apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
        else
            # Fallback to Ubuntu's docker.io
            apt-get install -y -qq docker.io docker-compose-v2 2>/dev/null || \
                apt-get install -y -qq docker.io
        fi
        systemctl enable --now docker
        ok "Docker installed"
    else
        ok "Docker already installed"
    fi
fi

# ===========================================================================
# Phase 2: Users + directories
# ===========================================================================
phase 2 "Users and directories"

# Create service user
if ! id -u "$SERVICE_USER" &>/dev/null; then
    useradd --system --home-dir /nonexistent --shell /bin/bash \
            --comment "GPS Receivers Service" "$SERVICE_USER"
    ok "Created user: $SERVICE_USER"
else
    ok "User exists: $SERVICE_USER"
fi

# Create service group if it doesn't exist (AD/LDAP users may not have a matching local group)
if ! getent group "$SERVICE_GROUP" &>/dev/null; then
    groupadd "$SERVICE_GROUP"
    usermod -aG "$SERVICE_GROUP" "$SERVICE_USER"
    ok "Created group: $SERVICE_GROUP (added $SERVICE_USER)"
else
    ok "Group exists: $SERVICE_GROUP"
fi

# Add admin to service group
if ! id -nG "$ADMIN_USER" 2>/dev/null | grep -qw "$SERVICE_GROUP"; then
    usermod -aG "$SERVICE_GROUP" "$ADMIN_USER"
    ok "Added $ADMIN_USER to $SERVICE_GROUP group"
else
    ok "$ADMIN_USER already in $SERVICE_GROUP group"
fi

# Add service user to docker group (needed for trm2rinex converter)
if getent group docker &>/dev/null; then
    if ! id -nG "$SERVICE_USER" 2>/dev/null | grep -qw docker; then
        usermod -aG docker "$SERVICE_USER"
        ok "Added $SERVICE_USER to docker group"
    else
        ok "$SERVICE_USER already in docker group"
    fi
fi

# Create directory structure
mkdir -p "$CONFIG_DIR"
mkdir -p "$CACHE_DIR"/{logs,tmp}
mkdir -p "$DATA_DIR"
mkdir -p "$NFS_MOUNT"

# Set ownership
chown root:${SERVICE_GROUP} "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"

chown -R ${SERVICE_USER}:${SERVICE_GROUP} "$CACHE_DIR"
chmod 700 "$CACHE_DIR"

chown ${SERVICE_USER}:${SERVICE_GROUP} "$DATA_DIR"
chmod 755 "$DATA_DIR"

ok "Directory structure ready"

# NFS mount
if ! grep -q "$NFS_MOUNT" /etc/fstab 2>/dev/null; then
    echo "$NFS_SOURCE $NFS_MOUNT nfs $NFS_OPTS 0 0" >> /etc/fstab
    ok "Added NFS entry to fstab"
fi

if ! mountpoint -q "$NFS_MOUNT" 2>/dev/null; then
    mount "$NFS_MOUNT" 2>/dev/null && ok "NFS mounted" || warn "NFS mount failed (archive server may be unreachable — nofail allows boot)"
fi

# SSH key for gpsops (rsync to rawdata.vedur.is)
GPSOPS_SSH_DIR="/home/$SERVICE_USER/.ssh"
if [[ ! -f "$GPSOPS_SSH_DIR/id_ed25519" ]]; then
    mkdir -p "$GPSOPS_SSH_DIR"
    ssh-keygen -t ed25519 -N "" -C "gpsops@$(hostname)" -f "$GPSOPS_SSH_DIR/id_ed25519" >/dev/null
    chown -R ${SERVICE_USER}:${SERVICE_GROUP} "$GPSOPS_SSH_DIR"
    chmod 700 "$GPSOPS_SSH_DIR"
    chmod 600 "$GPSOPS_SSH_DIR/id_ed25519"
    ok "Generated SSH key for $SERVICE_USER"
    warn "Public key (authorize on rawdata.vedur.is):"
    echo "    $(cat "$GPSOPS_SSH_DIR/id_ed25519.pub")"
else
    ok "SSH key exists for $SERVICE_USER"
fi

# ===========================================================================
# Phase 3: Clone/update git repositories
# ===========================================================================
phase 3 "Git repositories"

# Ensure base directory exists (owned by admin user)
sudo -u "$ADMIN_USER" mkdir -p "$GIT_BASE"

clone_or_update() {
    local repo_url="$1" target_dir="$2" owner="${3:-$ADMIN_USER}" group="${4:-$SERVICE_GROUP}"

    if [[ ! -d "$target_dir/.git" ]]; then
        echo "  Cloning $repo_url → $target_dir"
        sudo -u "$owner" git clone "$repo_url" "$target_dir" 2>&1 | tail -1
        ok "Cloned $(basename "$target_dir")"
    else
        echo "  Updating $target_dir..."
        cd "$target_dir"
        sudo -u "$owner" git pull --ff-only 2>&1 | tail -1 || warn "git pull failed for $(basename "$target_dir")"
        ok "Updated $(basename "$target_dir")"
    fi

    chown -R "${owner}:${group}" "$target_dir"
    chmod 755 "$target_dir"
}

clone_or_update "$REPO_RECEIVERS"  "$INSTALL_DIR"     "$ADMIN_USER" "$SERVICE_GROUP"
clone_or_update "$REPO_GTIMES"     "$GTIMES_DIR"      "$ADMIN_USER" "$SERVICE_GROUP"
clone_or_update "$REPO_GPS_PARSER" "$GPS_PARSER_DIR"   "$ADMIN_USER" "$SERVICE_GROUP"
clone_or_update "$REPO_CONFIG"     "$CONFIG_REPO_DIR"  "$ADMIN_USER" "$SERVICE_GROUP"

# ===========================================================================
# Phase 4: Python virtual environment
# ===========================================================================
phase 4 "Python virtual environment"

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "  Python: $PYTHON_VERSION"

if [[ ! -d "$VENV_DIR" ]]; then
    # Create venv dir owned by service user (inside bgo-owned repo)
    mkdir -p "$VENV_DIR"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$VENV_DIR"
    sudo -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
    ok "Created venv"
else
    ok "Venv exists"
fi

# Upgrade pip
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel -q

# Install packages (editable for easy updates)
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -e "$GTIMES_DIR" -q
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -e "$GPS_PARSER_DIR" -q
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -e "$INSTALL_DIR" -q
ok "Packages installed"

# Verify
if sudo -u "$SERVICE_USER" "$VENV_DIR/bin/receivers" --help &>/dev/null; then
    ok "receivers CLI works"
else
    err "receivers CLI failed — check pip install output"
    exit 1
fi

# ===========================================================================
# Phase 5: Configuration deployment
# ===========================================================================
phase 5 "Configuration"

# Copy config files from gps-config-data to /etc/gpsconfig/
CONFIG_FILES=(stations.cfg receivers.cfg postprocess.cfg scheduler.yaml database.cfg icinga.cfg)
for f in "${CONFIG_FILES[@]}"; do
    src="$CONFIG_REPO_DIR/$f"
    dst="$CONFIG_DIR/$f"
    if [[ -f "$src" ]]; then
        # Only overwrite if source is newer or file doesn't exist
        if [[ ! -f "$dst" ]] || [[ "$src" -nt "$dst" ]] || $FLAG_WIPE; then
            cp "$src" "$dst"
            ok "Deployed $f"
        else
            ok "$f unchanged"
        fi
    else
        warn "Not found in config repo: $f"
    fi
done

# Set ownership on all config files
chown root:${SERVICE_GROUP} "$CONFIG_DIR"/*
chmod 640 "$CONFIG_DIR"/*

# Patch database.cfg for local PostgreSQL
if [[ -f "$CONFIG_DIR/database.cfg" ]]; then
    # Set host=localhost, user=gpsops for local PostgreSQL
    sed -i 's/^host\s*=.*/host = localhost/' "$CONFIG_DIR/database.cfg"
    sed -i 's/^user\s*=.*/user = gpsops/' "$CONFIG_DIR/database.cfg"
    # Ensure mirror_host is set for dual-write
    if ! grep -q '^mirror_host' "$CONFIG_DIR/database.cfg"; then
        sed -i '/^\[postgresql\]/a mirror_host = pgdev.vedur.is' "$CONFIG_DIR/database.cfg"
    fi
    ok "Patched database.cfg (host=localhost, user=gpsops, mirror=pgdev.vedur.is)"
fi

# Patch receivers.cfg for server paths
if [[ -f "$CONFIG_DIR/receivers.cfg" ]]; then
    sed -i "s|^data_prepath\s*=.*|data_prepath = $DATA_DIR/|" "$CONFIG_DIR/receivers.cfg"
    sed -i "s|^tmp_dir\s*=.*|tmp_dir = $CACHE_DIR/tmp/|" "$CONFIG_DIR/receivers.cfg"
    ok "Patched receivers.cfg (data_prepath=$DATA_DIR/, tmp_dir=$CACHE_DIR/tmp/)"
fi

# ===========================================================================
# Phase 6: PostgreSQL database setup
# ===========================================================================
if ! $FLAG_SKIP_DB; then
phase 6 "PostgreSQL database"

# Ensure PostgreSQL is running
systemctl enable --now postgresql
ok "PostgreSQL running"

# Create database roles
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$SERVICE_USER'" | grep -q 1; then
    sudo -u postgres createuser "$SERVICE_USER"
    ok "Created PostgreSQL role: $SERVICE_USER"
else
    ok "PostgreSQL role exists: $SERVICE_USER"
fi

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$ADMIN_USER'" | grep -q 1; then
    sudo -u postgres createuser -s "$ADMIN_USER"
    ok "Created PostgreSQL superuser role: $ADMIN_USER"
else
    ok "PostgreSQL role exists: $ADMIN_USER"
fi

# Create database
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_catalog.pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    sudo -u postgres createdb -O "$SERVICE_USER" "$DB_NAME"
    ok "Created database: $DB_NAME"
else
    ok "Database exists: $DB_NAME"
    # Ensure ownership
    sudo -u postgres psql -c "ALTER DATABASE $DB_NAME OWNER TO $SERVICE_USER" 2>/dev/null
fi

# Disable JIT (performance fix — see memory notes)
sudo -u postgres psql -c "ALTER DATABASE $DB_NAME SET jit = off" 2>/dev/null
ok "JIT disabled for $DB_NAME"

# Configure pg_hba.conf for local access
PG_HBA=$(sudo -u postgres psql -tAc "SHOW hba_file")
if [[ -f "$PG_HBA" ]]; then
    # Add peer auth for gpsops and bgo if not present
    if ! grep -q "gps_health" "$PG_HBA" 2>/dev/null; then
        # Insert before the first existing rule
        sed -i "/^# TYPE/a\\
# GPS health monitoring - local peer auth\\
local   $DB_NAME    $SERVICE_USER                       peer\\
local   $DB_NAME    $ADMIN_USER                         peer\\
host    $DB_NAME    $SERVICE_USER   127.0.0.1/32        trust\\
host    $DB_NAME    $SERVICE_USER   ::1/128             trust\\
host    $DB_NAME    $ADMIN_USER     127.0.0.1/32        trust\\
host    $DB_NAME    $ADMIN_USER     ::1/128             trust" "$PG_HBA"
        systemctl reload postgresql
        ok "Configured pg_hba.conf (peer + trust for localhost)"
    else
        ok "pg_hba.conf already configured"
    fi
fi

# Verify connection
if sudo -u "$SERVICE_USER" psql -d "$DB_NAME" -c "SELECT 1" &>/dev/null; then
    ok "Database connection verified ($SERVICE_USER → $DB_NAME)"
else
    err "Cannot connect to database as $SERVICE_USER"
    err "Check pg_hba.conf: $PG_HBA"
    exit 1
fi

else
    echo ""
    warn "Skipping database setup (--skip-db)"
fi

# ===========================================================================
# Phase 7: Run migrations
# ===========================================================================
if ! $FLAG_SKIP_DB; then
phase 7 "Database migrations"

MIGRATIONS_DIR="$INSTALL_DIR/migrations"

# Check if schema_migrations table exists
HAS_MIGRATIONS=$(sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -tAc \
    "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='schema_migrations')" 2>/dev/null || echo "f")

if [[ "$HAS_MIGRATIONS" != "t" ]]; then
    # Fresh database — run consolidated schema
    echo "  Fresh database detected, running consolidated schema..."
    sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -f "$MIGRATIONS_DIR/000_consolidated_schema.sql" -q
    ok "Consolidated schema applied (migrations 001-028 marked as done)"
fi

# Apply any migrations not yet in schema_migrations
# Get list of applied migrations
APPLIED=$(sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -tAc \
    "SELECT migration_name FROM schema_migrations ORDER BY migration_name" 2>/dev/null)

# Find and apply pending migrations (skip rollbacks, skip 000 if already applied)
PENDING_COUNT=0
for migration_file in "$MIGRATIONS_DIR"/[0-9][0-9][0-9]_*.sql; do
    [[ ! -f "$migration_file" ]] && continue
    basename=$(basename "$migration_file" .sql)

    # Skip rollback files
    [[ "$basename" == *_rollback ]] && continue

    # Check if already applied
    if echo "$APPLIED" | grep -qx "$basename"; then
        continue
    fi

    echo "  Applying: $basename"
    if sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -f "$migration_file" -q 2>&1; then
        # Record migration if file doesn't self-record
        sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -c \
            "INSERT INTO schema_migrations (migration_name) VALUES ('$basename') ON CONFLICT DO NOTHING" -q
        ok "Applied $basename"
        PENDING_COUNT=$((PENDING_COUNT + 1))
    else
        err "Failed to apply $basename"
        exit 1
    fi
done

if [[ $PENDING_COUNT -eq 0 ]]; then
    TOTAL=$(echo "$APPLIED" | wc -l)
    ok "All migrations up to date ($TOTAL applied)"
else
    ok "Applied $PENDING_COUNT new migrations"
fi

else
    echo ""
    warn "Skipping migrations (--skip-db)"
fi

# ===========================================================================
# Phase 8: External tools
# ===========================================================================
if ! $FLAG_SKIP_TOOLS; then
phase 8 "External tools"

# Try to clone gps-tools repo (proprietary binaries)
if [[ ! -d "$TOOLS_DIR/.git" ]]; then
    echo "  Attempting to clone gps-tools repository..."
    if sudo -u "$ADMIN_USER" git clone "$REPO_TOOLS" "$TOOLS_DIR" 2>/dev/null; then
        chown -R ${ADMIN_USER}:${SERVICE_GROUP} "$TOOLS_DIR"
        chmod 750 "$TOOLS_DIR"
        ok "Cloned gps-tools"
    else
        warn "gps-tools repo not available — proprietary tools must be installed manually"
        warn "Expected location: $TOOLS_DIR with subdirs: rxtools/, bin/"
    fi
else
    cd "$TOOLS_DIR"
    sudo -u "$ADMIN_USER" git pull --ff-only 2>/dev/null || true
    ok "gps-tools updated"
fi

# Symlink RxTools binaries if available
if [[ -d "$TOOLS_DIR/rxtools/bin" ]]; then
    for bin in bin2asc sbf2rin sbfanalyzer; do
        if [[ -f "$TOOLS_DIR/rxtools/bin/$bin" ]]; then
            ln -sf "$TOOLS_DIR/rxtools/bin/$bin" /usr/local/bin/
        fi
    done
    # RxTools shared libraries
    RXTOOLS_LIB="$TOOLS_DIR/rxtools/lib"
    if [[ -d "$RXTOOLS_LIB" ]]; then
        echo "$RXTOOLS_LIB" > /etc/ld.so.conf.d/rxtools.conf
        ldconfig
    fi
    ok "RxTools symlinked"
fi

# Symlink other tools if available
for bin in teqc gfzrnx RNX2CRX CRX2RNX runpkr00 mdb2rinex; do
    if [[ -f "$TOOLS_DIR/bin/$bin" ]]; then
        ln -sf "$TOOLS_DIR/bin/$bin" /usr/local/bin/
    fi
done

# Report tool status
echo "  Tool availability:"
for tool in bin2asc sbf2rin teqc gfzrnx RNX2CRX runpkr00 mdb2rinex; do
    if command -v "$tool" &>/dev/null; then
        ok "$tool: $(which "$tool")"
    else
        warn "$tool: not found"
    fi
done

else
    echo ""
    warn "Skipping external tools (--skip-tools)"
fi

# ===========================================================================
# Phase 9: Docker + Grafana
# ===========================================================================
if ! $FLAG_SKIP_DOCKER; then
phase 9 "Docker + Grafana + Trimble converter"

if command -v docker &>/dev/null; then
    # ── Grafana ──
    COMPOSE_FILE="$INSTALL_DIR/deployment/server/docker-compose.yml"
    if [[ -f "$COMPOSE_FILE" ]]; then
        cd "$(dirname "$COMPOSE_FILE")"
        docker compose down 2>/dev/null || true
        docker compose up -d
        ok "Grafana started on port 3000"
    else
        warn "docker-compose.yml not found at $COMPOSE_FILE"
    fi

    # ── Trimble native RINEX 3 converter (trm2rinex) ──
    TRM_IMAGE="trm2rinex:cli-light"
    TRM_SOURCE="geodesyewsp/trm2rinex:cli-light"

    if docker image inspect "$TRM_IMAGE" &>/dev/null; then
        ok "Trimble converter image already installed ($TRM_IMAGE)"
    else
        echo "  Pulling Trimble converter image (~2.4 GB)..."
        if docker pull "$TRM_SOURCE"; then
            docker tag "$TRM_SOURCE" "$TRM_IMAGE"
            ok "Trimble converter installed ($TRM_IMAGE)"
        else
            warn "Failed to pull trm2rinex image — Trimble RINEX 3 conversion unavailable"
            warn "Manual install: docker pull $TRM_SOURCE && docker tag $TRM_SOURCE $TRM_IMAGE"
        fi
    fi

    # Verify converter works
    if docker image inspect "$TRM_IMAGE" &>/dev/null; then
        if docker run --rm --entrypoint="" "$TRM_IMAGE" \
            /opt/wine/bin/wine \
            "C:\\Program Files\\Trimble\\convertToRINEX\\convertToRinex.exe" \
            --help 2>&1 | grep -q "No input file specified"; then
            ok "Trimble convertToRinex verified"
        else
            warn "Trimble converter image exists but verification failed"
        fi
    fi
else
    warn "Docker not installed, skipping Grafana and Trimble converter"
fi

else
    echo ""
    warn "Skipping Docker/Grafana/Trimble converter (--skip-docker)"
fi

# ===========================================================================
# Phase 10: systemd + logrotate
# ===========================================================================
phase 10 "systemd + logrotate"

# Install service file (patch paths for this installation)
sed -e "s|/opt/receivers|$INSTALL_DIR|g" \
    "$INSTALL_DIR/deployment/systemd/gps-receivers-scheduler.service" \
    > /etc/systemd/system/gps-receivers-scheduler.service
systemctl daemon-reload
systemctl enable gps-receivers-scheduler
ok "systemd service installed and enabled"

# Install logrotate config
if [[ -f "$INSTALL_DIR/deployment/logrotate.d/gps-receivers" ]]; then
    cp "$INSTALL_DIR/deployment/logrotate.d/gps-receivers" /etc/logrotate.d/
    chmod 644 /etc/logrotate.d/gps-receivers
    ok "logrotate configured"
fi

# ===========================================================================
# Phase 11: Verification
# ===========================================================================
phase 11 "Verification"

WARNINGS=0

# CLI check
if sudo -u "$SERVICE_USER" \
    GPS_CONFIG_PATH="$CONFIG_DIR" \
    "$VENV_DIR/bin/receivers" --help &>/dev/null; then
    ok "receivers CLI"
else
    err "receivers CLI failed"
    WARNINGS=$((WARNINGS + 1))
fi

# Config files
for f in stations.cfg receivers.cfg scheduler.yaml database.cfg; do
    if [[ -f "$CONFIG_DIR/$f" ]]; then
        ok "Config: $f"
    else
        err "Missing: $CONFIG_DIR/$f"
        WARNINGS=$((WARNINGS + 1))
    fi
done

# Database
if ! $FLAG_SKIP_DB; then
    MIGRATION_COUNT=$(sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -tAc \
        "SELECT count(*) FROM schema_migrations" 2>/dev/null || echo "0")
    ok "Database: $MIGRATION_COUNT migrations applied"
fi

# External tools
TOOL_COUNT=0
for tool in bin2asc sbf2rin teqc gfzrnx; do
    command -v "$tool" &>/dev/null && TOOL_COUNT=$((TOOL_COUNT + 1))
done
if [[ $TOOL_COUNT -eq 4 ]]; then
    ok "External tools: all $TOOL_COUNT present"
elif [[ $TOOL_COUNT -gt 0 ]]; then
    warn "External tools: $TOOL_COUNT/4 present"
else
    warn "External tools: none found (install via gps-tools repo or manually)"
fi

# Docker services
if ! $FLAG_SKIP_DOCKER && command -v docker &>/dev/null; then
    if docker ps --format '{{.Names}}' | grep -q gps-grafana; then
        ok "Grafana: running on port 3000"
    else
        warn "Grafana: container not running"
        WARNINGS=$((WARNINGS + 1))
    fi
    if docker image inspect trm2rinex:cli-light &>/dev/null; then
        ok "Trimble converter: trm2rinex:cli-light installed"
    else
        warn "Trimble converter: image not installed (Trimble RINEX 3 unavailable)"
        WARNINGS=$((WARNINGS + 1))
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}=== Installation Complete ===${NC}"
echo ""
echo "  Config:     $CONFIG_DIR"
echo "  Data:       $DATA_DIR"
echo "  Cache/logs: $CACHE_DIR"
echo "  Venv:       $VENV_DIR"
echo "  NFS:        $NFS_MOUNT"
if [[ $WARNINGS -gt 0 ]]; then
    echo ""
    warn "$WARNINGS warnings — review output above"
fi
echo ""
echo "Next steps:"
echo "  1. Start the scheduler:"
echo "     sudo systemctl start gps-receivers-scheduler"
echo "     journalctl -u gps-receivers-scheduler -f"
echo ""
echo "  2. Test a download:"
echo "     sudo -u $SERVICE_USER GPS_CONFIG_PATH=$CONFIG_DIR \\"
echo "       $VENV_DIR/bin/receivers download ELDC --sync --archive --test-connection"
echo ""
echo "  3. Grafana: http://$(hostname -I | awk '{print $1}'):3000"
echo ""
echo "  4. Update code:"
echo "     cd $INSTALL_DIR && git pull"
echo "     sudo -u $SERVICE_USER $VENV_DIR/bin/pip install -e ."
echo "     sudo systemctl restart gps-receivers-scheduler"
echo ""
