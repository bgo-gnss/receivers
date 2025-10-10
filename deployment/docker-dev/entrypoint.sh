#!/bin/bash
# GPS Receivers Scheduler - Docker Entrypoint
# Simple wrapper that runs install.sh on first startup, then starts the scheduler
set -e

INSTALL_MARKER="/opt/receivers/.installed"
SCHEDULER_DB="/var/cache/gps_receivers/scheduler.db"

# Run installation if not already done
if [[ ! -f "$INSTALL_MARKER" ]]; then
    echo "=== First Run: Running installation ==="
    echo ""
    /opt/receivers/install.sh

    # Create marker file to skip installation on subsequent restarts
    touch "$INSTALL_MARKER"
    echo ""
fi

# ALWAYS remove the scheduler database on startup to force fresh job creation from YAML
# This ensures configuration changes are always picked up
if [[ -f "$SCHEDULER_DB" ]]; then
    echo "Removing old scheduler database to force config reload..."
    rm -f "$SCHEDULER_DB"
    echo "  ✓ Database removed"
fi

echo "=== Starting GPS Receivers Scheduler ==="
echo ""

# Execute the receivers command with all arguments
cd /opt/receivers
exec receivers "$@"
