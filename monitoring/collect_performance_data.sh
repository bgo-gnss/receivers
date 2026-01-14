#!/bin/bash
# Continuous performance data collection for GPS download scheduler
# Collects: timestamp, active downloads, started jobs, completed jobs
# Output: CSV format for analysis

OUTPUT_DIR="/tmp/gps_performance_data"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DATA_FILE="$OUTPUT_DIR/performance_${TIMESTAMP}.csv"
SUMMARY_FILE="$OUTPUT_DIR/summary_${TIMESTAMP}.txt"
SAMPLE_INTERVAL=30  # seconds between samples

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Initialize CSV file
echo "timestamp,active_downloads,started_30s,completed_30s,total_jobs_5min,container_uptime_seconds" > "$DATA_FILE"

echo "=== GPS Receivers Performance Data Collection Started ===" | tee "$SUMMARY_FILE"
echo "Start time: $(date)" | tee -a "$SUMMARY_FILE"
echo "Data file: $DATA_FILE" | tee -a "$SUMMARY_FILE"
echo "Sample interval: ${SAMPLE_INTERVAL}s" | tee -a "$SUMMARY_FILE"
echo "Max workers configured: 200" | tee -a "$SUMMARY_FILE"
echo "" | tee -a "$SUMMARY_FILE"

# Counter for samples
sample_count=0

# Trap Ctrl+C to generate final summary
trap 'generate_summary; exit 0' INT TERM

generate_summary() {
    echo "" | tee -a "$SUMMARY_FILE"
    echo "=== Collection Stopped ===" | tee -a "$SUMMARY_FILE"
    echo "End time: $(date)" | tee -a "$SUMMARY_FILE"
    echo "Total samples: $sample_count" | tee -a "$SUMMARY_FILE"
    echo "" | tee -a "$SUMMARY_FILE"

    if [ $sample_count -gt 0 ]; then
        echo "Computing statistics..." | tee -a "$SUMMARY_FILE"

        # Calculate overall statistics
        awk -F',' 'NR>1 {
            sum+=$2; count++;
            if($2>max) max=$2;
            if(min=="" || $2<min) min=$2
        }
        END {
            printf "Overall Statistics:\n"
            printf "  Total samples: %d\n", count
            printf "  Active downloads - Max: %d, Min: %d, Avg: %.1f\n", max, min, sum/count
        }' "$DATA_FILE" | tee -a "$SUMMARY_FILE"

        echo "" | tee -a "$SUMMARY_FILE"
        echo "Data saved to: $DATA_FILE" | tee -a "$SUMMARY_FILE"
        echo "Summary saved to: $SUMMARY_FILE" | tee -a "$SUMMARY_FILE"
        echo "" | tee -a "$SUMMARY_FILE"
        echo "Run analyze script: ./analyze_performance_data.sh $DATA_FILE" | tee -a "$SUMMARY_FILE"
    fi
}

echo "Collecting data every ${SAMPLE_INTERVAL}s (press Ctrl+C to stop)..."
echo ""

while true; do
    # Get timestamp
    ts=$(date '+%Y-%m-%d %H:%M:%S')

    # Get container uptime in seconds
    uptime_str=$(docker inspect gps-receivers-scheduler-dev --format '{{.State.StartedAt}}' 2>/dev/null)
    if [ -n "$uptime_str" ]; then
        start_epoch=$(date -d "$uptime_str" +%s 2>/dev/null || echo 0)
        now_epoch=$(date +%s)
        uptime_seconds=$((now_epoch - start_epoch))
    else
        uptime_seconds=0
    fi

    # Count active downloads (unique stations with "Downloading" in progress)
    active=$(docker logs --tail 500 gps-receivers-scheduler-dev 2>&1 | \
        grep "Downloading.*\.gz:" | \
        grep -oP '[A-Z0-9]{4}(?=\d{7})' | \
        sort -u | wc -l | tr -d '\n\r ')

    # Count starts and completions in last 30s (directly without storing logs)
    started=$(docker logs --since 30s gps-receivers-scheduler-dev 2>&1 | grep "Starting download:" | wc -l | tr -d '\n\r ')
    completed=$(docker logs --since 30s gps-receivers-scheduler-dev 2>&1 | grep "Completed:" | wc -l | tr -d '\n\r ')

    # Count total completions in last 5 minutes
    total_5min=$(docker logs --since 5m gps-receivers-scheduler-dev 2>&1 | \
        grep "Completed:" | wc -l | tr -d '\n\r ')

    # Write to CSV (use printf to ensure single line, no newlines in variables)
    printf "%s,%d,%d,%d,%d,%d\n" "$ts" "$active" "$started" "$completed" "$total_5min" "$uptime_seconds" >> "$DATA_FILE"

    # Increment sample counter
    ((sample_count++))

    # Display progress every 10 samples (5 minutes)
    if [ $((sample_count % 10)) -eq 0 ]; then
        echo "[$(date '+%H:%M:%S')] Sample #$sample_count - Active: $active, Rate: $(echo "scale=1; $total_5min/5" | bc 2>/dev/null || echo 0) jobs/min"
    fi

    sleep $SAMPLE_INTERVAL
done
