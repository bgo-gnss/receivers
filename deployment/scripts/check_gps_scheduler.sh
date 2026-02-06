#!/bin/bash
# =============================================================================
# GPS Pipeline Scheduler Health Check
# =============================================================================
# For use with Icinga/Nagios monitoring
# Exit codes: 0=OK, 1=WARNING, 2=CRITICAL, 3=UNKNOWN
#
# Usage: check_gps_scheduler.sh [--verbose]
# =============================================================================

set -e

CACHE_DIR="${GPS_CACHE_DIR:-/var/cache/gps_receivers}"
LOG_DIR="$CACHE_DIR/logs"
AUDIT_LOG="$LOG_DIR/download_audit.jsonl"
PIPELINE_DB="$LOG_DIR/pipeline.db"
SCHEDULER_DB="$CACHE_DIR/scheduler.db"

VERBOSE=false
[ "$1" == "--verbose" ] && VERBOSE=true

# Counters for performance data
WARNINGS=0
CRITICALS=0
MESSAGES=""

add_msg() {
    MESSAGES="${MESSAGES}$1\n"
}

# -----------------------------------------------------------------------------
# Check 1: Service Status
# -----------------------------------------------------------------------------
check_service() {
    if ! systemctl is-active --quiet gps-receivers-scheduler 2>/dev/null; then
        add_msg "CRITICAL: Scheduler service is not running"
        CRITICALS=$((CRITICALS + 1))
        return
    fi

    # Check for restart loop
    RESTARTS=$(journalctl -u gps-receivers-scheduler --since "10 minutes ago" 2>/dev/null | grep -c "Started\|Stopped" || echo 0)
    if [ "$RESTARTS" -gt 4 ]; then
        add_msg "WARNING: Service restarted $RESTARTS times in last 10 minutes"
        WARNINGS=$((WARNINGS + 1))
    fi
}

# -----------------------------------------------------------------------------
# Check 2: Download Success Rate (last hour)
# -----------------------------------------------------------------------------
check_downloads() {
    if [ ! -f "$AUDIT_LOG" ]; then
        add_msg "WARNING: Audit log not found: $AUDIT_LOG"
        WARNINGS=$((WARNINGS + 1))
        return
    fi

    HOUR_AGO=$(date -d '1 hour ago' '+%Y-%m-%dT%H')
    CURRENT=$(date '+%Y-%m-%dT%H')

    SUCCESS=$(grep -h "$HOUR_AGO\|$CURRENT" "$AUDIT_LOG" 2>/dev/null | grep -c '"status":"success"' || echo 0)
    FAILED=$(grep -h "$HOUR_AGO\|$CURRENT" "$AUDIT_LOG" 2>/dev/null | grep -c '"status":"failed"' || echo 0)
    TOTAL=$((SUCCESS + FAILED))

    if [ "$TOTAL" -eq 0 ]; then
        add_msg "WARNING: No download activity in last hour"
        WARNINGS=$((WARNINGS + 1))
        return
    fi

    SUCCESS_RATE=$(awk "BEGIN {printf \"%.1f\", ($SUCCESS/$TOTAL)*100}")

    if (( $(echo "$SUCCESS_RATE < 80" | bc -l) )); then
        add_msg "CRITICAL: Download success rate ${SUCCESS_RATE}% (threshold 80%)"
        CRITICALS=$((CRITICALS + 1))
    elif (( $(echo "$SUCCESS_RATE < 90" | bc -l) )); then
        add_msg "WARNING: Download success rate ${SUCCESS_RATE}% (threshold 90%)"
        WARNINGS=$((WARNINGS + 1))
    fi

    # Store for perfdata
    DOWNLOAD_SUCCESS=$SUCCESS
    DOWNLOAD_FAILED=$FAILED
    DOWNLOAD_RATE=$SUCCESS_RATE
}

# -----------------------------------------------------------------------------
# Check 3: Pipeline Health
# -----------------------------------------------------------------------------
check_pipelines() {
    if [ ! -f "$PIPELINE_DB" ]; then
        $VERBOSE && add_msg "INFO: Pipeline database not found (may not be using pipelines)"
        return
    fi

    # Check for stuck pipelines (running > 2 hours)
    STUCK=$(sqlite3 "$PIPELINE_DB" "SELECT COUNT(*) FROM pipeline_jobs WHERE completed=0 AND created_at < datetime('now', '-2 hours');" 2>/dev/null || echo 0)

    if [ "$STUCK" -gt 0 ]; then
        add_msg "WARNING: $STUCK pipelines stuck (>2 hours old)"
        WARNINGS=$((WARNINGS + 1))
    fi

    # Check incomplete pipeline count
    INCOMPLETE=$(sqlite3 "$PIPELINE_DB" "SELECT COUNT(*) FROM pipeline_jobs WHERE completed=0;" 2>/dev/null || echo 0)

    if [ "$INCOMPLETE" -gt 50 ]; then
        add_msg "WARNING: $INCOMPLETE incomplete pipelines (threshold 50)"
        WARNINGS=$((WARNINGS + 1))
    fi

    PIPELINE_INCOMPLETE=$INCOMPLETE
    PIPELINE_STUCK=$STUCK
}

