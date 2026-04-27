#!/bin/bash
# ===========================================================================
# GPS Receivers Scheduler — Dev/Production Server Installation
# Veðurstofa Íslands
#
# Usage:
#   cd ~/git/receivers
#   sudo bash deployment/server/install.sh              # Fresh install or update (URL-pinned deps)
#   sudo bash deployment/server/install.sh --dev        # Editable installs of gtimes/gps_parser/tostools
#   sudo bash deployment/server/install.sh --wipe       # Wipe venv + redeploy config
#   sudo bash deployment/server/install.sh --wipe-all   # Drop DB + delete data + reinstall
#   sudo bash deployment/server/install.sh --wipe-db    # Drop and recreate database only
#
# Dependency modes:
#   Default (URL-pinned):  gtimes/gps_parser/tostools resolved from pyproject.toml git URLs
#                          (immutable, reproducible — best for production)
#   --dev                  gtimes/gps_parser/tostools cloned to ~/git/ and installed editable
#                          (for hacking on sibling packages live; `git pull` picks up changes)
#
# Layout:
#   /home/bgo/git/           — repos + venv (owned by bgo, world-readable)
#   /home/gpsops/.config/gpsconfig/ — config (bgo:gpsops, group-readable)
#   /home/gpsops/.cache/gps_receivers/ — logs, scheduler DB (owned by gpsops)
#   /mnt/gpsdata/            — working data (owned by gpsops)
#
# bgo owns repos + venv, does git pull + pip install directly.
# gpsops runs the scheduler, reads config from its own ~/.config/gpsconfig/.
# ===========================================================================

set -euo pipefail

# ── Constants ──────────────────────────────────────────────────────────────
readonly SERVICE_USER="gpsops"
readonly SERVICE_GROUP="gpsops"
readonly ADMIN_USER="bgo"

readonly ADMIN_HOME="/home/$ADMIN_USER"
readonly GPSOPS_HOME="/home/$SERVICE_USER"

# Repos + venv under bgo's home
readonly GIT_BASE="$ADMIN_HOME/git"
readonly INSTALL_DIR="$GIT_BASE/receivers"
readonly GTIMES_DIR="$GIT_BASE/gtimes"
readonly GPS_PARSER_DIR="$GIT_BASE/gps_parser"
readonly TOSTOOLS_DIR="$GIT_BASE/tostools"
readonly CONFIG_REPO_DIR="$GIT_BASE/gps-config-data"
readonly TOOLS_DIR="$GIT_BASE/gps-tools"
readonly VENV_DIR="$INSTALL_DIR/venv"

# Minimum Python version (see pyproject.toml requires-python)
readonly MIN_PYTHON_MAJOR=3
readonly MIN_PYTHON_MINOR=10

# gpsops owns config + cache + data
readonly CONFIG_DIR="$GPSOPS_HOME/.config/gpsconfig"
readonly CACHE_DIR="$GPSOPS_HOME/.cache/gps_receivers"
readonly DATA_DIR="/mnt/data/gpsdata"
readonly NFS_MOUNT="/mnt/rawgpsdata"
readonly DB_NAME="gps_health"

readonly NFS_SOURCE="ananas.vedur.is:/gps/gpsdata"
readonly NFS_OPTS="mountvers=3,auto,nofail,nolock,tcp,ro"

# Git repositories — public (HTTPS, no auth needed)
readonly REPO_GTIMES="https://github.com/bennigo/gtimes.git"
readonly REPO_GPS_PARSER="https://github.com/bennigo/gps_parser.git"
readonly REPO_TOSTOOLS="https://github.com/bennigo/tostools.git"
# Internal (requires bgo's LDAP credentials)
readonly REPO_CONFIG="https://git.vedur.is/bgo/gps-config-data.git"
readonly REPO_TOOLS="https://git.vedur.is/bgo/gps-tools.git"  # TODO: move to gps/gps-tools once IT grants write access

