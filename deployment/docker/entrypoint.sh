#!/bin/bash
# GPS Receivers Scheduler - Docker Entrypoint
set -e

echo "=== GPS Receivers Scheduler - Starting ==="
echo ""

# Ensure required directories exist with correct permissions
# (bind mounts might not have subdirectories)
mkdir -p /var/cache/gps_receivers/logs /var/cache/gps_receivers/tmp /mnt/gpsdata
chmod 755 /var/cache/gps_receivers/logs /var/cache/gps_receivers/tmp /mnt/gpsdata
echo "✓ Directories verified"
echo ""

# Clone or update gps-config-data
CONFIG_REPO_DIR="/opt/gps-config-data"
CONFIG_REPO_URL="${GPS_CONFIG_REPO_URL:-https://git.vedur.is/bgo/gps-config-data.git}"

if [[ ! -d "$CONFIG_REPO_DIR" ]]; then
    echo "Cloning configuration repository from $CONFIG_REPO_URL..."
    git clone "$CONFIG_REPO_URL" "$CONFIG_REPO_DIR" || {
        echo "⚠️  Failed to clone config repo, using mounted config if available"
    }
else
    echo "Updating configuration repository..."
    cd "$CONFIG_REPO_DIR"
    git pull || echo "⚠️  Failed to update config repo"
fi

# Deploy configuration
if [[ -d "$CONFIG_REPO_DIR" ]]; then
    echo "Deploying configuration..."
    cd "$CONFIG_REPO_DIR"

    # Auto-detect environment or use ENV variable
    ENVIRONMENT="${GPS_ENVIRONMENT:-production}"
    echo "Environment: $ENVIRONMENT"

    if [[ -f "deploy.py" ]]; then
        python3 deploy.py --env "$ENVIRONMENT" --target /etc/gpsconfig
    else
        echo "⚠️  No deploy.py found, copying config files directly"
        # Copy all config files (cfg and yaml)
        cp -v *.cfg *.yaml /etc/gpsconfig/ 2>/dev/null || true
        # Make sure permissions are correct
        chmod 644 /etc/gpsconfig/* 2>/dev/null || true
    fi

    echo "✓ Configuration deployed"
fi

# Fix paths in receivers.cfg for Docker environment
echo "Fixing paths for Docker environment..."
if [[ -f "/etc/gpsconfig/receivers.cfg" ]]; then
    # Replace development paths with Docker paths
    sed -i 's|prepath = /home/bgo/.*|prepath = /mnt/gpsdata|' /etc/gpsconfig/receivers.cfg
    sed -i 's|tmp_dir = /home/bgo/.*|tmp_dir = /var/cache/gps_receivers/tmp|' /etc/gpsconfig/receivers.cfg
    echo "✓ Paths configured for Docker"
fi

echo ""
echo "=== Starting Scheduler ==="
echo ""

cd /opt/receivers

# Parse command
case "$1" in
    scheduler)
        shift
        exec receivers scheduler "$@"
        ;;
    *)
        exec receivers "$@"
        ;;
esac
