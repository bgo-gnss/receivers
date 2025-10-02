# GPS Receivers Scheduler - Monitoring and Health Checks

**Version**: 0.1.0
**Target System**: Veðurstofa Íslands Production Server
**Last Updated**: 2025-10-02

## Overview

This guide covers monitoring, health checks, and alerting for the GPS Receivers Scheduler. The system provides multiple monitoring layers including systemd watchdog, structured logging, performance metrics, and integration points for external monitoring systems.

## Monitoring Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Monitoring Layers                         │
├─────────────────────────────────────────────────────────────┤
│ 1. Systemd Watchdog (300s)                                   │
│    └─ Process health, automatic restart on hang             │
│                                                              │
│ 2. Application Logging                                       │
│    ├─ scheduler.log (human-readable)                        │
│    └─ download_audit.jsonl (structured metrics)             │
│                                                              │
│ 3. System Resource Monitoring                                │
│    ├─ Memory usage (2GB limit)                              │
│    ├─ CPU usage (200% limit)                                │
│    └─ File descriptors (65536 limit)                        │
│                                                              │
│ 4. Network Health                                            │
│    ├─ Active FTP connections                                │
│    ├─ Connection failures                                   │
│    └─ Download success rate                                 │
│                                                              │
│ 5. Data Quality                                              │
│    ├─ File completeness                                     │
│    ├─ Archive integrity                                     │
│    └─ Gap detection                                         │
└─────────────────────────────────────────────────────────────┘
```

## Health Checks

### Service Health

```bash
# Basic service status
sudo systemctl is-active gps-receivers-scheduler
# Output: active | inactive | failed

# Detailed status
sudo systemctl status gps-receivers-scheduler

# Expected output:
● gps-receivers-scheduler.service - GPS Receivers Scheduler
     Loaded: loaded (/etc/systemd/system/gps-receivers-scheduler.service)
     Active: active (running) since Thu 2025-10-02 00:00:00 GMT
   Main PID: 12345 (receivers)
      Tasks: 103 (limit: 65536)
     Memory: 249.5M (max: 2.0G)
        CPU: 1h 23min
     CGroup: /system.slice/gps-receivers-scheduler.service
             └─12345 /opt/miniforge3/envs/gpslibrary/bin/receivers scheduler start

# Check for recent restarts (should be stable)
sudo journalctl -u gps-receivers-scheduler | grep "Started\|Stopped"

# Watchdog status
systemctl show gps-receivers-scheduler | grep Watchdog
# Output: WatchdogTimestampMonotonic=... (should update every 300s)
```

### Process Health

```bash
# Check process is running
ps aux | grep "receivers scheduler start" | grep -v grep

# Resource usage
ps -p $(pgrep -f "receivers scheduler") -o pid,ppid,%cpu,%mem,vsz,rss,tty,stat,start,time,cmd

# Thread count (should be ~max_workers + overhead)
ps -Lf -p $(pgrep -f "receivers scheduler") | wc -l

# Open files
sudo lsof -p $(pgrep -f "receivers scheduler") | wc -l

# Network connections
sudo lsof -i -a -p $(pgrep -f "receivers scheduler")
```

### Scheduler Health

```bash
# Check scheduled jobs
sudo -u gpsops /opt/miniforge3/envs/gpslibrary/bin/receivers scheduler status --show-jobs

# Expected output:
=== Scheduler Status ===
Status: Running
Jobs scheduled: 519 (173 stations × 3 sessions)
Next run: 2025-10-02 08:15:00
Executor: ThreadPoolExecutor (max_workers=100)

Recent jobs (last 10):
- REYK_1Hz_1hr: Next run at 08:15:02
- AKUR_1Hz_1hr: Next run at 08:15:05
- HOFN_1Hz_1hr: Next run at 08:15:08
...

# Check database health
sqlite3 ~/.cache/gps_receivers/scheduler.db "PRAGMA integrity_check;"
# Output: ok

