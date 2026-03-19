#!/bin/bash
# ===========================================================================
# GPS Receivers Scheduler — Dev/Production Server Installation
# Veðurstofa Íslands
#
# Usage:
#   sudo bash deployment/server/install.sh              # Fresh install or update
#   sudo bash deployment/server/install.sh --wipe       # Wipe venv + redeploy config
#   sudo bash deployment/server/install.sh --wipe-all   # Drop DB + delete data + reinstall
#   sudo bash deployment/server/install.sh --wipe-db    # Drop and recreate database only
#
# Everything lives under /home/gpsops/:
#   git/         — source repos (receivers, gtimes, gps_parser, gps-config-data)
#   venv/        — Python virtual environment
#   .config/gpsconfig/  — configuration files
#   .cache/gps_receivers/ — logs, scheduler DB, tmp
#
# Public repos are cloned as gpsops (HTTPS, no auth).
# Internal repos (git.vedur.is) are cloned as bgo then copied.
# The 'receivers' CLI is symlinked to /usr/local/bin/ for all users.
# ===========================================================================

set -euo pipefail

# ── Constants ──────────────────────────────────────────────────────────────
readonly SERVICE_USER="gpsops"
readonly SERVICE_GROUP="gpsops"
readonly ADMIN_USER="bgo"

readonly GPSOPS_HOME="/home/$SERVICE_USER"
readonly GIT_BASE="$GPSOPS_HOME/git"
readonly INSTALL_DIR="$GIT_BASE/receivers"
readonly GTIMES_DIR="$GIT_BASE/gtimes"
readonly GPS_PARSER_DIR="$GIT_BASE/gps_parser"
readonly CONFIG_REPO_DIR="$GIT_BASE/gps-config-data"
readonly TOOLS_DIR="$GIT_BASE/gps-tools"
readonly CONFIG_DIR="$GPSOPS_HOME/.config/gpsconfig"
readonly CACHE_DIR="$GPSOPS_HOME/.cache/gps_receivers"
readonly VENV_DIR="$GPSOPS_HOME/venv"
readonly DATA_DIR="/mnt/gpsdata"
readonly NFS_MOUNT="/mnt/rawgpsdata"
readonly DB_NAME="gps_health"

readonly NFS_SOURCE="ananas.vedur.is:/gps/gpsdata"
readonly NFS_OPTS="mountvers=3,auto,nofail,nolock,tcp,ro"

# Git repositories — public (HTTPS, no auth needed)
readonly REPO_RECEIVERS="https://github.com/bennigo/receivers.git"
readonly REPO_GTIMES="https://github.com/bennigo/gtimes.git"
readonly REPO_GPS_PARSER="https://github.com/bennigo/gps_parser.git"
# Internal (requires bgo's LDAP credentials)
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
            echo "  --wipe         Wipe venv + redeploy config (keep data + DB)"
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
echo "  OS:       $(lsb_release -ds 2>/dev/null || grep PRETTY_NAME /etc/os-release | cut -d= -f2)"
echo "  Wipe:     $FLAG_WIPE  Wipe-all: $FLAG_WIPE_ALL  Wipe-db: $FLAG_WIPE_DB"