# -----------------------------------------------------------------------------
# Check 4: Resource Usage
# -----------------------------------------------------------------------------
check_resources() {
    PID=$(pgrep -f "receivers scheduler" 2>/dev/null | head -1)

    if [ -z "$PID" ]; then
        return  # Service check already handles this
    fi

    # Memory usage (in MB)
    MEM_MB=$(ps -p "$PID" -o rss= 2>/dev/null | awk '{print int($1/1024)}' || echo 0)

    if [ "$MEM_MB" -gt 3500 ]; then
        add_msg "CRITICAL: Memory usage ${MEM_MB}MB (limit 4GB)"
        CRITICALS=$((CRITICALS + 1))
    elif [ "$MEM_MB" -gt 2500 ]; then
        add_msg "WARNING: Memory usage ${MEM_MB}MB approaching limit"
        WARNINGS=$((WARNINGS + 1))
    fi

    # CPU usage
    CPU=$(ps -p "$PID" -o %cpu= 2>/dev/null | awk '{print int($1)}' || echo 0)

    if [ "$CPU" -gt 350 ]; then
        add_msg "WARNING: CPU usage ${CPU}% (limit 400%)"
        WARNINGS=$((WARNINGS + 1))
    fi

    RESOURCE_MEM=$MEM_MB
    RESOURCE_CPU=$CPU
}

# -----------------------------------------------------------------------------
# Check 5: Disk Space
# -----------------------------------------------------------------------------
check_disk() {
    CACHE_USAGE=$(df "$CACHE_DIR" 2>/dev/null | awk 'NR==2 {print int($5)}' || echo 0)

    if [ "$CACHE_USAGE" -gt 90 ]; then
        add_msg "CRITICAL: Cache disk at ${CACHE_USAGE}% capacity"
        CRITICALS=$((CRITICALS + 1))
    elif [ "$CACHE_USAGE" -gt 80 ]; then
        add_msg "WARNING: Cache disk at ${CACHE_USAGE}% capacity"
        WARNINGS=$((WARNINGS + 1))
    fi

    DISK_CACHE=$CACHE_USAGE
}

# -----------------------------------------------------------------------------
# Run All Checks
# -----------------------------------------------------------------------------
check_service
check_downloads
check_pipelines
check_resources
check_disk

# -----------------------------------------------------------------------------
# Output Results
# -----------------------------------------------------------------------------
if [ "$CRITICALS" -gt 0 ]; then
    STATUS="CRITICAL"
    EXIT_CODE=2
elif [ "$WARNINGS" -gt 0 ]; then
    STATUS="WARNING"
    EXIT_CODE=1
else
    STATUS="OK"
    EXIT_CODE=0
fi

# Build output
OUTPUT="$STATUS: GPS Scheduler"
[ "$CRITICALS" -gt 0 ] && OUTPUT="$OUTPUT - $CRITICALS critical"
[ "$WARNINGS" -gt 0 ] && OUTPUT="$OUTPUT - $WARNINGS warning"
[ "$CRITICALS" -eq 0 ] && [ "$WARNINGS" -eq 0 ] && OUTPUT="$OUTPUT - All checks passed"

# Add performance data
PERFDATA="downloads_success=${DOWNLOAD_SUCCESS:-0} downloads_failed=${DOWNLOAD_FAILED:-0} success_rate=${DOWNLOAD_RATE:-0}%"
PERFDATA="$PERFDATA pipelines_incomplete=${PIPELINE_INCOMPLETE:-0} pipelines_stuck=${PIPELINE_STUCK:-0}"
PERFDATA="$PERFDATA memory_mb=${RESOURCE_MEM:-0} cpu_pct=${RESOURCE_CPU:-0} disk_pct=${DISK_CACHE:-0}%"

echo "$OUTPUT | $PERFDATA"

# Verbose output
if $VERBOSE && [ -n "$MESSAGES" ]; then
    echo ""
    echo "Details:"
    echo -e "$MESSAGES"
fi

exit $EXIT_CODE