# ── Flags ──────────────────────────────────────────────────────────────────
FLAG_DEV=false
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
        --dev)          FLAG_DEV=true ;;
        --wipe)         FLAG_WIPE=true ;;
        --wipe-all)     FLAG_WIPE_ALL=true; FLAG_WIPE=true ;;
        --wipe-db)      FLAG_WIPE_DB=true ;;
        --skip-tools)   FLAG_SKIP_TOOLS=true ;;
        --skip-db)      FLAG_SKIP_DB=true ;;
        --skip-docker)  FLAG_SKIP_DOCKER=true ;;
        -h|--help)
            echo "Usage: $0 [--dev] [--wipe] [--wipe-all] [--wipe-db] [--skip-tools] [--skip-db] [--skip-docker]"
            echo ""
            echo "  --dev          Editable installs of gtimes/gps_parser/tostools"
            echo "                 (default: URL-pinned from pyproject.toml — production mode)"
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

# Verify we're running from the receivers repo
if [[ ! -f "$INSTALL_DIR/pyproject.toml" ]]; then
    err "receivers repo not found at $INSTALL_DIR"
    err "Clone it first: git clone https://github.com/bennigo/receivers.git $INSTALL_DIR"
    exit 1
fi

# Verify Python version (pyproject.toml requires-python = ">=3.10")
if ! command -v python3 &>/dev/null; then
    err "python3 not found"
    exit 1