# Job statistics
sqlite3 ~/.cache/gps_receivers/scheduler.db "SELECT COUNT(*) FROM apscheduler_jobs;"
# Output: 519 (or configured number)
```

### Download Health

```bash
# Recent download activity (last hour)
grep "$(date -d '1 hour ago' '+%Y-%m-%d %H')" \
  /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -r '[.status] | group_by(.) | map({status: .[0], count: length})'

# Expected output:
[
  {"status": "success", "count": 85},
  {"status": "failed", "count": 2}
]

# Success rate (last 24 hours)
grep "$(date '+%Y-%m-%d')" \
  /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -s 'map(select(.status == "success")) | length' | \
  awk '{total=173*3; printf "Success rate: %.1f%%\n", ($1/total)*100}'

# Failed downloads (last 24 hours)
grep "$(date '+%Y-%m-%d')" \
  /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -r 'select(.status == "failed") | "\(.timestamp) \(.station) \(.session) - \(.error)"'

# Average download time per session
cat /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -r 'select(.status == "success") | "\(.session) \(.download_time_seconds)"' | \
  awk '{sum[$1]+=$2; count[$1]++} END {for(s in sum) printf "%s: %.1fs\n", s, sum[s]/count[s]}'
```

## Monitoring Metrics

### Key Performance Indicators (KPIs)

#### 1. Download Success Rate
**Target**: >95% success rate per day
**Alert Threshold**: <90% success rate

```bash
# Daily success rate
TODAY=$(date '+%Y-%m-%d')
SUCCESS=$(grep "$TODAY" /var/cache/gps_receivers/logs/download_audit.jsonl | jq -r 'select(.status == "success")' | wc -l)
TOTAL=$(grep "$TODAY" /var/cache/gps_receivers/logs/download_audit.jsonl | wc -l)
echo "Success rate: $(awk "BEGIN {printf \"%.1f%%\", ($SUCCESS/$TOTAL)*100}")"
```

#### 2. Service Uptime
**Target**: 99.9% uptime
**Alert Threshold**: Service down for >5 minutes

```bash
# Service uptime
systemctl show gps-receivers-scheduler --property=ActiveEnterTimestamp,ActiveExitTimestamp

# Time since last restart
systemctl show gps-receivers-scheduler | grep ActiveEnterTimestamp | \
  awk -F= '{print $2}' | xargs -I{} date -d "{}" "+Started: %Y-%m-%d %H:%M:%S (%s seconds ago)"
```

#### 3. Download Latency
**Target**:
- 1Hz_1hr: <30 seconds average
- 15s_24hr: <300 seconds average
- status_1hr: <15 seconds average

**Alert Threshold**: 2x target values

```bash
# Average download time by session (last 24 hours)
grep "$(date '+%Y-%m-%d')" /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -r 'select(.status == "success") | "\(.session) \(.download_time_seconds)"' | \
  awk '{sum[$1]+=$2; count[$1]++; if($2>max[$1]) max[$1]=$2}
       END {for(s in sum) printf "%s: avg=%.1fs, max=%.1fs\n", s, sum[s]/count[s], max[s]}'
```

#### 4. Resource Usage
**Target**:
- Memory: <1.5GB average, <2GB max
- CPU: <50% average
- Disk: /var/cache growing <1GB/week

**Alert Threshold**: Memory >1.8GB, CPU >80%, Disk >100GB

```bash
# Memory usage
ps -p $(pgrep -f "receivers scheduler") -o rss= | awk '{printf "Memory: %.1f MB\n", $1/1024}'

# CPU usage (5-minute average)
top -b -n 12 -d 5 -p $(pgrep -f "receivers scheduler") | \
  grep receivers | awk '{sum+=$9; count++} END {printf "CPU: %.1f%%\n", sum/count}'

