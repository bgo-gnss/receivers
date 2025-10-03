#!/bin/bash
# Run GPS Receivers Scheduler in Docker - Simple non-interactive test
# Tests the scheduler with a few stations for a short period

set -e

echo "=== GPS Receivers Scheduler - Docker Test ==="
echo ""

# Get the absolute path to the receivers directory
RECEIVERS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

GPS_PARSER_DIR="$(cd "$RECEIVERS_DIR/../gps_parser" && pwd)"
GTIMES_DIR="$(cd "$RECEIVERS_DIR/../gtimes" && pwd)"
GPS_CONFIG_DATA_DIR="$(cd "$RECEIVERS_DIR/../gps-config-data" && pwd)"

echo "Building test Docker image..."
docker build -t gps-receivers-test -f "$RECEIVERS_DIR/deployment/test/Dockerfile.test" "$RECEIVERS_DIR/deployment/test/" -q

echo ""
echo "Starting scheduler test (will run for 2 minutes then stop)..."
echo "Testing with stations: ELDC, THOB, ORFC"
echo ""

# Run container non-interactively
docker run --rm \
    --privileged \
    --network=host \
    --name gps-receivers-scheduler-test \
    -v "$RECEIVERS_DIR:/opt/receivers-source:ro" \
    -v "$GPS_PARSER_DIR:/opt/gps_parser-source:ro" \
    -v "$GTIMES_DIR:/opt/gtimes-source:ro" \
    -v "$GPS_CONFIG_DATA_DIR:/opt/gps-config-data-source:ro" \
    gps-receivers-test \
    /bin/bash -c '
        set -e

        echo "=== Installing receivers package ==="

        # Copy sources
        cp -r /opt/receivers-source /opt/receivers
        cp -r /opt/gps_parser-source /opt/gps_parser
        cp -r /opt/gtimes-source /opt/gtimes
        cp -r /opt/gps-config-data-source /opt/gps-config-data-fallback

        # Run installation quietly
        cd /opt/receivers
        ./deployment/scripts/install.sh > /tmp/install.log 2>&1

        echo "✓ Installation complete"
        echo ""
        echo "=== Testing Scheduler Configuration ==="
        echo ""

        # Test scheduler config
        /opt/receivers/venv/bin/receivers scheduler test \
            --stations ELDC THOB ORFC \
            --max-stations 3 \
            --verbose

        echo ""
        echo "=== Starting Scheduler (limited test) ==="
        echo ""
        echo "Running scheduler for 120 seconds with 3 stations..."
        echo "Press Ctrl+C to stop early"
        echo ""

        # Start scheduler in background
        /opt/receivers/venv/bin/receivers scheduler start \
            --stations ELDC THOB ORFC \
            --max-workers 3 \
            --verbose \
            --show-jobs &

        SCHEDULER_PID=$!

        # Let it run for 2 minutes
        sleep 120

        # Stop scheduler
        echo ""
        echo "=== Stopping Scheduler ==="
        kill $SCHEDULER_PID 2>/dev/null || true

        echo ""
        echo "=== Test Complete ==="
        echo ""
        echo "Scheduler logs:"
        if [[ -f /var/cache/gps_receivers/logs/scheduler.log ]]; then
            echo "----------------------------------------"
            tail -50 /var/cache/gps_receivers/logs/scheduler.log
            echo "----------------------------------------"
        else
            echo "No logs found"
        fi
    '

echo ""
echo "Test finished"
