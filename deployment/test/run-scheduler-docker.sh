#!/bin/bash
# Run GPS Receivers Scheduler in Docker for testing
# This keeps the container running so you can test the scheduler

set -e

echo "=== GPS Receivers Scheduler - Docker Test Environment ==="
echo ""

# Get the absolute path to the receivers directory
RECEIVERS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
echo "Receivers directory: $RECEIVERS_DIR"

GPS_PARSER_DIR="$(cd "$RECEIVERS_DIR/../gps_parser" && pwd)"
echo "GPS parser directory: $GPS_PARSER_DIR"

GTIMES_DIR="$(cd "$RECEIVERS_DIR/../gtimes" && pwd)"
echo "GTimes directory: $GTIMES_DIR"

GPS_CONFIG_DATA_DIR="$(cd "$RECEIVERS_DIR/../gps-config-data" && pwd)"
echo "GPS config data directory: $GPS_CONFIG_DATA_DIR"

echo ""
echo "Building test Docker image..."
docker build -t gps-receivers-test -f "$RECEIVERS_DIR/deployment/test/Dockerfile.test" "$RECEIVERS_DIR/deployment/test/"

echo ""
echo "Starting interactive test container..."
echo "The scheduler will be installed but NOT started automatically."
echo "You can start it manually inside the container."
echo ""

# Run container in interactive mode
docker run -it --rm \
    --privileged \
    --network=host \
    --name gps-receivers-live-test \
    -v "$RECEIVERS_DIR:/opt/receivers-source:ro" \
    -v "$GPS_PARSER_DIR:/opt/gps_parser-source:ro" \
    -v "$GTIMES_DIR:/opt/gtimes-source:ro" \
    -v "$GPS_CONFIG_DATA_DIR:/opt/gps-config-data-source:ro" \
    gps-receivers-test \
    /bin/bash -c '
        set -e

        echo "=== Setting up test environment ==="
        echo ""

        # Copy sources
        cp -r /opt/receivers-source /opt/receivers
        cp -r /opt/gps_parser-source /opt/gps_parser
        cp -r /opt/gtimes-source /opt/gtimes
        cp -r /opt/gps-config-data-source /opt/gps-config-data-fallback

        # Run installation
        cd /opt/receivers
        ./deployment/scripts/install.sh

        echo ""
        echo "=== Installation Complete ==="
        echo ""
        echo "You are now in the Docker container. Available commands:"
        echo ""
        echo "  # Test scheduler configuration"
        echo "  /opt/receivers/venv/bin/receivers scheduler test --stations ELDC THOB --verbose"
        echo ""
        echo "  # Start scheduler (limited stations for testing)"
        echo "  /opt/receivers/venv/bin/receivers scheduler start --stations ELDC THOB --max-workers 2 --verbose --show-jobs"
        echo ""
        echo "  # Check logs"
        echo "  tail -f /var/cache/gps_receivers/logs/scheduler.log"
        echo ""
        echo "  # Exit container"
        echo "  exit"
        echo ""

        # Start interactive shell
        /bin/bash
    '

echo ""
echo "Container exited"
