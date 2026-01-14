#!/bin/bash
# Monitor concurrent downloads in GPS receivers scheduler

echo "=== GPS Receivers Download Monitor ==="
echo "Press Ctrl+C to exit"
echo ""

while true; do
    clear
    echo "=== GPS Receivers Download Monitor ==="
    echo "Timestamp: $(date '+%Y-%m-%d %H:%M:%S')"
    echo ""

    # Get current downloads
    active_downloads=$(docker logs --tail 200 gps-receivers-scheduler-dev 2>&1 | grep "Downloading.*\.gz:" | tail -20)

    if [ -z "$active_downloads" ]; then
        echo "No active downloads at the moment"
    else
        echo "Active downloads:"
        echo "$active_downloads" | while read line; do
            # Extract station name and progress
            station=$(echo "$line" | grep -oP '(?<=Downloading )[A-Z0-9]+' | head -1)
            progress=$(echo "$line" | grep -oP '\d+%' | head -1)
            speed=$(echo "$line" | grep -oP '\d+\.?\d*[kMG]?B/s' | tail -1)
            size=$(echo "$line" | grep -oP '\d+\.?\d*[kMG]?/\d+\.?\d*[kMG]?' | head -1)
            echo "  ├─ $station: $progress ($size) @ $speed"
        done | sort -u

        echo ""
        echo "Total concurrent downloads: $(echo "$active_downloads" | grep -c "Downloading")"
    fi

    echo ""
    echo "Recent completions (last 5):"
    docker logs --tail 100 gps-receivers-scheduler-dev 2>&1 | grep "Completed:" | tail -5 | while read line; do
        station=$(echo "$line" | grep -oP '(?<=Completed: )[A-Z0-9]+')
        files=$(echo "$line" | grep -oP '\d+(?= files)')
        duration=$(echo "$line" | grep -oP '\d+\.?\d*s')
        echo "  ✓ $station - $files files in $duration"
    done

    echo ""
    echo "Scheduler stats:"
    echo "  Workers: 5 (default)"
    echo "  Container: $(docker ps --filter name=gps-receivers-scheduler-dev --format 'Up {{.RunningFor}}')"

    sleep 3
done
