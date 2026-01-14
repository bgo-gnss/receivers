# GPS Receivers Performance Monitoring

Overnight monitoring system for GPS download scheduler with 200 workers.

## Quick Start

```bash
# Start monitoring (runs in background)
./start_monitoring.sh

# Check status
./monitoring_status.sh

# Analyze collected data
./analyze_performance_data.sh /tmp/gps_performance_data/performance_*.csv

# Stop monitoring
pkill -f collect_performance_data.sh
```

## What It Does

The monitoring system:
- **Samples every 30 seconds** to track concurrent downloads
- **Collects metrics**: active workers, job starts, job completions, container uptime
- **Generates 15-minute statistics**: max, min, average concurrent workers
- **Runs overnight** without supervision
- **Saves to CSV** for analysis

## Current Configuration

- **Max Workers**: 200 (from scheduler.yaml line 17)
- **Schedule**: Every 10 minutes for all 3 session types
- **Stations**: 176 for 15s_24hr and 1Hz_1hr, 106 for status_1hr
- **Distribution Window**: 10 minutes (spreads jobs evenly)

## Data Files

All data stored in `/tmp/gps_performance_data/`:

```
performance_YYYYMMDD_HHMMSS.csv  # Raw samples (CSV format)
monitor.log                       # Monitoring script log
summary_YYYYMMDD_HHMMSS.txt      # Summary statistics
```

### CSV Format

```csv
timestamp,active_downloads,started_30s,completed_30s,total_jobs_5min,container_uptime_seconds
2025-10-11 01:05:43,6,0,0,8,1023
```

**Columns**:
- `timestamp`: Sample time
- `active_downloads`: Number of concurrent workers actively downloading
- `started_30s`: Jobs started in last 30 seconds
- `completed_30s`: Jobs completed in last 30 seconds
- `total_jobs_5min`: Total jobs completed in last 5 minutes
- `container_uptime_seconds`: Container uptime in seconds

## Analysis Tools

### Monitoring Status
```bash
./monitoring_status.sh
```
Shows:
- Current active workers
- Completion rate (jobs/min)
- Worker utilization (active/200)
- Last 15 minutes: max, avg, min concurrent

### Data Analysis
```bash
./analyze_performance_data.sh <csv_file>
```
Generates:
- **Overall statistics**: max, min, avg workers across entire run
- **15-minute windows**: detailed stats per window
- **Hourly summary**: aggregated by hour
- **Peak moments**: top 20 concurrency peaks

Example output:
```
Overall Statistics:
  Total samples: 120
  Duration: 1.0 hours

  Active downloads:
    Maximum: 45 workers
    Minimum: 0 workers
    Average: 12.3 workers

  Job completion rate:
    Average: 3.2 jobs/min

15-Minute Window Statistics:
Window  1 (2025-10-11 01:00:00):
  Active downloads - Max:  45, Min:   0, Avg:  12.3
  Job starts:      85 total (  5.7 jobs/min)
  Job completions:  82 total (  5.5 jobs/min)
  Samples: 30
```

### Real-time Monitoring
```bash
# Watch live log
tail -f /tmp/gps_performance_data/monitor.log

# Quick concurrent check
./check_concurrent.sh

# Detailed live monitor (refreshes every 5s)
./monitor_concurrent.sh
```

## Expected Behavior

### Typical Pattern (10-minute schedule)
```
00:XX:42 - Jobs scheduled (every 10 minutes)
00:XX:42 - First jobs start (distributed over 10 min window)
00:XX:45 - Concurrency ramps up (20-30 workers)
00:XX:50 - Peak concurrency (40-80 workers)
00:XX:55 - Gradual decline as jobs complete
00:X+1:02 - Most jobs complete
00:X+1:10 - Window ends, low activity
```

### Key Metrics to Watch

**Good Performance**:
- Peak concurrency: 50-100 workers (utilization 25-50%)
- Average concurrent: 20-40 workers
- Job completion rate: 5-15 jobs/min
- Most jobs complete within 2-5 minutes

**Potential Issues**:
- Peak concurrency <10: Schedule too sparse or network issues
- Average >150: Possible bottleneck (network or receivers)
- Completion rate <2 jobs/min: Slow connections or timeouts

## Network Insight

Your hypothesis: **Network is not the bottleneck**

With 200 workers:
- Your laptop: High-speed network (Gbps+)
- Each receiver: Slow connection (often 2-50 KB/s, cellular/satellite)
- **Bottleneck is receiver connections**, not your network

Therefore:
- 50-100 concurrent downloads feasible
- Limited by: receiver availability, connection stability
- 200 workers gives headroom for burst periods

## Scheduler Schedule

From `scheduler.yaml`:
```yaml
sessions:
  15s_24hr:
    schedule: "10m"  # Every 10 minutes (stress test)
    distribution_window: 10
    lookback_periods: 7  # Check last 7 days

  1Hz_1hr:
    schedule: "10m"
    distribution_window: 10
    lookback_periods: 24  # Check last 24 hours

  status_1hr:
    schedule: "10m"
    distribution_window: 10
    lookback_periods: 24
```

**Next batch starts**: Every 10 minutes (e.g., 01:08, 01:18, 01:28, ...)

## Stopping and Cleanup

```bash
# Stop monitoring
pkill -f collect_performance_data.sh

# View final summary
cat /tmp/gps_performance_data/summary_*.txt

# Cleanup old data (keeps today's data)
find /tmp/gps_performance_data -name "performance_*.csv" -mtime +1 -delete

# Complete cleanup (deletes ALL data)
rm -rf /tmp/gps_performance_data/
```

## Troubleshooting

### Monitoring not collecting data
```bash
# Check if running
pgrep -f collect_performance_data.sh

# Check log for errors
tail -f /tmp/gps_performance_data/monitor.log

# Restart monitoring
pkill -f collect_performance_data.sh
./start_monitoring.sh
```

### CSV data looks corrupted
The script now uses `printf` and `tr -d '\n\r'` to ensure clean CSV output.
If you see multiline entries, restart monitoring.

### Container not accessible
```bash
# Check container status
docker ps --filter name=gps-receivers-scheduler-dev

# Check container logs
docker logs --tail 50 gps-receivers-scheduler-dev
```

## Files Created

```bash
receivers/
├── collect_performance_data.sh    # Main monitoring script
├── analyze_performance_data.sh    # Analysis tool
├── start_monitoring.sh            # Launcher (runs in background)
├── monitoring_status.sh           # Status checker
├── check_concurrent.sh            # Quick concurrent check
├── monitor_concurrent.sh          # Live monitoring display
└── MONITORING_README.md           # This file
```

---

**Created**: 2025-10-11
**Purpose**: Overnight monitoring of 200-worker GPS scheduler
**Data Directory**: `/tmp/gps_performance_data/`