# ── Handle wipe modes ────────────────────────────────────────────────────
if $FLAG_WIPE_ALL; then
    echo ""
    echo -e "${RED}WARNING: --wipe-all will DROP the database and delete $DATA_DIR/*${NC}"
    read -p "  Type 'yes' to confirm: " confirm
    if [[ "$confirm" != "yes" ]]; then echo "Aborted."; exit 1; fi
    systemctl stop gps-receivers-scheduler 2>/dev/null || true
    sudo -u postgres dropdb --if-exists "$DB_NAME"
    rm -rf "$VENV_DIR"
    rm -rf "$DATA_DIR"/*
    ok "Wipe-all complete, proceeding with fresh install"
elif $FLAG_WIPE_DB; then
    echo ""
    echo -e "${RED}WARNING: --wipe-db will DROP and recreate the $DB_NAME database${NC}"
    read -p "  Type 'yes' to confirm: " confirm
    if [[ "$confirm" != "yes" ]]; then echo "Aborted."; exit 1; fi
    systemctl stop gps-receivers-scheduler 2>/dev/null || true
    sudo -u postgres dropdb --if-exists "$DB_NAME"
    ok "Database dropped, will be recreated in Phase 6"
elif $FLAG_WIPE; then
    systemctl stop gps-receivers-scheduler 2>/dev/null || true
    rm -rf "$VENV_DIR"
    ok "Venv wiped, will be recreated in Phase 4"
fi

# ===========================================================================
# Phase 1: System packages
# ===========================================================================
phase 1 "System packages"

PACKAGES=(
    postgresql postgresql-contrib libpq-dev
    python3 python3-pip python3-venv python3-dev
    git jq curl wget
    nfs-common
)

MISSING=()
for pkg in "${PACKAGES[@]}"; do
    if ! dpkg -l "$pkg" 2>/dev/null | grep -q '^ii'; then
        MISSING+=("$pkg")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "  Installing: ${MISSING[*]}"
    # Disable third-party PostgreSQL repos that may not support this Ubuntu release
    for f in /etc/apt/sources.list.d/*pgdg* /etc/apt/sources.list.d/*postgresql*; do
        [[ -f "$f" ]] && mv "$f" "${f}.disabled" && warn "Disabled $(basename "$f")"
    done
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING[@]}"
    ok "Installed ${#MISSING[@]} packages"
else
    ok "All system packages already installed"
fi

# Docker
if ! $FLAG_SKIP_DOCKER; then
    if ! command -v docker &>/dev/null; then
        echo "  Installing Docker..."
        if [[ -f /etc/apt/sources.list.d/docker.list ]]; then
            apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
        else
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

# Create service user with a real home directory
if ! id -u "$SERVICE_USER" &>/dev/null; then
    useradd --system --create-home --home-dir "$GPSOPS_HOME" \
            --shell /bin/bash --comment "GPS Receivers Service" "$SERVICE_USER"
    ok "Created user: $SERVICE_USER (home: $GPSOPS_HOME)"
else
    ok "User exists: $SERVICE_USER"
    # Ensure home directory exists (AD/LDAP users may not have one yet)
    if [[ ! -d "$GPSOPS_HOME" ]]; then
        mkdir -p "$GPSOPS_HOME"
        chown "$SERVICE_USER":"$(id -gn "$SERVICE_USER")" "$GPSOPS_HOME"
        ok "Created home directory: $GPSOPS_HOME"
    fi
fi

# Create service group if it doesn't exist (AD/LDAP environments)
if ! getent group "$SERVICE_GROUP" &>/dev/null; then
    groupadd "$SERVICE_GROUP"
    usermod -aG "$SERVICE_GROUP" "$SERVICE_USER"
    ok "Created group: $SERVICE_GROUP"
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

# Add service user to docker group
if getent group docker &>/dev/null; then
    if ! id -nG "$SERVICE_USER" 2>/dev/null | grep -qw docker; then
        usermod -aG docker "$SERVICE_USER"
        ok "Added $SERVICE_USER to docker group"
    fi
fi

# Create directory structure under gpsops home
sudo -u "$SERVICE_USER" mkdir -p "$GIT_BASE"
sudo -u "$SERVICE_USER" mkdir -p "$CONFIG_DIR"
sudo -u "$SERVICE_USER" mkdir -p "$CACHE_DIR"/{logs,tmp}

# Data directories (system-level)
mkdir -p "$DATA_DIR"
chown "$SERVICE_USER":"$SERVICE_GROUP" "$DATA_DIR"
chmod 755 "$DATA_DIR"

mkdir -p "$NFS_MOUNT"

ok "Directory structure ready"

# NFS mount for production archive
if ! grep -q "$NFS_MOUNT" /etc/fstab 2>/dev/null; then
    echo "$NFS_SOURCE $NFS_MOUNT nfs $NFS_OPTS 0 0" >> /etc/fstab
    ok "Added NFS entry to fstab"
fi
if ! mountpoint -q "$NFS_MOUNT" 2>/dev/null; then
    mount "$NFS_MOUNT" 2>/dev/null && ok "NFS mounted" || \
        warn "NFS mount failed (nofail allows boot without archive server)"
fi

# SSH key for gpsops (rsync to rawdata.vedur.is)
GPSOPS_SSH_DIR="$GPSOPS_HOME/.ssh"
if [[ ! -f "$GPSOPS_SSH_DIR/id_ed25519" ]]; then
    sudo -u "$SERVICE_USER" mkdir -p "$GPSOPS_SSH_DIR"
    sudo -u "$SERVICE_USER" ssh-keygen -t ed25519 -N "" -C "gpsops@$(hostname)" \
        -f "$GPSOPS_SSH_DIR/id_ed25519" >/dev/null
    chmod 700 "$GPSOPS_SSH_DIR"
    chmod 600 "$GPSOPS_SSH_DIR/id_ed25519"
    ok "Generated SSH key for $SERVICE_USER"
    warn "Authorize on rawdata.vedur.is:"
    echo "    $(cat "$GPSOPS_SSH_DIR/id_ed25519.pub")"
else
    ok "SSH key exists for $SERVICE_USER"
fi

# ===========================================================================
# Phase 3: Git repositories
# ===========================================================================
phase 3 "Git repositories"

# Clone or update a public repo as gpsops (no auth needed)
clone_public() {
    local repo_url="$1" target_dir="$2"
    if [[ ! -d "$target_dir/.git" ]]; then
        echo "  Cloning $repo_url"
        sudo -u "$SERVICE_USER" git clone "$repo_url" "$target_dir" 2>&1 | tail -1
        ok "Cloned $(basename "$target_dir")"
    else
        cd "$target_dir"
        sudo -u "$SERVICE_USER" git pull --ff-only 2>&1 | tail -1 || \
            warn "git pull failed for $(basename "$target_dir")"
        ok "Updated $(basename "$target_dir")"
    fi
}

# Clone or update an internal repo (clone as bgo, copy to gpsops location)
clone_internal() {
    local repo_url="$1" target_dir="$2"
    local tmp_dir="/tmp/_gps_clone_$(basename "$target_dir")"

    if [[ ! -d "$target_dir/.git" ]]; then
        echo "  Cloning $repo_url (as $ADMIN_USER)"
        rm -rf "$tmp_dir"
        if sudo -u "$ADMIN_USER" git clone "$repo_url" "$tmp_dir" 2>&1 | tail -1; then
            mv "$tmp_dir" "$target_dir"
            chown -R "$SERVICE_USER":"$SERVICE_GROUP" "$target_dir"
            ok "Cloned $(basename "$target_dir")"
        else
            warn "Clone failed for $(basename "$target_dir") — may need $ADMIN_USER credentials"
            rm -rf "$tmp_dir"
        fi
    else
        # Update: pull as bgo in a temp copy, rsync changes
        cd "$target_dir"
        # Try pulling directly (may work if git stores credentials)
        if sudo -u "$SERVICE_USER" git pull --ff-only 2>&1 | tail -1; then
            ok "Updated $(basename "$target_dir")"
        else
            warn "git pull failed for $(basename "$target_dir") — may need manual update"
        fi
    fi
}

clone_public "$REPO_RECEIVERS"  "$INSTALL_DIR"
clone_public "$REPO_GTIMES"     "$GTIMES_DIR"
clone_public "$REPO_GPS_PARSER" "$GPS_PARSER_DIR"
clone_internal "$REPO_CONFIG"   "$CONFIG_REPO_DIR"

# ===========================================================================
# Phase 4: Python virtual environment
# ===========================================================================
phase 4 "Python virtual environment"

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "  Python: $PYTHON_VERSION"

if [[ ! -d "$VENV_DIR" ]]; then
    sudo -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
    ok "Created venv"
else
    ok "Venv exists"
fi

# Upgrade pip + install packages (editable mode for easy updates)
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel -q
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -e "$GTIMES_DIR" -q
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -e "$GPS_PARSER_DIR" -q
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -e "$INSTALL_DIR" -q
ok "Packages installed"

# Symlink receivers CLI to /usr/local/bin/ for all-user access
ln -sf "$VENV_DIR/bin/receivers" /usr/local/bin/receivers
ok "receivers CLI available system-wide (/usr/local/bin/receivers)"

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

# Copy config files from gps-config-data to gpsops config dir
CONFIG_FILES=(stations.cfg receivers.cfg postprocess.cfg scheduler.yaml database.cfg icinga.cfg)
for f in "${CONFIG_FILES[@]}"; do
    src="$CONFIG_REPO_DIR/$f"
    dst="$CONFIG_DIR/$f"
    if [[ -f "$src" ]]; then
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

# Set ownership — gpsops owns config, group-readable for bgo
chown "$SERVICE_USER":"$SERVICE_GROUP" "$CONFIG_DIR"/*
chmod 640 "$CONFIG_DIR"/*

# Patch database.cfg for local PostgreSQL + mirror
if [[ -f "$CONFIG_DIR/database.cfg" ]]; then
    sed -i 's/^host\s*=.*/host = localhost/' "$CONFIG_DIR/database.cfg"
    sed -i "s/^user\s*=.*/user = $SERVICE_USER/" "$CONFIG_DIR/database.cfg"
    # Mirror writes to external DB (grafana.vedur.is reads from it)
    if ! grep -q '^mirror_host' "$CONFIG_DIR/database.cfg"; then
        sed -i '/^\[postgresql\]/a mirror_host = pgdev.vedur.is' "$CONFIG_DIR/database.cfg"
    fi
    # Mirror authenticates as bgo (LDAP auth on pgdev)
    if ! grep -q '^mirror_user' "$CONFIG_DIR/database.cfg"; then
        sed -i '/^mirror_host/a mirror_user = bgo' "$CONFIG_DIR/database.cfg"
    fi
    ok "Patched database.cfg (localhost/$SERVICE_USER, mirror=pgdev.vedur.is/bgo)"