# Disk usage
du -sh /var/cache/gps_receivers
du -sh /mnt/gpsdata
```

#### 5. Data Completeness
**Target**: <1% missing data per day
**Alert Threshold**: >5% missing data

```bash
# Expected files per day
EXPECTED=$((173 * 3))  # 173 stations, 3 session types

# Actual archived files today
ARCHIVED=$(find /mnt/gpsdata -name "*.gz" -newermt "$(date '+%Y-%m-%d 00:00')" | wc -l)

echo "Expected: $EXPECTED, Archived: $ARCHIVED"
echo "Completeness: $(awk "BEGIN {printf \"%.1f%%\", ($ARCHIVED/$EXPECTED)*100}")"
```

### Monitoring Dashboards

#### Real-time Dashboard Script

```bash
#!/bin/bash
# GPS Scheduler Monitoring Dashboard
# Save as: /usr/local/bin/gps-scheduler-dashboard

clear
while true; do
  echo "=== GPS Receivers Scheduler - Dashboard ==="
  echo "$(date '+%Y-%m-%d %H:%M:%S')"
  echo ""

  # Service status
  echo "Service Status:"
  systemctl is-active gps-receivers-scheduler | \
    awk '{print "  " ($1=="active" ? "✓" : "✗"), $1}'

  # Uptime
  systemctl show gps-receivers-scheduler | grep ActiveEnterTimestamp | \
    awk -F= '{print "  Uptime:", $2}'

  echo ""

  # Resource usage
  echo "Resource Usage:"
  ps -p $(pgrep -f "receivers scheduler") -o %cpu,%mem,rss | tail -1 | \
    awk '{printf "  CPU: %s%%, Memory: %s%% (%.1f MB)\n", $1, $2, $3/1024}'

  echo ""

  # Recent activity (last 5 minutes)
  echo "Recent Activity (5 min):"
  FIVE_MIN_AGO=$(date -d '5 minutes ago' '+%Y-%m-%d %H:%M')
  grep -h "$FIVE_MIN_AGO\|$(date '+%Y-%m-%d %H:%M')" \
    /var/cache/gps_receivers/logs/download_audit.jsonl 2>/dev/null | \
    jq -r '.status' | sort | uniq -c | \
    awk '{printf "  %s: %d\n", $2, $1}'

  echo ""

  # Failed downloads (last hour)
  echo "Failed Downloads (1 hour):"
  grep "$(date -d '1 hour ago' '+%Y-%m-%d %H')\|$(date '+%Y-%m-%d %H')" \
    /var/cache/gps_receivers/logs/download_audit.jsonl 2>/dev/null | \
    jq -r 'select(.status == "failed") | "  \(.station) - \(.error)"' | \
    tail -5

  echo ""
  echo "Press Ctrl+C to exit. Refreshing in 10 seconds..."
  sleep 10
  clear
done
```

Make executable:
```bash
sudo chmod +x /usr/local/bin/gps-scheduler-dashboard
```

Usage:
```bash
gps-scheduler-dashboard
```

## Alerting

### Critical Alerts (Immediate Action Required)

#### 1. Service Down
**Condition**: Systemd service not active
**Check Frequency**: Every 1 minute
**Action**: Restart service, notify on-call engineer

```bash
# Monitoring check script
#!/bin/bash
if ! systemctl is-active --quiet gps-receivers-scheduler; then
  echo "CRITICAL: GPS scheduler service is down"
  sudo systemctl restart gps-receivers-scheduler
  # Send alert via email/SMS/Icinga
fi
```

#### 2. Repeated Restart Loops
**Condition**: >3 restarts in 5 minutes
**Check Frequency**: Every 5 minutes
**Action**: Stop service, investigate logs, notify team

```bash
# Check restart count
RESTARTS=$(journalctl -u gps-receivers-scheduler --since "5 minutes ago" | \
  grep -c "Started\|Stopped")

if [ $RESTARTS -gt 3 ]; then
  echo "CRITICAL: Service restarting repeatedly ($RESTARTS times in 5 minutes)"
  # Alert and investigate
