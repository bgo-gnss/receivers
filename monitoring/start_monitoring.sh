#!/bin/bash
# Start performance monitoring in background

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/tmp/gps_performance_data/monitor.log"

# Create output directory
mkdir -p /tmp/gps_performance_data

# Check if already running
if pgrep -f "collect_performance_data.sh" > /dev/null; then
    echo "⚠️  Performance monitoring is already running!"
    echo ""
    echo "To view status: ./monitoring_status.sh"
    echo "To stop: pkill -f collect_performance_data.sh"
    exit 1
fi

echo "🚀 Starting GPS Receivers Performance Monitoring"
echo ""
echo "Configuration:"
echo "  Sample interval: 30 seconds"
echo "  Max workers: 200"
echo "  Data directory: /tmp/gps_performance_data/"
echo ""

# Start in background with nohup
nohup "$SCRIPT_DIR/collect_performance_data.sh" > "$LOG_FILE" 2>&1 &
MONITOR_PID=$!

sleep 2

if ps -p $MONITOR_PID > /dev/null; then
    echo "✅ Monitoring started successfully!"
    echo ""
    echo "  PID: $MONITOR_PID"
    echo "  Log: $LOG_FILE"
    echo ""
    echo "Commands:"
    echo "  Check status:    ./monitoring_status.sh"
    echo "  View live data:  tail -f $LOG_FILE"
    echo "  Stop monitoring: pkill -f collect_performance_data.sh"
    echo "  Analyze data:    ./analyze_performance_data.sh <data_file>"
    echo ""
    echo "Data collection will continue until stopped."
    echo "Press Ctrl+C to stop, or use: pkill -f collect_performance_data.sh"
else
    echo "❌ Failed to start monitoring"
    echo "Check log: $LOG_FILE"
    exit 1
fi