fi

# Patch receivers.cfg for server paths
if [[ -f "$CONFIG_DIR/receivers.cfg" ]]; then
    sed -i "s|^data_prepath\s*=.*|data_prepath = $DATA_DIR/|" "$CONFIG_DIR/receivers.cfg"
    sed -i "s|^tmp_dir\s*=.*|tmp_dir = $CACHE_DIR/tmp/|" "$CONFIG_DIR/receivers.cfg"
    ok "Patched receivers.cfg (data_prepath=$DATA_DIR/)"
fi

# ===========================================================================
# Phase 6: PostgreSQL database setup
# ===========================================================================
if ! $FLAG_SKIP_DB; then
phase 6 "PostgreSQL database"

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

# Create database owned by service user
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_catalog.pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    sudo -u postgres createdb -O "$SERVICE_USER" "$DB_NAME"
    ok "Created database: $DB_NAME"
else
    ok "Database exists: $DB_NAME"
    sudo -u postgres psql -c "ALTER DATABASE $DB_NAME OWNER TO $SERVICE_USER" 2>/dev/null
fi

# Disable JIT (adds overhead for small result sets — see migration 030 notes)
sudo -u postgres psql -c "ALTER DATABASE $DB_NAME SET jit = off" 2>/dev/null
ok "JIT disabled for $DB_NAME"

