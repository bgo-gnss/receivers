#!/bin/bash
# Monitor concurrent download performance with 200 workers

echo "=== GPS Receivers - Concurrent Download Monitor ==="
echo "Max Workers: 200 (from scheduler.yaml)"
echo "Press Ctrl+C to exit"
echo ""

while true; do
    clear
    echo "=== GPS Concurrent Download Monitor - $(date '+%Y-%m-%d %H:%M:%S') ==="
    echo ""

    # Get recent download activity (last 30 seconds of logs)
    recent_logs=$(docker logs --since 30s gps-receivers-scheduler-dev 2>&1)

    # Count unique "Starting download" in last 30s
    starting_count=$(echo "$recent_logs" | grep -c "Starting download:" || echo 0)

    # Count unique "Completed" in last 30s
    completed_count=$(echo "$recent_logs" | grep -c "Completed:" || echo 0)

    # Count unique stations actively downloading (has "Downloading" progress)
    active_stations=$(docker logs --tail 500 gps-receivers-scheduler-dev 2>&1 | \
        grep "Downloading.*\.gz:" | \
        grep -oP '[A-Z0-9]{4}(?=\d{7})' | \
        sort -u | wc -l)

    # Get unique active station names
    active_list=$(docker logs --tail 500 gps-receivers-scheduler-dev 2>&1 | \
        grep "Downloading.*\.gz:" | \
        grep -oP '[A-Z0-9]{4}(?=\d{7})' | \
        sort -u | head -20 | tr '\n' ' ')

    echo "📊 Concurrent Activity:"
    echo "  Active downloads: $active_stations stations"
    echo "  Started (last 30s): $starting_count jobs"
    echo "  Completed (last 30s): $completed_count jobs"
    echo ""

    if [ $active_stations -gt 0 ]; then
        echo "📥 Active stations (max 20 shown):"
        echo "  $active_list"
    else
        echo "⏸️  No active downloads at the moment"
    fi

    echo ""
    echo "🔄 Recent activity (last 10):"
    docker logs --tail 200 gps-receivers-scheduler-dev 2>&1 | \
        grep -E "Starting download:|Completed:" | \
        grep -v "Downloading" | \
        tail -10 | \
        sed 's/.*- /  /' | \
        sed 's/Starting download:/▶️  START:/' | \
        sed 's/Completed:/✅ DONE: /'

    echo ""
    echo "⚡ Performance Summary:"
    total_jobs=$(docker logs --since 5m gps-receivers-scheduler-dev 2>&1 | grep -c "Completed:" || echo 0)
    echo "  Jobs completed (last 5 min): $total_jobs"
    echo "  Average rate: $(echo "scale=1; $total_jobs / 5" | bc) jobs/min"

    # Peak concurrent estimate (max workers utilized)
    echo "  Peak concurrent: ~$active_stations/$200 workers"
    utilization=$(echo "scale=1; $active_stations * 100 / 200" | bc)
    echo "  Worker utilization: ${utilization}%"

    echo ""
    echo "Next update in 5 seconds..."
    sleep 5
done
