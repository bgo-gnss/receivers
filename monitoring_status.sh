#!/bin/bash
# Check status of performance monitoring

echo "=== GPS Receivers Monitoring Status ==="
echo ""

# Check if monitoring is running
if pgrep -f "collect_performance_data.sh" > /dev/null; then
    MONITOR_PID=$(pgrep -f "collect_performance_data.sh")
    echo "✅ Monitoring is RUNNING"
    echo "  PID: $MONITOR_PID"

    # Get process start time and duration
    START_TIME=$(ps -p $MONITOR_PID -o lstart= 2>/dev/null)
    ELAPSED=$(ps -p $MONITOR_PID -o etime= 2>/dev/null | tr -d ' ')
    echo "  Started: $START_TIME"
    echo "  Running for: $ELAPSED"
else
    echo "❌ Monitoring is NOT running"
    echo ""
    echo "Start with: ./start_monitoring.sh"
    exit 0
fi

echo ""
echo "Data Files:"
if ls /tmp/gps_performance_data/performance_*.csv 1> /dev/null 2>&1; then
    for file in /tmp/gps_performance_data/performance_*.csv; do
        SIZE=$(du -h "$file" | cut -f1)
        LINES=$(wc -l < "$file")
        SAMPLES=$((LINES - 1))  # Subtract header
        DURATION=$((SAMPLES * 30 / 60))  # minutes
        echo "  📊 $(basename "$file")"
        echo "     Size: $SIZE | Samples: $SAMPLES | Duration: ${DURATION} min"
    done
else
    echo "  No data files yet"
fi

echo ""
echo "Latest Data:"
if ls /tmp/gps_performance_data/performance_*.csv 1> /dev/null 2>&1; then
    LATEST_FILE=$(ls -t /tmp/gps_performance_data/performance_*.csv | head -1)
    echo "  $(tail -1 "$LATEST_FILE")"

    # Parse latest values
    LATEST_DATA=$(tail -1 "$LATEST_FILE")
    ACTIVE=$(echo "$LATEST_DATA" | cut -d',' -f2)
    COMPLETED_5MIN=$(echo "$LATEST_DATA" | cut -d',' -f5)
    RATE=$(echo "scale=1; $COMPLETED_5MIN / 5" | bc 2>/dev/null || echo "N/A")

    echo ""
    echo "  Current metrics:"
    echo "    Active downloads: $ACTIVE workers"
    echo "    Completion rate: $RATE jobs/min"
    echo "    Utilization: $(echo "scale=1; $ACTIVE * 100 / 200" | bc 2>/dev/null || echo 0)%"
fi

echo ""
echo "Quick Stats (last 15 minutes):"
if ls /tmp/gps_performance_data/performance_*.csv 1> /dev/null 2>&1; then
    LATEST_FILE=$(ls -t /tmp/gps_performance_data/performance_*.csv | head -1)

    # Get last 30 samples (15 minutes at 30s interval)
    tail -31 "$LATEST_FILE" | awk -F',' '
    NR>1 {
        active = $2;
        sum += active;
        count++;
        if (active > max) max = active;
        if (min == 0 || active < min) min = active;
    }
    END {
        if (count > 0) {
            printf "  Max concurrent: %d workers\n", max;
            printf "  Avg concurrent: %.1f workers\n", sum/count;
            printf "  Min concurrent: %d workers\n", min;
        }
    }'
fi

echo ""
echo "Commands:"
echo "  View live log:   tail -f /tmp/gps_performance_data/monitor.log"
echo "  Analyze data:    ./analyze_performance_data.sh $LATEST_FILE"
echo "  Stop monitoring: pkill -f collect_performance_data.sh"