# Configure pg_hba.conf
PG_HBA=$(sudo -u postgres psql -tAc "SHOW hba_file")
if [[ -f "$PG_HBA" ]]; then
    if ! grep -q "$DB_NAME" "$PG_HBA" 2>/dev/null; then
        sed -i "/^# TYPE/a\\
# GPS health monitoring\\
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
    warn "Skipping database setup (--skip-db)"
fi

# ===========================================================================
# Phase 7: Database migrations
# ===========================================================================
if ! $FLAG_SKIP_DB; then
phase 7 "Database migrations"

MIGRATIONS_DIR="$INSTALL_DIR/migrations"

# Check if schema_migrations table exists
HAS_MIGRATIONS=$(sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -tAc \
    "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='schema_migrations')" 2>/dev/null || echo "f")

if [[ "$HAS_MIGRATIONS" != "t" ]]; then
    echo "  Fresh database, running consolidated schema..."
    sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -f "$MIGRATIONS_DIR/000_consolidated_schema.sql" -q
    ok "Consolidated schema applied (migrations 001-028 marked as done)"
fi

# Apply pending migrations
APPLIED=$(sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -tAc \
    "SELECT migration_name FROM schema_migrations ORDER BY migration_name" 2>/dev/null)

PENDING_COUNT=0
for migration_file in "$MIGRATIONS_DIR"/[0-9][0-9][0-9]_*.sql; do
    [[ ! -f "$migration_file" ]] && continue
    basename=$(basename "$migration_file" .sql)
    [[ "$basename" == *_rollback ]] && continue
    echo "$APPLIED" | grep -qx "$basename" && continue

    echo "  Applying: $basename"
    if sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -f "$migration_file" -q 2>&1; then
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