fi
```

#### 3. Memory Leak Detection
**Condition**: Memory usage >1.8GB or growing >100MB/hour
**Check Frequency**: Every 15 minutes
**Action**: Restart service during quiet period

```bash
# Memory growth check
CURRENT_MEM=$(ps -p $(pgrep -f "receivers scheduler") -o rss= | awk '{print $1/1024}')
if (( $(echo "$CURRENT_MEM > 1800" | bc -l) )); then
  echo "CRITICAL: Memory usage at ${CURRENT_MEM}MB (limit 2048MB)"
  # Schedule restart during low-activity period
fi
```

### Warning Alerts (Investigation Required)

#### 1. Low Success Rate
**Condition**: <90% success rate in last hour
**Check Frequency**: Every hour
**Action**: Check network, investigate failed stations

```bash
# Success rate check
HOUR_AGO=$(date -d '1 hour ago' '+%Y-%m-%d %H')
CURRENT_HOUR=$(date '+%Y-%m-%d %H')

SUCCESS=$(grep -h "$HOUR_AGO\|$CURRENT_HOUR" \
  /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -r 'select(.status == "success")' | wc -l)

TOTAL=$(grep -h "$HOUR_AGO\|$CURRENT_HOUR" \
  /var/cache/gps_receivers/logs/download_audit.jsonl | wc -l)

SUCCESS_RATE=$(awk "BEGIN {printf \"%.1f\", ($SUCCESS/$TOTAL)*100}")

if (( $(echo "$SUCCESS_RATE < 90" | bc -l) )); then
  echo "WARNING: Download success rate at ${SUCCESS_RATE}% (threshold 90%)"
  # Send warning alert
fi
```

#### 2. Slow Downloads
**Condition**: Average download time >2x normal
**Check Frequency**: Every hour
**Action**: Check network bandwidth, receiver connectivity

```bash
# Download time check
HOUR_AGO=$(date -d '1 hour ago' '+%Y-%m-%d %H')
grep "$HOUR_AGO" /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -r 'select(.status == "success" and .session == "1Hz_1hr") | .download_time_seconds' | \
  awk '{sum+=$1; count++} END {avg=sum/count; if(avg>60) print "WARNING: Average 1Hz_1hr download time:", avg, "seconds (normal <30s)"}'
```

#### 3. Disk Space
**Condition**: /var/cache or /mnt/gpsdata >80% full
**Check Frequency**: Every hour
**Action**: Review log retention, archive cleanup

```bash
# Disk usage check
df /var/cache/gps_receivers | awk 'NR==2 {if($5+0 > 80) print "WARNING: /var/cache at "$5" capacity"}'
df /mnt/gpsdata | awk 'NR==2 {if($5+0 > 80) print "WARNING: /mnt/gpsdata at "$5" capacity"}'
```

### Information Alerts (Awareness)

#### 1. Station Offline
**Condition**: Station fails >5 consecutive downloads
**Check Frequency**: Daily
**Action**: Update station status, plan maintenance

```bash
# Detect consistently failing stations
cat /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -r 'select(.status == "failed") | .station' | \
  sort | uniq -c | awk '$1 > 5 {print "INFO: Station "$2" has "$1" failures"}'
```

#### 2. Unusual Network Activity
**Condition**: >200 concurrent FTP connections
**Check Frequency**: Every 5 minutes
**Action**: Monitor for network issues

```bash
# Connection count
CONNECTIONS=$(sudo lsof -i -a -p $(pgrep -f "receivers scheduler") | grep -c ESTABLISHED)
if [ $CONNECTIONS -gt 200 ]; then
  echo "INFO: High concurrent connections: $CONNECTIONS"
fi
```

## External Monitoring Integration

### Icinga 2 Integration

Create check script at `/usr/local/bin/check_gps_scheduler`:

```bash
#!/bin/bash
# Icinga check for GPS Receivers Scheduler

