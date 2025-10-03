#!/bin/bash
# Test Installation Script for GPS Receivers Scheduler
# This script sets up and runs the installation in a Docker container

set -e

# Parse command line options
FETCH_CONFIG_FROM_GIT=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --fetch-config)
            FETCH_CONFIG_FROM_GIT=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--fetch-config]"
            echo "  --fetch-config: Fetch gps-config-data from git.vedur.is (instead of using local copy)"
            exit 1
            ;;
    esac
done

echo "=== GPS Receivers Scheduler - Installation Test ==="
echo ""

# Get the absolute path to the receivers directory
RECEIVERS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
echo "Receivers directory: $RECEIVERS_DIR"

# Get the absolute path to gps_parser (sibling of receivers)
GPS_PARSER_DIR="$(cd "$RECEIVERS_DIR/../gps_parser" && pwd)"
echo "GPS parser directory: $GPS_PARSER_DIR"

# Get the absolute path to gtimes (sibling of receivers)
GTIMES_DIR="$(cd "$RECEIVERS_DIR/../gtimes" && pwd)"
echo "GTimes directory: $GTIMES_DIR"

# Optionally get local gps-config-data (for offline testing)
if [[ "$FETCH_CONFIG_FROM_GIT" == "false" ]] && [[ -d "$RECEIVERS_DIR/../gps-config-data" ]]; then
    GPS_CONFIG_DATA_DIR="$(cd "$RECEIVERS_DIR/../gps-config-data" && pwd)"
    echo "GPS config data directory: $GPS_CONFIG_DATA_DIR (local copy for offline test)"
    CONFIG_MOUNT="-v $GPS_CONFIG_DATA_DIR:/opt/gps-config-data-source:ro"
else
    echo "GPS config data: Will fetch from git.vedur.is during installation"
    CONFIG_MOUNT=""
fi

echo ""
echo "Building test Docker image..."
docker build -t gps-receivers-test -f "$RECEIVERS_DIR/deployment/test/Dockerfile.test" "$RECEIVERS_DIR/deployment/test/"

echo ""
echo "Starting test container..."
echo "This will run the installation script in a clean Ubuntu environment"
echo ""

# Run the container with:
# - All required repositories mounted
# - Interactive terminal
# - Privileged mode (needed for systemd)
# - Remove container after exit
docker run --rm \
    --privileged \
    -v "$RECEIVERS_DIR:/opt/receivers-source:ro" \
    -v "$GPS_PARSER_DIR:/opt/gps_parser-source:ro" \
    -v "$GTIMES_DIR:/opt/gtimes-source:ro" \
    $CONFIG_MOUNT \
    gps-receivers-test \
    /bin/bash -c "
        set -e

        echo '=== Inside Test Container ==='
        echo ''
        echo 'Current environment:'
        echo '  OS:' \$(cat /etc/os-release | grep PRETTY_NAME)
        echo '  Python:' \$(python3 --version)
        echo '  Git:' \$(git --version)
        echo ''

        echo 'Simulating fresh server deployment...'
        echo ''

        # Copy sources to writable locations (simulates git clone)
        echo '  Copying receivers repository...'
        cp -r /opt/receivers-source /opt/receivers

        echo '  Copying dependency repositories...'
        cp -r /opt/gps_parser-source /opt/gps_parser
        cp -r /opt/gtimes-source /opt/gtimes

        # Only copy config if local copy provided (offline test)
        if [[ -d /opt/gps-config-data-source ]]; then
            echo '  Copying gps-config-data (offline test mode)...'
            cp -r /opt/gps-config-data-source /opt/gps-config-data-fallback
        else
            echo '  Skipping gps-config-data - will fetch from git.vedur.is'
        fi

        echo '  ✓ Repositories ready'
        echo ''

        echo 'Starting installation...'
        echo ''

        cd /opt/receivers
        ./deployment/scripts/install.sh

        echo ''
        echo '=== Installation Test Complete ==='
        echo ''
        echo 'Verification:'
        /opt/receivers/venv/bin/receivers --help | head -5
        echo ''
        echo 'Installed configuration files:'
        ls -la /etc/gpsconfig/
        echo ''
        echo 'Config repository source:'
        if [[ -d /opt/gps-config-data/.git ]]; then
            echo '  Git remote:' \$(cd /opt/gps-config-data && git remote -v | grep fetch)
        fi
    "

echo ""
echo "Test container exited"
