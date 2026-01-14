#!/bin/bash
# Quick check of concurrent download performance

echo "=== GPS Concurrent Download Status - $(date '+%H:%M:%S') ==="
echo ""

# Count unique stations actively downloading
active_stations=$(docker logs --tail 500 gps-receivers-scheduler-dev 2>&1 | \
    grep "Downloading.*\.gz:" | \
    grep -oP '[A-Z0-9]{4}(?=\d{7})' | \
    sort -u | wc -l)

# Get unique active station names
active_list=$(docker logs --tail 500 gps-receivers-scheduler-dev 2>&1 | \
    grep "Downloading.*\.gz:" | \
    grep -oP '[A-Z0-9]{4}(?=\d{7})' | \
    sort -u | head -30)

echo "📊 Current Activity:"
echo "  Active concurrent downloads: $active_stations stations"
echo ""

if [ $active_stations -gt 0 ]; then
    echo "📥 Stations currently downloading:"
    echo "$active_list" | sed 's/^/  • /'
    echo ""
fi

echo "🔄 Recent completions (last 10):"
docker logs --tail 200 gps-receivers-scheduler-dev 2>&1 | \
    grep "Completed:" | \
    grep -v "Downloading" | \
    tail -10 | \
    sed 's/.*Completed: /  ✓ /'

echo ""
echo "⚡ Performance (last 5 minutes):"
total_jobs=$(docker logs --since 5m gps-receivers-scheduler-dev 2>&1 | grep -c "Completed:" || echo 0)
echo "  Jobs completed: $total_jobs"
echo "  Average rate: $(echo "scale=1; $total_jobs / 5" | bc 2>/dev/null || echo "N/A") jobs/min"
echo "  Worker utilization: $active_stations/200 ($(echo "scale=1; $active_stations * 100 / 200" | bc 2>/dev/null || echo 0)%)"

echo ""
echo "💾 Container status:"
docker ps --filter name=gps-receivers-scheduler-dev --format '  {{.Status}}'