# Grant gpsops full access (migrations run as bgo, objects owned by bgo)
sudo -u "$ADMIN_USER" psql -d "$DB_NAME" -q <<GRANTS
GRANT ALL ON ALL TABLES IN SCHEMA public TO $SERVICE_USER;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $SERVICE_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $SERVICE_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $SERVICE_USER;
GRANTS
ok "Granted $SERVICE_USER full access to all database objects"

else
    warn "Skipping migrations (--skip-db)"
fi

# ===========================================================================
# Phase 8: External tools
# ===========================================================================
if ! $FLAG_SKIP_TOOLS; then
phase 8 "External tools"

# gps-tools repo (internal — clone as bgo, copy to gpsops)
if [[ ! -d "$TOOLS_DIR/.git" ]]; then
    echo "  Attempting to clone gps-tools..."
    clone_internal "$REPO_TOOLS" "$TOOLS_DIR"
else
    cd "$TOOLS_DIR"
    sudo -u "$SERVICE_USER" git pull --ff-only 2>/dev/null || true
    ok "gps-tools updated"
fi

# Symlink RxTools binaries
if [[ -d "$TOOLS_DIR/rxtools/bin" ]]; then
    for bin in bin2asc sbf2rin sbfanalyzer; do
        [[ -f "$TOOLS_DIR/rxtools/bin/$bin" ]] && ln -sf "$TOOLS_DIR/rxtools/bin/$bin" /usr/local/bin/
    done
    # RxTools shared libraries
    if [[ -d "$TOOLS_DIR/rxtools/lib" ]]; then
        echo "$TOOLS_DIR/rxtools/lib" > /etc/ld.so.conf.d/rxtools.conf
        ldconfig
    fi
    ok "RxTools symlinked"
fi

# Symlink other tools
for bin in teqc gfzrnx RNX2CRX CRX2RNX runpkr00 mdb2rinex; do
    [[ -f "$TOOLS_DIR/bin/$bin" ]] && ln -sf "$TOOLS_DIR/bin/$bin" /usr/local/bin/
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
    warn "Skipping external tools (--skip-tools)"
fi

