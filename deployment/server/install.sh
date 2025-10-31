#!/bin/bash
# GPS Receivers Scheduler - Production Installation Script
# Veðurstofa Íslands
# Usage: Run from the receivers repository root directory
#        cd /opt/receivers && sudo ./deployment/scripts/install.sh

set -e

echo "=== GPS Receivers Scheduler - Production Installation ==="
echo ""

# Configuration
INSTALL_USER="gpsops"
INSTALL_GROUP="gpsops"
INSTALL_DIR="/opt/receivers"
CONFIG_DIR="/etc/gpsconfig"
CACHE_DIR="/var/cache/gps_receivers"
DATA_DIR="/mnt/gpsdata"
VENV_DIR="$INSTALL_DIR/venv"
PYTHON_MIN_VERSION="3.9"

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "Error: This script must be run as root"
   exit 1
fi

echo "Step 1: Creating system user and group..."
if ! id -u "$INSTALL_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /bin/bash \
            --comment "GPS Receivers Service" "$INSTALL_USER"
    echo "  ✓ Created user: $INSTALL_USER"
else
    echo "  ✓ User already exists: $INSTALL_USER"
fi

echo ""
echo "Step 2: Creating directory structure..."
mkdir -p "$INSTALL_DIR"/{src,logs,tmp}
mkdir -p "$CONFIG_DIR"
mkdir -p "$CACHE_DIR"/{logs,tmp}
mkdir -p "$DATA_DIR"

echo "  ✓ Created directories"

echo ""
echo "Step 3: Setting permissions..."
chown -R "$INSTALL_USER:$INSTALL_GROUP" "$INSTALL_DIR"
chown -R "$INSTALL_USER:$INSTALL_GROUP" "$CACHE_DIR"
chown -R "$INSTALL_USER:$INSTALL_GROUP" "$DATA_DIR"
chmod 755 "$CONFIG_DIR"
chmod 700 "$CACHE_DIR"

echo "  ✓ Permissions configured"

echo ""
echo "Step 4: Installing systemd service..."
cp deployment/systemd/gps-receivers-scheduler.service /etc/systemd/system/
systemctl daemon-reload
echo "  ✓ Systemd service installed"

echo ""
echo "Step 5: Installing log rotation..."
cp deployment/logrotate.d/gps-receivers /etc/logrotate.d/
chmod 644 /etc/logrotate.d/gps-receivers
echo "  ✓ Log rotation configured"

echo ""
echo "Step 6: Checking system Python..."
# Check if Python 3 is installed
if ! command -v python3 &> /dev/null; then
    echo "  ✗ Error: python3 not found"
    echo "    Installing python3 and python3-venv..."
    apt-get update
    apt-get install -y python3 python3-pip python3-venv python3-dev
fi

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "  ✓ Found Python $PYTHON_VERSION"

if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"; then
    echo "  ✗ Error: Python $PYTHON_VERSION is too old (minimum: $PYTHON_MIN_VERSION)"
    exit 1
fi

echo ""
echo "Step 7: Creating Python virtual environment..."
if [[ -d "$VENV_DIR" ]]; then
    echo "  ℹ Virtual environment already exists at $VENV_DIR"
    echo "    Removing old venv..."
    rm -rf "$VENV_DIR"
fi

sudo -u "$INSTALL_USER" python3 -m venv "$VENV_DIR"
echo "  ✓ Virtual environment created"

echo ""
echo "Step 8: Installing Python packages..."
echo "  Upgrading pip, setuptools, wheel..."
sudo -u "$INSTALL_USER" "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel

# Install gtimes (prefer local if available, otherwise PyPI)
if [[ -d "/opt/gtimes" ]]; then
    echo "  Installing gtimes from local directory..."
    sudo -u "$INSTALL_USER" "$VENV_DIR/bin/pip" install -e /opt/gtimes
else
    echo "  Installing gtimes from PyPI..."
    sudo -u "$INSTALL_USER" "$VENV_DIR/bin/pip" install gtimes
fi

# Install gps_parser (prefer local if available, otherwise GitHub)
if [[ -d "/opt/gps_parser" ]]; then
    echo "  Installing gps_parser from local directory..."
    sudo -u "$INSTALL_USER" "$VENV_DIR/bin/pip" install -e /opt/gps_parser
else
    echo "  Installing gps_parser from GitHub..."
    sudo -u "$INSTALL_USER" "$VENV_DIR/bin/pip" install git+https://github.com/bennigo/gps_parser.git
fi

echo "  Installing receivers package..."
sudo -u "$INSTALL_USER" "$VENV_DIR/bin/pip" install -e "$INSTALL_DIR"

echo "  ✓ All packages installed"

