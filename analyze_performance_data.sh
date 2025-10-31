#!/bin/bash
# Analyze performance data collected by collect_performance_data.sh
# Generates 15-minute windowed statistics

DATA_FILE="${1:-/tmp/gps_performance_data/performance_*.csv}"

if [ ! -f "$DATA_FILE" ]; then
    echo "Usage: $0 <data_file.csv>"
    echo ""
    echo "Available data files:"
    ls -lh /tmp/gps_performance_data/performance_*.csv 2>/dev/null || echo "  No data files found"
    exit 1
fi

echo "=== GPS Receivers Performance Analysis ==="
echo "Data file: $DATA_FILE"
echo ""

# Overall statistics
echo "Overall Statistics:"
echo "==================="
awk -F',' 'NR>1 {
    sum+=$2; count++;
    sum_rate+=$5/5; # 5-minute window average
    if($2>max) max=$2;
    if(min=="" || $2<min) min=$2;
    if($2>0) active_samples++;
}
END {
    printf "  Total samples: %d\n", count
    printf "  Duration: %.1f hours\n", count*30/3600
    printf "  \n"
    printf "  Active downloads:\n"
    printf "    Maximum: %d workers\n", max
    printf "    Minimum: %d workers\n", min
    printf "    Average: %.1f workers\n", sum/count
    printf "    Samples with activity: %d (%.1f%%)\n", active_samples, active_samples*100/count
    printf "  \n"
    printf "  Job completion rate:\n"
    printf "    Average: %.1f jobs/min\n", sum_rate/count
}' "$DATA_FILE"

echo ""
echo "15-Minute Window Statistics:"
echo "============================"

# Process in 15-minute windows (30 samples per window at 30s interval)
awk -F',' '
BEGIN {
    window_size = 30;  # 30 samples * 30s = 15 minutes
    window_num = 0;
}
NR>1 {
    # Parse timestamp
    timestamp = $1;
    active = $2;
    started = $3;
    completed = $4;
    total_5min = $5;

    # Add to current window
    window_active[window_num] = window_active[window_num] " " active;
    window_started[window_num] += started;
    window_completed[window_num] += completed;
    window_timestamps[window_num] = window_timestamps[window_num] " " timestamp;
    window_count[window_num]++;

    # Move to next window when full
    if (window_count[window_num] >= window_size) {
        window_num++;
    }
}
END {
    # Analyze each window
    for (w = 0; w <= window_num; w++) {
        if (window_count[w] == 0) continue;

        # Get first timestamp for this window
        split(window_timestamps[w], ts_arr, " ");
        start_time = ts_arr[2];  # Skip first empty element

        # Calculate statistics for active downloads
        split(window_active[w], vals, " ");
        max_active = 0;
        sum_active = 0;
        count_active = 0;
        min_active = 999999;

        for (i in vals) {
            if (vals[i] == "") continue;
            val = vals[i] + 0;
            sum_active += val;
            count_active++;
            if (val > max_active) max_active = val;
            if (val < min_active) min_active = val;
        }

        avg_active = (count_active > 0) ? sum_active / count_active : 0;

        # Calculate job rate (jobs per minute)
        total_started = window_started[w];
        total_completed = window_completed[w];
        duration_min = window_count[w] * 0.5;  # 30s samples = 0.5 min each
        rate_started = total_started / duration_min;
        rate_completed = total_completed / duration_min;

        printf "Window %2d (%s):\n", w+1, start_time;
        printf "  Active downloads - Max: %3d, Min: %3d, Avg: %5.1f\n", max_active, min_active, avg_active;
        printf "  Job starts:    %4d total (%5.1f jobs/min)\n", total_started, rate_started;
        printf "  Job completions: %4d total (%5.1f jobs/min)\n", total_completed, rate_completed;
        printf "  Samples: %d\n", window_count[w];
        printf "\n";
    }
}' "$DATA_FILE"

echo ""
echo "Hourly Summary:"
echo "==============="

# Hourly statistics
awk -F',' '
NR>1 {
    # Extract hour from timestamp (format: YYYY-MM-DD HH:MM:SS)
    split($1, dt, " ");
    split(dt[2], tm, ":");
    hour = tm[1];

    active = $2;
    completed = $4;

    # Accumulate per hour
    hour_active[hour] = hour_active[hour] " " active;
    hour_completed[hour] += completed;
    hour_count[hour]++;
}
END {
    # Sort hours
    n = asorti(hour_count, sorted_hours);

    for (i = 1; i <= n; i++) {
        h = sorted_hours[i];

        # Calculate stats for this hour
        split(hour_active[h], vals, " ");
        max_active = 0;
        sum_active = 0;
        count = 0;

        for (j in vals) {
            if (vals[j] == "") continue;
            val = vals[j] + 0;
            sum_active += val;
            count++;
            if (val > max_active) max_active = val;
        }

        avg_active = (count > 0) ? sum_active / count : 0;
        duration_min = hour_count[h] * 0.5;  # 30s samples
        rate = hour_completed[h] / duration_min;

        printf "Hour %s:00 - Max: %3d, Avg: %5.1f concurrent | %4d jobs (%4.1f jobs/min)\n", \
            h, max_active, avg_active, hour_completed[h], rate;
    }
}' "$DATA_FILE"

echo ""
echo "Peak Concurrency Moments:"
echo "========================="

# Find top 10 peak concurrency moments
awk -F',' 'NR>1 {print $2, $1}' "$DATA_FILE" | \
    sort -rn | \
    head -20 | \
    awk '{printf "  %3d workers at %s\n", $1, $2" "$3}'

echo ""
echo "Analysis complete!"