fi
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
PY_MAJOR=${PYTHON_VERSION%%.*}
PY_MINOR=${PYTHON_VERSION##*.}
if (( PY_MAJOR < MIN_PYTHON_MAJOR )) || \
   (( PY_MAJOR == MIN_PYTHON_MAJOR && PY_MINOR < MIN_PYTHON_MINOR )); then
    err "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ required (found $PYTHON_VERSION)"
    err "Upgrade python3 (Ubuntu 22.04+ ships 3.10, 24.04 ships 3.12)"
    exit 1
fi

echo -e "${BLUE}=== GPS Receivers Scheduler — Server Installation ===${NC}"
echo "  Host:     $(hostname)"
echo "  Date:     $(date -Iseconds)"
echo "  OS:       $(lsb_release -ds 2>/dev/null || grep PRETTY_NAME /etc/os-release | cut -d= -f2)"
echo "  Python:   $PYTHON_VERSION"
echo "  Mode:     $($FLAG_DEV && echo 'dev (editable siblings)' || echo 'production (URL-pinned siblings)')"
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
    # Add admin user too — so `docker ps` / `docker logs` work without sudo.
    # Note: group membership only takes effect on next login (or `newgrp docker`).
    if ! id -nG "$ADMIN_USER" 2>/dev/null | grep -qw docker; then
        usermod -aG docker "$ADMIN_USER"
        ok "Added $ADMIN_USER to docker group (re-login needed to take effect)"
    fi
fi

# Ensure bgo's home + git dir are traversable by gpsops (for venv + editable installs)
chmod o+x "$ADMIN_HOME"
chmod o+x "$GIT_BASE"

# gpsops config + cache directories
sudo -u "$SERVICE_USER" mkdir -p "$CONFIG_DIR"
sudo -u "$SERVICE_USER" mkdir -p "$CACHE_DIR"/{logs,tmp}

# Allow $ADMIN_USER (and anyone else in $SERVICE_GROUP) to traverse into
# $GPSOPS_HOME and read the cache/logs tree. Two layers matter:
#
# 1. $GPSOPS_HOME itself is mode 700 by default on Ubuntu. chmod 750 allows
#    group traverse into /home/$SERVICE_USER/.
# 2. On LDAP-integrated systems (e.g. Veðurstofa), new users can inherit a
#    non-service primary group (e.g. starfsmenn). That makes files created
#    by $SERVICE_USER land with the wrong group. Force the cache tree's
#    group to $SERVICE_GROUP, and set the SGID bit on the cache dirs so
#    subsequently created files (log rotations, scheduler.db re-inits)
#    inherit the group too.
# 3. `$CACHE_DIR/..` (i.e. /home/$SERVICE_USER/.cache/) also gets created
#    with mode 700 — have to chmod it 750 separately or traversal fails
#    one level before the cache tree.
chgrp "$SERVICE_GROUP" "$GPSOPS_HOME"
chmod 750 "$GPSOPS_HOME"
# .cache parent (don't recurse — only adjust the traverse bit)
chgrp "$SERVICE_GROUP" "$(dirname "$CACHE_DIR")"
chmod 750 "$(dirname "$CACHE_DIR")"
# Full cache tree: group ownership + group-read + SGID on directories
chgrp -R "$SERVICE_GROUP" "$CACHE_DIR"
chmod -R g+rX "$CACHE_DIR"
find "$CACHE_DIR" -type d -exec chmod g+s {} \;

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

# All repos cloned/updated as bgo (HTTPS, public repos need no auth)
clone_or_update() {
    local repo_url="$1" target_dir="$2"
    if [[ ! -d "$target_dir/.git" ]]; then
        echo "  Cloning $repo_url"
        sudo -u "$ADMIN_USER" git clone "$repo_url" "$target_dir" 2>&1 | tail -1
        ok "Cloned $(basename "$target_dir")"
    else
        cd "$target_dir"
        sudo -u "$ADMIN_USER" git pull --ff-only 2>&1 | tail -1 || \
            warn "git pull failed for $(basename "$target_dir")"
        ok "Updated $(basename "$target_dir")"
    fi
    # Ensure world-readable so gpsops can access
    chmod -R o+rX "$target_dir"
}

# receivers is already cloned (we're running from it)
ok "receivers: $INSTALL_DIR"
chmod -R o+rX "$INSTALL_DIR"

# Sibling packages: cloned for editable installs in --dev mode only.
# In production mode, pyproject.toml's git-URL pins handle them.
if $FLAG_DEV; then
    clone_or_update "$REPO_GTIMES"     "$GTIMES_DIR"
    clone_or_update "$REPO_GPS_PARSER" "$GPS_PARSER_DIR"
    clone_or_update "$REPO_TOSTOOLS"   "$TOSTOOLS_DIR"
else
    ok "Siblings (gtimes/gps_parser/tostools): resolved from pyproject.toml git URLs"
fi

# Internal repo — may need bgo's LDAP credentials if not public.
# GIT_TERMINAL_PROMPT=0: fail fast instead of hanging at a username prompt.
# Cached credentials (~/.netrc, credential helper) still work if present.
if [[ ! -d "$CONFIG_REPO_DIR/.git" ]]; then
    echo "  Cloning gps-config-data..."
    if sudo -u "$ADMIN_USER" GIT_TERMINAL_PROMPT=0 git clone "$REPO_CONFIG" "$CONFIG_REPO_DIR" 2>&1 | tail -1; then
        chmod -R o+rX "$CONFIG_REPO_DIR"
        ok "Cloned gps-config-data"
    else
        warn "Clone failed — run manually: git clone $REPO_CONFIG $CONFIG_REPO_DIR"
    fi
else
    cd "$CONFIG_REPO_DIR"
    sudo -u "$ADMIN_USER" GIT_TERMINAL_PROMPT=0 git pull --ff-only 2>&1 | tail -1 || true
    chmod -R o+rX "$CONFIG_REPO_DIR"
    ok "Updated gps-config-data"
fi

# ===========================================================================
# Phase 4: Python virtual environment
# ===========================================================================
phase 4 "Python virtual environment"

echo "  Python: $PYTHON_VERSION"

if [[ ! -d "$VENV_DIR" ]]; then
    sudo -u "$ADMIN_USER" python3 -m venv "$VENV_DIR"
    ok "Created venv"
else
    ok "Venv exists"
fi

# Install packages as bgo (bgo owns the venv)
sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel -q

# Always install receivers first — this resolves the git-URL pins in
# pyproject.toml for gtimes/gps_parser/tostools (the production path).
sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" install -e "$INSTALL_DIR" -q

if $FLAG_DEV; then
    # Override the URL-resolved installs with editable siblings. `pip install -e`
    # force-replaces prior installs of the same package, so `git pull` in any
    # sibling dir becomes live without reinstall churn.
    sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" install -e "$GTIMES_DIR" -q
    sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" install -e "$GPS_PARSER_DIR" -q
    sudo -u "$ADMIN_USER" "$VENV_DIR/bin/pip" install -e "$TOSTOOLS_DIR" -q
    ok "Packages installed (receivers + editable gtimes/gps_parser/tostools)"
else
    ok "Packages installed (receivers + URL-pinned gtimes/gps_parser/tostools)"
fi

# Ensure venv is world-readable + executable
chmod -R o+rX "$VENV_DIR"

# Symlink receivers CLI to /usr/local/bin/ for all-user access
ln -sf "$VENV_DIR/bin/receivers" /usr/local/bin/receivers
ok "receivers CLI available system-wide (/usr/local/bin/receivers)"

# Verify CLI works as gpsops
if sudo -u "$SERVICE_USER" "$VENV_DIR/bin/receivers" --help &>/dev/null; then
    ok "receivers CLI works as $SERVICE_USER"
else
    err "receivers CLI failed as $SERVICE_USER — check permissions"
    exit 1
fi

# ===========================================================================
# Phase 5: Configuration deployment
# ===========================================================================
phase 5 "Configuration"

# Deploy config files: gps-config-data → package defaults → skip
DEFAULTS_DIR="$INSTALL_DIR/config/defaults"
CONFIG_FILES=(stations.cfg receivers.cfg scheduler.yaml database.cfg icinga.cfg)
for f in "${CONFIG_FILES[@]}"; do
    dst="$CONFIG_DIR/$f"
    src=""

    # Source priority: gps-config-data > package defaults
    if [[ -f "$CONFIG_REPO_DIR/$f" ]]; then
        src="$CONFIG_REPO_DIR/$f"
    elif [[ -f "$DEFAULTS_DIR/$f" ]]; then
        src="$DEFAULTS_DIR/$f"
    fi

    if [[ -n "$src" ]]; then
        if [[ ! -f "$dst" ]] || [[ "$src" -nt "$dst" ]] || $FLAG_WIPE; then
            cp "$src" "$dst"
            if [[ "$src" == "$DEFAULTS_DIR"* ]]; then
                ok "Deployed $f (from package defaults)"
            else
                ok "Deployed $f"
            fi
        else
            ok "$f unchanged"
        fi
    else
        warn "Not found: $f (not in config repo or package defaults)"
    fi
done

# Config owned by the service user (no admin-user assumption in software).
# Admin has write access via group membership: install.sh Phase 2 adds
# $ADMIN_USER to $SERVICE_GROUP, and the files are mode 660 (group-writable).
# This matches the Unix convention that files under /home/<user>/ belong to <user>.
chown ${SERVICE_USER}:${SERVICE_GROUP} "$CONFIG_DIR"
chown ${SERVICE_USER}:${SERVICE_GROUP} "$CONFIG_DIR"/*
chmod 770 "$CONFIG_DIR"
chmod 660 "$CONFIG_DIR"/*

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
    # Uncomment PolaRX5 TCP credentials (commented-out in package defaults for portability;
    # idempotent: sed is a no-op when lines are already uncommented)
    sed -i 's/^# tcp_username = /tcp_username = /' "$CONFIG_DIR/receivers.cfg"
    sed -i 's/^# tcp_password = /tcp_password = /' "$CONFIG_DIR/receivers.cfg"
    ok "Patched receivers.cfg (data_prepath=$DATA_DIR/, TCP auth activated)"
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

# gps-tools repo (git.vedur.is is IMO-internal; intended to be public-anon
# once populated — tracked as TODO on receivers project hub).
# GIT_TERMINAL_PROMPT=0 so we fail fast instead of prompting when the
# repo doesn't exist yet.
if [[ ! -d "$TOOLS_DIR/.git" ]]; then
    echo "  Cloning gps-tools..."
    if sudo -u "$ADMIN_USER" GIT_TERMINAL_PROMPT=0 git clone "$REPO_TOOLS" "$TOOLS_DIR" 2>/dev/null; then
        chmod -R o+rX "$TOOLS_DIR"
        ok "Cloned gps-tools"
    else
        warn "gps-tools not available — proprietary tools must be installed manually"
        warn "See docs/gps-tools-repo.md (or ask bgo) for how to populate it"
    fi
else
    cd "$TOOLS_DIR"
    sudo -u "$ADMIN_USER" GIT_TERMINAL_PROMPT=0 git pull --ff-only 2>/dev/null || true
    chmod -R o+rX "$TOOLS_DIR"
    ok "gps-tools updated"
fi

# Symlink RxTools binaries
# Septentrio bundles .so libraries alongside binaries in rxtools/bin/ (no
# separate lib/). Register bin/ with ld.so so the dynamic linker finds
# libcomms/libgeod/Qt6/etc. when the binaries are called via the
# /usr/local/bin/ symlinks.
if [[ -d "$TOOLS_DIR/rxtools/bin" ]]; then
    # sbfanalyzer is wrapped by runSbfanalyzer (Qt plugin path setup).
    # Symlink the wrapper, not the raw binary.
    [[ -f "$TOOLS_DIR/rxtools/bin/bin2asc" ]] && ln -sf "$TOOLS_DIR/rxtools/bin/bin2asc" /usr/local/bin/bin2asc
    [[ -f "$TOOLS_DIR/rxtools/bin/sbf2rin" ]] && ln -sf "$TOOLS_DIR/rxtools/bin/sbf2rin" /usr/local/bin/sbf2rin
    [[ -f "$TOOLS_DIR/rxtools/bin/runSbfanalyzer" ]] && ln -sf "$TOOLS_DIR/rxtools/bin/runSbfanalyzer" /usr/local/bin/sbfanalyzer
    echo "$TOOLS_DIR/rxtools/bin" > /etc/ld.so.conf.d/rxtools.conf
    ldconfig
    ok "RxTools symlinked (bin + ld.so path)"
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
    "$INSTALL_DIR/deployment/systemd/gps-receivers-scheduler.service" \
    > /etc/systemd/system/gps-receivers-scheduler.service

systemctl daemon-reload
systemctl enable --now gps-receivers-scheduler
ok "systemd service installed, enabled, and started"

# Logrotate
if [[ -f "$INSTALL_DIR/deployment/logrotate.d/gps-receivers" ]]; then
    # Patch log path to match this installation
    sed -e "s|/home/gpsops/.cache/gps_receivers|$CACHE_DIR|g" \
        "$INSTALL_DIR/deployment/logrotate.d/gps-receivers" \
        > /etc/logrotate.d/gps-receivers
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
echo "  Repos:      $GIT_BASE (owned by $ADMIN_USER)"
echo "  Venv:       $VENV_DIR (owned by $ADMIN_USER)"
echo "  Config:     $CONFIG_DIR (bgo:gpsops)"
echo "  Data:       $DATA_DIR (owned by $SERVICE_USER)"
echo "  Logs:       $CACHE_DIR/logs/ (owned by $SERVICE_USER)"
if $FLAG_DEV; then
    echo "  Mode:       dev — gtimes/gps_parser/tostools are editable at $GIT_BASE/{gtimes,gps_parser,tostools}"
else
    echo "  Mode:       production — gtimes/gps_parser/tostools pinned via pyproject.toml git URLs"
    echo "              (to switch to editable siblings later: re-run with --dev)"
fi
if [[ $WARNINGS -gt 0 ]]; then
    echo ""
    warn "$WARNINGS warnings — review output above"
fi
echo ""
echo "Day-to-day operations:"
echo ""
echo "  # Update code + reinstall (as bgo, no sudo needed):"
echo "  cd ~/git/receivers && git pull"
echo "  ~/git/receivers/venv/bin/pip install -e ."
echo "  sudo systemctl restart gps-receivers-scheduler"
if $FLAG_DEV; then
    echo ""
    echo "  # Update sibling package (editable — git pull is live, no reinstall):"
    echo "  cd ~/git/gtimes && git pull"
    echo "  sudo systemctl restart gps-receivers-scheduler"
fi
echo ""
echo "  # Manual download:"
echo "  sudo -u $SERVICE_USER receivers download ELDC --sync --archive"
echo ""
echo "  # Service management:"
echo "  sudo systemctl restart gps-receivers-scheduler"
echo "  journalctl -u gps-receivers-scheduler -f"
echo ""
echo "  # Grafana: http://${IP_ADDR:-<server-ip>}:3000"
echo ""