echo ""
echo "Step 9: Cloning configuration repository..."
CONFIG_REPO_DIR="/opt/gps-config-data"
if [[ ! -d "$CONFIG_REPO_DIR" ]]; then
    echo "  Cloning gps-config-data from git.vedur.is..."
    # Use HTTPS for read-only access (no SSH keys needed)
    if git clone https://git.vedur.is/bgo/gps-config-data.git "$CONFIG_REPO_DIR" 2>/dev/null; then
        chown -R "$INSTALL_USER:$INSTALL_GROUP" "$CONFIG_REPO_DIR"
        echo "  ✓ Configuration repository cloned"
    elif [[ -d "/opt/gps-config-data-fallback" ]]; then
        echo "  ⚠ Git clone failed, using local fallback copy..."
        cp -r /opt/gps-config-data-fallback "$CONFIG_REPO_DIR"
        chown -R "$INSTALL_USER:$INSTALL_GROUP" "$CONFIG_REPO_DIR"
        echo "  ✓ Configuration copied from fallback"
    else
        echo "  ✗ Error: Cannot clone gps-config-data and no fallback available"
        exit 1
    fi
else
    echo "  ✓ Configuration repository already exists"
    echo "    Ensuring correct ownership..."
    chown -R "$INSTALL_USER:$INSTALL_GROUP" "$CONFIG_REPO_DIR"
    echo "    Updating from remote..."
    cd "$CONFIG_REPO_DIR"
    if ! sudo -u "$INSTALL_USER" git pull 2>/dev/null; then
        echo "  ⚠ Git pull failed (network issue or not a git repo), continuing..."
    fi
fi

echo ""
echo "Step 10: Deploying configuration files..."
# Use gps-config CLI to deploy templates
# The --env parameter will auto-detect based on hostname if not specified
if sudo -u "$INSTALL_USER" "$VENV_DIR/bin/gps-config" deploy \
    --config-dir "$CONFIG_REPO_DIR" \
    --verbose; then
    echo "  ✓ Configuration deployed to $CONFIG_DIR"
else
    echo "  ✗ Error: Configuration deployment failed"
    echo "    You may need to manually run: gps-config deploy --env <environment>"
    exit 1
fi

echo ""
echo "Step 11: Copying configuration files to $CONFIG_DIR..."
# Copy rendered template files
for config_file in receivers.cfg scheduler.yaml postprocess.cfg; do
    if [[ -f "$CONFIG_REPO_DIR/$config_file" ]]; then
        cp "$CONFIG_REPO_DIR/$config_file" "$CONFIG_DIR/"
        chown "$INSTALL_USER:$INSTALL_GROUP" "$CONFIG_DIR/$config_file"
        echo "  ✓ Copied $config_file"
    fi
done

# Copy non-templated shared config files
for config_file in stations.cfg database.cfg icinga.cfg; do
    if [[ -f "$CONFIG_REPO_DIR/$config_file" ]]; then
        cp "$CONFIG_REPO_DIR/$config_file" "$CONFIG_DIR/"
        chown "$INSTALL_USER:$INSTALL_GROUP" "$CONFIG_DIR/$config_file"
        echo "  ✓ Copied $config_file"
    fi
done

echo ""
echo "Step 12: Verifying installation..."
if ! sudo -u "$INSTALL_USER" "$VENV_DIR/bin/receivers" --help &> /dev/null; then
    echo "  ✗ Error: receivers command not working"
    exit 1
fi

if ! sudo -u "$INSTALL_USER" "$VENV_DIR/bin/gps-config" --help &> /dev/null; then
    echo "  ✗ Error: gps-config command not working"
    exit 1
fi

# Verify configuration files exist
for config_file in stations.cfg receivers.cfg scheduler.yaml; do
    if [[ ! -f "$CONFIG_DIR/$config_file" ]]; then
        echo "  ✗ Error: Missing required config: $config_file"
        exit 1
    fi
done

echo "  ✓ Installation verified"

echo ""
echo "=== Installation Complete ===="
echo ""
echo "Configuration deployed to: $CONFIG_DIR"
echo "  - stations.cfg (shared)"
echo "  - receivers.cfg (from template)"
echo "  - postprocess.cfg (from template)"
echo "  - scheduler.yaml (from template)"
echo ""
echo "Next steps:"
echo "  1. Test the service:"
echo "     systemctl start gps-receivers-scheduler"
echo "     systemctl status gps-receivers-scheduler"
echo ""
echo "  2. View logs:"
echo "     journalctl -u gps-receivers-scheduler -f"
echo ""
echo "  3. Enable on boot:"
echo "     systemctl enable gps-receivers-scheduler"
echo ""
echo "  4. Update configurations (when needed):"
echo "     cd $CONFIG_REPO_DIR && git pull"
echo "     gps-config deploy --env <environment>"
echo ""
