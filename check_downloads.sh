#!/bin/bash
# Quick check of current download status

echo "=== Active Downloads ==="
active=$(docker logs --tail 300 gps-receivers-scheduler-dev 2>&1 | \
  grep "Downloading.*\.gz:" | tail -30)

if [ -z "$active" ]; then
    echo "  None currently active"
else
    echo "$active" | \
      sed -n 's/.*Downloading \([A-Z0-9]\{4\}\)[0-9].*\.gz:.*\([0-9]\+%\).*\[\([0-9:]\+\).*\([0-9.]\+[kMG]B\/s\).*/  • \1  \2  @ \4  (\3)/p' | \
      sort -u -k2 | head -10
fi

echo ""
echo "=== Download Statistics ==="
# Count unique stations currently downloading
station_count=$(echo "$active" | grep -oP '[A-Z0-9]{4}(?=\d{7})' | sort -u | wc -l)
echo "  Concurrent stations: $station_count"

# Total progress lines
total_lines=$(echo "$active" | wc -l)
echo "  Recent progress updates: $total_lines"

echo ""
echo "=== Recent Completions (last 5) ==="
completions=$(docker logs --tail 100 gps-receivers-scheduler-dev 2>&1 | grep "Completed:")
if [ -z "$completions" ]; then
    echo "  No recent completions"
else
    echo "$completions" | tail -5 | \
      sed -n 's/.*Completed: \([A-Z0-9]\+\) (\([^)]\+\)) - \([0-9]\+\) files in \([0-9.]\+s\)/  ✓ \1 (\2): \3 files in \4/p'
fi

echo ""
echo "=== Scheduler Status ==="
docker ps --filter name=gps-receivers-scheduler-dev --format '  Container: {{.Status}}'