# Check service is active
if ! systemctl is-active --quiet gps-receivers-scheduler; then
  echo "CRITICAL: Service not running"
  exit 2
fi

# Check recent downloads (last hour)
HOUR_AGO=$(date -d '1 hour ago' '+%Y-%m-%d %H')
SUCCESS=$(grep "$HOUR_AGO" /var/cache/gps_receivers/logs/download_audit.jsonl 2>/dev/null | \
  jq -r 'select(.status == "success")' | wc -l)
FAILED=$(grep "$HOUR_AGO" /var/cache/gps_receivers/logs/download_audit.jsonl 2>/dev/null | \
  jq -r 'select(.status == "failed")' | wc -l)

TOTAL=$((SUCCESS + FAILED))
if [ $TOTAL -eq 0 ]; then
  echo "WARNING: No downloads in last hour"
  exit 1
fi

SUCCESS_RATE=$(awk "BEGIN {printf \"%.1f\", ($SUCCESS/$TOTAL)*100}")

# Performance data
echo "OK: Success rate ${SUCCESS_RATE}% ($SUCCESS/$TOTAL) | success=$SUCCESS failed=$FAILED rate=$SUCCESS_RATE%"
exit 0
```

Icinga configuration:
```
object CheckCommand "gps-scheduler" {
  command = [ "/usr/local/bin/check_gps_scheduler" ]
}

object Service "gps-scheduler-health" {
  host_name = "gps-server"
  check_command = "gps-scheduler"
  check_interval = 5m
  retry_interval = 1m
}
```

### Prometheus Integration

Expose metrics at `/var/cache/gps_receivers/metrics.prom`:

```python
# Add to scheduler code
def export_metrics():
    """Export metrics in Prometheus format"""
    metrics_file = Path("/var/cache/gps_receivers/metrics.prom")

    # Query download statistics
    success_count = db.query("SELECT COUNT(*) FROM downloads WHERE status='success' AND timestamp > datetime('now', '-1 hour')")[0]
    failed_count = db.query("SELECT COUNT(*) FROM downloads WHERE status='failed' AND timestamp > datetime('now', '-1 hour')")[0]

    metrics = f"""
# HELP gps_scheduler_downloads_total Total downloads by status
# TYPE gps_scheduler_downloads_total counter
gps_scheduler_downloads_total{{status="success"}} {success_count}
gps_scheduler_downloads_total{{status="failed"}} {failed_count}

# HELP gps_scheduler_uptime_seconds Service uptime in seconds
# TYPE gps_scheduler_uptime_seconds gauge
gps_scheduler_uptime_seconds {time.time() - start_time}

# HELP gps_scheduler_memory_bytes Memory usage in bytes
# TYPE gps_scheduler_memory_bytes gauge
gps_scheduler_memory_bytes {psutil.Process().memory_info().rss}
"""

    metrics_file.write_text(metrics)
```

Node exporter config:
```yaml
# /etc/prometheus/node_exporter.yml
textfile:
  directory: /var/cache/gps_receivers
  files:
    - metrics.prom
```

### Email Alerts

Configure email alerts for critical events:

```bash
# /etc/gps-scheduler/alert.sh
#!/bin/bash
ALERT_EMAIL="gps-validation@vedur.is"
SUBJECT="$1"
MESSAGE="$2"

echo "$MESSAGE" | mail -s "GPS Scheduler Alert: $SUBJECT" "$ALERT_EMAIL"
```

Usage in monitoring scripts:
```bash
if [ $SERVICE_DOWN ]; then
  /etc/gps-scheduler/alert.sh "Service Down" "GPS Scheduler service is not running on $(hostname)"
fi
```

## Log Analysis

### Daily Report Generation

```bash
#!/bin/bash
# Generate daily report
# Save as: /usr/local/bin/gps-scheduler-daily-report

YESTERDAY=$(date -d yesterday '+%Y-%m-%d')
REPORT_FILE="/var/cache/gps_receivers/reports/daily-${YESTERDAY}.txt"

