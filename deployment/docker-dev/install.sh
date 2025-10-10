#!/bin/bash
# GPS Receivers Scheduler - Docker Installation Script
# Runs inside the container on first startup to set up Python environment and config
set -e

echo "=== GPS Receivers Docker Installation ==="
echo ""

# Configuration
INSTALL_DIR="/opt/receivers"
CONFIG_DIR="/etc/gpsconfig"
CACHE_DIR="/var/cache/gps_receivers"
DATA_DIR="/mnt/gpsdata"
VENV_DIR="$INSTALL_DIR/venv"
CONFIG_REPO_DIR="/opt/gps-config-data"

echo "Step 1: Verifying directory structure..."
mkdir -p "$CACHE_DIR"/{logs,tmp} "$DATA_DIR" 2>/dev/null || true
echo "  ✓ Directories verified"

echo ""
echo "Step 2: Creating Python virtual environment..."
if [[ -d "$VENV_DIR" ]]; then
    echo "  ℹ Virtual environment already exists, skipping..."
else
    python3 -m venv "$VENV_DIR"
    echo "  ✓ Virtual environment created"
fi

echo ""
echo "Step 3: Installing Python packages..."
echo "  Upgrading pip, setuptools, wheel..."
"$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel --quiet

# Install gtimes from local mount
if [[ -d "/opt/gtimes" && -f "/opt/gtimes/pyproject.toml" ]]; then
    echo "  Installing gtimes from /opt/gtimes (local mount)..."
    "$VENV_DIR/bin/pip" install -e /opt/gtimes --quiet
else
    echo "  ⚠ Warning: /opt/gtimes not found, installing from PyPI..."
    "$VENV_DIR/bin/pip" install gtimes --quiet
fi

# Install gps_parser from local mount
if [[ -d "/opt/gps_parser" && -f "/opt/gps_parser/pyproject.toml" ]]; then
    echo "  Installing gps_parser from /opt/gps_parser (local mount)..."
    "$VENV_DIR/bin/pip" install -e /opt/gps_parser --quiet
else
    echo "  ⚠ Warning: /opt/gps_parser not found, installing from GitHub..."
    "$VENV_DIR/bin/pip" install git+https://github.com/bennigo/gps_parser.git --quiet
fi

# Install receivers from local mount (build context root)
echo "  Installing receivers package..."
if [[ -d "$INSTALL_DIR/src" && -f "$INSTALL_DIR/pyproject.toml" ]]; then
    "$VENV_DIR/bin/pip" install -e "$INSTALL_DIR" --quiet
else
    echo "  ✗ Error: receivers source not found at $INSTALL_DIR"
    exit 1
fi

echo "  ✓ All packages installed"

echo ""
echo "Step 4: Deploying configuration files..."

# Check if gps-config-data is mounted and has content
if [[ ! -d "$CONFIG_REPO_DIR" || -z "$(ls -A $CONFIG_REPO_DIR 2>/dev/null)" ]]; then
    echo "  ⚠ gps-config-data not mounted, attempting to clone..."
    CONFIG_REPO_URL="${GPS_CONFIG_REPO_URL:-https://git.vedur.is/bgo/gps-config-data.git}"
    if git clone "$CONFIG_REPO_URL" "$CONFIG_REPO_DIR" 2>/dev/null; then
        echo "  ✓ Configuration repository cloned"
    else
        echo "  ✗ Error: Cannot access gps-config-data"
        exit 1
    fi
else
    echo "  ✓ Using mounted gps-config-data"
fi

# Copy configuration files directly
echo "  Copying configuration files to $CONFIG_DIR..."

# Copy scheduler.yaml (prefer non-template if it exists)
if [[ -f "$CONFIG_REPO_DIR/scheduler.yaml" ]]; then
    cp "$CONFIG_REPO_DIR/scheduler.yaml" "$CONFIG_DIR/scheduler.yaml"
    echo "    ✓ scheduler.yaml"
elif [[ -f "$CONFIG_REPO_DIR/scheduler.yaml.template" ]]; then
    cp "$CONFIG_REPO_DIR/scheduler.yaml.template" "$CONFIG_DIR/scheduler.yaml"
    echo "    ✓ scheduler.yaml (from template)"
fi

# Copy other config files
for config_file in receivers.cfg stations.cfg postprocess.cfg database.cfg icinga.cfg; do
    if [[ -f "$CONFIG_REPO_DIR/$config_file" ]]; then
        cp "$CONFIG_REPO_DIR/$config_file" "$CONFIG_DIR/"
        echo "    ✓ $config_file"
    elif [[ -f "$CONFIG_REPO_DIR/${config_file}.template" ]]; then
        cp "$CONFIG_REPO_DIR/${config_file}.template" "$CONFIG_DIR/$config_file"
        echo "    ✓ $config_file (from template)"
    fi
done

echo ""
echo "Step 5: Fixing paths for Docker environment..."
# Fix receivers.cfg to use Docker paths (only if file exists)
if [[ -f "$CONFIG_DIR/receivers.cfg" ]]; then
    # Update prepath to /mnt/gpsdata for Docker
    sed -i 's|prepath = /home/bgo/.*|prepath = /mnt/gpsdata|' "$CONFIG_DIR/receivers.cfg"
    sed -i 's|prepath = /mnt/gpsdata.*|prepath = /mnt/gpsdata|' "$CONFIG_DIR/receivers.cfg"

    # Update tmp_dir to cache location
    sed -i 's|tmp_dir = /home/bgo/.*|tmp_dir = /var/cache/gps_receivers/tmp|' "$CONFIG_DIR/receivers.cfg"
    sed -i 's|tmp_dir = .*|tmp_dir = /var/cache/gps_receivers/tmp|' "$CONFIG_DIR/receivers.cfg"

    echo "  ✓ Updated receivers.cfg paths for Docker"
fi

# Fix scheduler.yaml to use Docker paths (only if file exists)
if [[ -f "$CONFIG_DIR/scheduler.yaml" ]]; then
    # Update database path to /var/cache
    sed -i 's|database: ~/.cache/gps_receivers/scheduler.db|database: /var/cache/gps_receivers/scheduler.db|' "$CONFIG_DIR/scheduler.yaml"

    # Update log_dir path to /var/cache
    sed -i 's|log_dir: ~/.cache/gps_receivers/logs|log_dir: /var/cache/gps_receivers/logs|' "$CONFIG_DIR/scheduler.yaml"

    echo "  ✓ Updated scheduler.yaml paths for Docker"
fi

echo ""
echo "Step 6: Verifying installation..."
if ! "$VENV_DIR/bin/receivers" --help &> /dev/null; then
    echo "  ✗ Error: receivers command not working"
    exit 1
fi

# Verify critical config files exist
for config_file in stations.cfg scheduler.yaml; do
    if [[ ! -f "$CONFIG_DIR/$config_file" ]]; then
        echo "  ✗ Error: Missing required config: $config_file"
        exit 1
    fi
done

echo "  ✓ Installation verified"

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Configuration: $CONFIG_DIR"
echo "Data directory: $DATA_DIR"
echo "Cache directory: $CACHE_DIR"
echo ""