# ===========================================================================
# Phase 9: Docker + Grafana + Trimble converter
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
        ok "Trimble converter already installed ($TRM_IMAGE)"
    else
        echo "  Pulling Trimble converter image (~2.4 GB)..."
        if docker pull "$TRM_SOURCE"; then
            docker tag "$TRM_SOURCE" "$TRM_IMAGE"
            ok "Trimble converter installed ($TRM_IMAGE)"
        else
            warn "Failed to pull trm2rinex image"
            warn "Manual: docker pull $TRM_SOURCE && docker tag $TRM_SOURCE $TRM_IMAGE"
        fi
    fi

    # Verify converter
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
    warn "Skipping Docker/Grafana/Trimble converter (--skip-docker)"
fi

# ===========================================================================
# Phase 10: systemd + logrotate
# ===========================================================================
phase 10 "systemd + logrotate"

# Install service file (patch paths for this installation)
sed -e "s|WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" \
    -e "s|ExecStart=.*/receivers |ExecStart=$VENV_DIR/bin/receivers |" \
    -e "s|ExecStop=.*/receivers |ExecStop=$VENV_DIR/bin/receivers |" \
    -e "s|ReadWritePaths=.*|ReadWritePaths=$CACHE_DIR $DATA_DIR /tmp|" \
    -e '/^Environment="GPS_CONFIG_PATH=/d' \
    -e '/^Environment="GPS_CACHE_DIR=/d' \
    "$INSTALL_DIR/deployment/systemd/gps-receivers-scheduler.service" \
    > /etc/systemd/system/gps-receivers-scheduler.service

systemctl daemon-reload
systemctl enable gps-receivers-scheduler
ok "systemd service installed and enabled"

# Logrotate
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

# CLI
if sudo -u "$SERVICE_USER" receivers --help &>/dev/null; then
    ok "receivers CLI (as $SERVICE_USER)"
else
    err "receivers CLI failed as $SERVICE_USER"
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
    ok "External tools: all present"
elif [[ $TOOL_COUNT -gt 0 ]]; then
    warn "External tools: $TOOL_COUNT/4 present"
else
    warn "External tools: none (install via gps-tools repo)"
fi

# Docker
if ! $FLAG_SKIP_DOCKER && command -v docker &>/dev/null; then
    if docker ps --format '{{.Names}}' | grep -q gps-grafana; then
        ok "Grafana: running on port 3000"
    else
        warn "Grafana: not running"
        WARNINGS=$((WARNINGS + 1))
    fi
    if docker image inspect trm2rinex:cli-light &>/dev/null; then
        ok "Trimble converter: installed"
    else
        warn "Trimble converter: not installed"
        WARNINGS=$((WARNINGS + 1))
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────
IP_ADDR=$(hostname -I 2>/dev/null | awk '{print $1}')

echo ""
echo -e "${BLUE}=== Installation Complete ===${NC}"
echo ""
echo "  Home:       $GPSOPS_HOME"
echo "  Config:     $CONFIG_DIR"
echo "  Data:       $DATA_DIR"
echo "  Logs:       $CACHE_DIR/logs/"
echo "  Venv:       $VENV_DIR"
echo "  NFS:        $NFS_MOUNT"
if [[ $WARNINGS -gt 0 ]]; then
    echo ""
    warn "$WARNINGS warnings — review output above"
fi
echo ""
echo "Day-to-day operations (as bgo):"
echo ""
echo "  # Become gpsops for admin tasks:"
echo "  sudo su - $SERVICE_USER"
echo ""
echo "  # Update code + reinstall:"
echo "  sudo su - $SERVICE_USER -c 'cd ~/git/receivers && git pull && ~/venv/bin/pip install -e .'"
echo ""
echo "  # Manual download:"
echo "  sudo su - $SERVICE_USER -c 'receivers download ELDC --sync --archive'"
echo ""
echo "  # Service management:"
echo "  sudo systemctl restart gps-receivers-scheduler"
echo "  journalctl -u gps-receivers-scheduler -f"
echo ""
echo "  # Grafana: http://${IP_ADDR:-<server-ip>}:3000"
echo ""