{
  echo "GPS Receivers Scheduler - Daily Report"
  echo "Date: $YESTERDAY"
  echo "Generated: $(date)"
  echo ""

  echo "=== Download Statistics ==="
  grep "$YESTERDAY" /var/cache/gps_receivers/logs/download_audit.jsonl | \
    jq -r '.status' | sort | uniq -c | \
    awk '{printf "%s: %d\n", $2, $1}'

  echo ""
  echo "=== Failed Downloads by Station ==="
  grep "$YESTERDAY" /var/cache/gps_receivers/logs/download_audit.jsonl | \
    jq -r 'select(.status == "failed") | .station' | \
    sort | uniq -c | sort -rn | head -20

  echo ""
  echo "=== Average Download Times ==="
  grep "$YESTERDAY" /var/cache/gps_receivers/logs/download_audit.jsonl | \
    jq -r 'select(.status == "success") | "\(.session) \(.download_time_seconds)"' | \
    awk '{sum[$1]+=$2; count[$1]++} END {for(s in sum) printf "%s: %.1fs\n", s, sum[s]/count[s]}'

  echo ""
  echo "=== Service Restarts ==="
  journalctl -u gps-receivers-scheduler --since "$YESTERDAY" --until "$(date -d yesterday '+%Y-%m-%d') 23:59:59" | \
    grep -c "Started\|Stopped"

} > "$REPORT_FILE"

# Email report
mail -s "GPS Scheduler Daily Report - $YESTERDAY" gps-validation@vedur.is < "$REPORT_FILE"
```

Cron job:
```cron
# Run daily at 00:30
30 0 * * * /usr/local/bin/gps-scheduler-daily-report
```

## Troubleshooting Common Issues

### High Memory Usage

```bash
# 1. Check memory trend
ps -p $(pgrep -f "receivers scheduler") -o %mem,rss,vsz,cmd --sort=-rss

# 2. Check for memory leaks
# Monitor over time:
watch -n 60 'ps -p $(pgrep -f "receivers scheduler") -o rss='

# 3. Restart during quiet period if needed
# Schedule restart at 3 AM daily
0 3 * * * systemctl restart gps-receivers-scheduler
```

### Failed Downloads

```bash
# 1. Identify failing stations
cat /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -r 'select(.status == "failed") | "\(.station) - \(.error)"' | \
  sort | uniq -c | sort -rn

# 2. Test specific station
sudo -u gpsops receivers download <STATION> --test-connection --verbose

# 3. Check network connectivity
ping <receiver-ip>
telnet <receiver-ip> 21

# 4. Review recent logs
tail -100 /var/cache/gps_receivers/logs/scheduler.log | grep <STATION>
```

### Service Crashes

```bash
# 1. Check crash logs
sudo journalctl -u gps-receivers-scheduler -p err --since "1 hour ago"

# 2. Check for Python tracebacks
tail -200 /var/cache/gps_receivers/logs/scheduler.log | grep -A 20 "Traceback"

# 3. Check system resources at time of crash
journalctl --since "1 hour ago" | grep -i "out of memory\|killed"

# 4. Review systemd service limits
systemctl show gps-receivers-scheduler | grep -i limit
```

## Best Practices

1. **Regular Monitoring**: Check dashboard at least once daily
2. **Alert Tuning**: Adjust thresholds based on observed patterns
3. **Log Retention**: Keep 30 days of detailed logs, 90 days of audit trails
4. **Capacity Planning**: Monitor disk growth, plan archive expansion
5. **Documentation**: Update runbooks with new failure patterns
6. **Automation**: Automate common remediation tasks
7. **Testing**: Regular failover and recovery testing

## Support Contacts

- **GPS Team**: gps-validation@vedur.is
- **DevOps On-Call**: [on-call number]
- **Escalation**: [manager contact]

---

**Document Version**: 1.0
**Last Review**: 2025-10-02
**Next Review**: 2025-11-01
