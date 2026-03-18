#!/bin/bash
# Prototype: Send GPS health data to Icinga
# Usage: ./test_health_to_icinga.sh STATION [DATE]
#
# This script extracts health metrics from daily JSON files and sends
# them to Icinga monitoring system as passive check results.

set -e

# ======================================================================
# CONFIGURATION
# ======================================================================

STATION="${1:-ELEY}"
DATE="${2:-$(date +%Y%m%d)}"

# Paths - adjust based on actual data location
YEAR=$(date -d "$DATE" +%Y)
MONTH=$(date -d "$DATE" +%b | tr '[:upper:]' '[:lower:]')
JSON_FILE="/data/${YEAR}/${MONTH}/${STATION}/status_1hr/json/${STATION}_${DATE}_health.json"

# Icinga API configuration
ICINGA_API="https://ut-icinga-m-vip.vedur.is:5665/v1"
ICINGA_USER="icingaweb"
ICINGA_PASS="ji5Aeb8oopieGoh"
HOSTNAME="${STATION,,}.gps.vedur.is"  # Lowercase station name
CHECK_SOURCE="eldey"

# Health thresholds
VOLTAGE_WARN=11.5
VOLTAGE_CRIT=11.0
CPU_WARN=75
CPU_CRIT=90
TEMP_WARN=60
TEMP_CRIT=70
DISK_WARN=80
DISK_CRIT=90
SATS_WARN=6
SATS_CRIT=4

# ======================================================================
# FUNCTIONS
# ======================================================================

# Print banner
print_banner() {
    echo "======================================"
    echo "🎯 GPS HEALTH TO ICINGA"
    echo "======================================"
    echo "Station: $STATION"
    echo "Date: $DATE"
    echo "JSON: $JSON_FILE"
    echo "Icinga Host: $HOSTNAME"
    echo "======================================"
}

# Send check result to Icinga
send_to_icinga() {
    local service_name="$1"
    local exit_status="$2"
    local plugin_output="$3"
    local perfdata="$4"

    echo ""
    echo "📤 Sending: ${HOSTNAME}!${service_name}"
    echo "   Status: ${exit_status} (0=OK, 1=WARNING, 2=CRITICAL, 3=UNKNOWN)"
    echo "   Message: ${plugin_output}"
    if [[ -n "$perfdata" ]]; then
        echo "   Perfdata: ${perfdata}"
    fi

    # URL encode service name
    local url_encoded_service=$(echo "$service_name" | sed 's/ /%20/g')

    # Build JSON payload
    local json_data=$(cat <<EOF
{
    "exit_status": ${exit_status},
    "plugin_output": "${plugin_output}",
    "performance_data": "${perfdata}",
    "check_source": "${CHECK_SOURCE}"
}
EOF
)

    echo ""
    echo "💻 Curl command:"
    echo "curl -k -u \"${ICINGA_USER}:***\" \\"
    echo "  -H \"Accept: application/json\" \\"
    echo "  -H \"Content-Type: application/json\" \\"
    echo "  -d '${json_data}' \\"
    echo "  \"${ICINGA_API}/actions/process-check-result?service=${HOSTNAME}!${url_encoded_service}\""

    # Send to Icinga
    local response=$(curl -k -s -w "\n%{http_code}" \
        -u "${ICINGA_USER}:${ICINGA_PASS}" \
        -H "Accept: application/json" \
        -H "Content-Type: application/json" \
        -d "${json_data}" \
        "${ICINGA_API}/actions/process-check-result?service=${HOSTNAME}!${url_encoded_service}")

    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | head -n-1)

    echo ""
    echo "📥 Icinga Response (HTTP ${http_code}):"
    if command -v jq &> /dev/null; then
        echo "$body" | jq '.'
    else
        echo "$body"
    fi

    if [[ "$http_code" == "200" ]]; then
        echo "✅ Success"
    elif [[ "$http_code" == "404" ]]; then
        echo "⚠️  HTTP 404: Service '${service_name}' not found in Icinga"
        echo "   → Create service in Icinga first"
    else
        echo "❌ HTTP $http_code: Check failed"
    fi
}

# Extract value from JSON (with fallback if jq not available)
json_get() {
    local path="$1"

    if command -v jq &> /dev/null; then
        jq -r "${path} // empty" "$JSON_FILE" 2>/dev/null || echo ""
    else
        echo ""
    fi
}

# Compare floats (bc required)
float_compare() {
    local value="$1"
    local operator="$2"
    local threshold="$3"

    if [[ -z "$value" ]] || ! command -v bc &> /dev/null; then
        return 1
    fi

    case "$operator" in
        "lt") (( $(echo "$value < $threshold" | bc -l) )) ;;
        "gt") (( $(echo "$value > $threshold" | bc -l) )) ;;
        "le") (( $(echo "$value <= $threshold" | bc -l) )) ;;
        "ge") (( $(echo "$value >= $threshold" | bc -l) )) ;;
        *) return 1 ;;
    esac
}

# ======================================================================
# MAIN
# ======================================================================

print_banner

# Check dependencies
echo ""
echo "🔍 Checking dependencies..."
if ! command -v jq &> /dev/null; then
    echo "⚠️  Warning: jq not found - JSON parsing may be limited"
    echo "   Install: sudo apt-get install jq"
fi

if ! command -v bc &> /dev/null; then
    echo "❌ Error: bc not found - required for threshold comparisons"
    echo "   Install: sudo apt-get install bc"
    exit 1
fi

if ! command -v curl &> /dev/null; then
    echo "❌ Error: curl not found - required for Icinga API"
    exit 1
fi

# Check if JSON file exists
echo ""
echo "📄 Checking JSON file..."
if [[ ! -f "$JSON_FILE" ]]; then
    echo "❌ ERROR: JSON file not found: $JSON_FILE"
    echo ""
    echo "To create it, run:"
    echo "  receivers health $STATION --extract-day $DATE --save-json"
    exit 1
fi
echo "✅ Found: $JSON_FILE"

# Verify it's valid JSON
if command -v jq &> /dev/null; then
    if ! jq empty "$JSON_FILE" 2>/dev/null; then
        echo "❌ ERROR: Invalid JSON file"
        exit 1
    fi
fi

# ======================================================================
# HEALTH CHECKS
# ======================================================================

# ----------------------------------------------------------------------
# 1. VOLTAGE CHECK
# ----------------------------------------------------------------------
echo ""
echo "========================================"
echo "🔋 1. VOLTAGE CHECK"
echo "========================================"

VOLTAGE=$(json_get '.aggregated.daily.voltage.mean')
VOLTAGE_MIN=$(json_get '.aggregated.daily.voltage.min')
VOLTAGE_MAX=$(json_get '.aggregated.daily.voltage.max')
VOLTAGE_SAMPLES=$(json_get '.aggregated.daily.voltage.samples')

if [[ -z "$VOLTAGE" ]]; then
    send_to_icinga "GPS Health - Voltage" 3 "⚠️ No voltage data available for $DATE" ""
else
    # Determine status
    if float_compare "$VOLTAGE" "lt" "$VOLTAGE_CRIT"; then
        STATUS=2; EMOJI="🔴"; TEXT="CRITICAL"
    elif float_compare "$VOLTAGE" "lt" "$VOLTAGE_WARN"; then
        STATUS=1; EMOJI="⚠️"; TEXT="WARNING"
    else
        STATUS=0; EMOJI="✅"; TEXT="OK"
    fi

    MESSAGE="${EMOJI} Voltage ${TEXT}: ${VOLTAGE}V (range: ${VOLTAGE_MIN}V-${VOLTAGE_MAX}V, ${VOLTAGE_SAMPLES} samples)"
    PERFDATA="voltage=${VOLTAGE}V;${VOLTAGE_WARN};${VOLTAGE_CRIT};10;16 voltage_min=${VOLTAGE_MIN}V voltage_max=${VOLTAGE_MAX}V"

    send_to_icinga "GPS Health - Voltage" "$STATUS" "$MESSAGE" "$PERFDATA"
fi

# ----------------------------------------------------------------------
# 2. CPU LOAD CHECK
# ----------------------------------------------------------------------
echo ""
echo "========================================"
echo "🖥️  2. CPU LOAD CHECK"
echo "========================================"

CPU=$(json_get '.aggregated.daily.cpu_load.mean')
CPU_MIN=$(json_get '.aggregated.daily.cpu_load.min')
CPU_MAX=$(json_get '.aggregated.daily.cpu_load.max')
CPU_SAMPLES=$(json_get '.aggregated.daily.cpu_load.samples')

if [[ -z "$CPU" ]]; then
    send_to_icinga "GPS Health - CPU Load" 3 "⚠️ No CPU data available for $DATE" ""
else
    if float_compare "$CPU" "gt" "$CPU_CRIT"; then
        STATUS=2; EMOJI="🔴"; TEXT="CRITICAL"
    elif float_compare "$CPU" "gt" "$CPU_WARN"; then
        STATUS=1; EMOJI="⚠️"; TEXT="WARNING"
    else
        STATUS=0; EMOJI="✅"; TEXT="OK"
    fi

    MESSAGE="${EMOJI} CPU Load ${TEXT}: ${CPU}% (range: ${CPU_MIN}%-${CPU_MAX}%, ${CPU_SAMPLES} samples)"
    PERFDATA="cpu=${CPU}%;${CPU_WARN};${CPU_CRIT};0;100 cpu_min=${CPU_MIN}% cpu_max=${CPU_MAX}%"

    send_to_icinga "GPS Health - CPU Load" "$STATUS" "$MESSAGE" "$PERFDATA"
fi

# ----------------------------------------------------------------------
# 3. TEMPERATURE CHECK
# ----------------------------------------------------------------------
echo ""
echo "========================================"
echo "🌡️  3. TEMPERATURE CHECK"
echo "========================================"

TEMP=$(json_get '.aggregated.daily.temperature.mean')
TEMP_MIN=$(json_get '.aggregated.daily.temperature.min')
TEMP_MAX=$(json_get '.aggregated.daily.temperature.max')
TEMP_SAMPLES=$(json_get '.aggregated.daily.temperature.samples')

if [[ -z "$TEMP" ]]; then
    send_to_icinga "GPS Health - Temperature" 3 "⚠️ No temperature data available for $DATE" ""
else
    if float_compare "$TEMP" "gt" "$TEMP_CRIT"; then
        STATUS=2; EMOJI="🔴"; TEXT="CRITICAL"
    elif float_compare "$TEMP" "gt" "$TEMP_WARN"; then
        STATUS=1; EMOJI="⚠️"; TEXT="WARNING"
    else
        STATUS=0; EMOJI="✅"; TEXT="OK"
    fi

    MESSAGE="${EMOJI} Temperature ${TEXT}: ${TEMP}°C (range: ${TEMP_MIN}°C-${TEMP_MAX}°C, ${TEMP_SAMPLES} samples)"
    PERFDATA="temperature=${TEMP}C;${TEMP_WARN};${TEMP_CRIT};0;100 temp_min=${TEMP_MIN}C temp_max=${TEMP_MAX}C"

    send_to_icinga "GPS Health - Temperature" "$STATUS" "$MESSAGE" "$PERFDATA"
fi

# ----------------------------------------------------------------------
# 4. DISK USAGE CHECK
# ----------------------------------------------------------------------
echo ""
echo "========================================"
echo "💾 4. DISK USAGE CHECK"
echo "========================================"

DISK=$(json_get '.aggregated.daily.disk_usage.mean')
DISK_MIN=$(json_get '.aggregated.daily.disk_usage.min')
DISK_MAX=$(json_get '.aggregated.daily.disk_usage.max')
DISK_SAMPLES=$(json_get '.aggregated.daily.disk_usage.samples')

if [[ -z "$DISK" ]]; then
    send_to_icinga "GPS Health - Disk Usage" 3 "⚠️ No disk data available for $DATE" ""
else
    if float_compare "$DISK" "gt" "$DISK_CRIT"; then
        STATUS=2; EMOJI="🔴"; TEXT="CRITICAL"
    elif float_compare "$DISK" "gt" "$DISK_WARN"; then
        STATUS=1; EMOJI="⚠️"; TEXT="WARNING"
    else
        STATUS=0; EMOJI="✅"; TEXT="OK"
    fi

    MESSAGE="${EMOJI} Disk Usage ${TEXT}: ${DISK}% (range: ${DISK_MIN}%-${DISK_MAX}%, ${DISK_SAMPLES} samples)"
    PERFDATA="disk=${DISK}%;${DISK_WARN};${DISK_CRIT};0;100 disk_min=${DISK_MIN}% disk_max=${DISK_MAX}%"

    send_to_icinga "GPS Health - Disk Usage" "$STATUS" "$MESSAGE" "$PERFDATA"
fi

# ----------------------------------------------------------------------
# 5. SATELLITES CHECK
# ----------------------------------------------------------------------
echo ""
echo "========================================"
echo "🛰️  5. SATELLITES CHECK"
echo "========================================"

SATS=$(json_get '.aggregated.daily.satellites.total.mean')
SATS_MIN=$(json_get '.aggregated.daily.satellites.total.min')
SATS_MAX=$(json_get '.aggregated.daily.satellites.total.max')
SATS_SAMPLES=$(json_get '.aggregated.daily.satellites.total.samples')

if [[ -z "$SATS" ]]; then
    send_to_icinga "GPS Health - Satellites" 3 "⚠️ No satellite data available for $DATE" ""
else
    if float_compare "$SATS" "lt" "$SATS_CRIT"; then
        STATUS=2; EMOJI="🔴"; TEXT="CRITICAL"
    elif float_compare "$SATS" "lt" "$SATS_WARN"; then
        STATUS=1; EMOJI="⚠️"; TEXT="WARNING"
    else
        STATUS=0; EMOJI="✅"; TEXT="OK"
    fi

    MESSAGE="${EMOJI} Satellites ${TEXT}: ${SATS} avg (range: ${SATS_MIN}-${SATS_MAX}, ${SATS_SAMPLES} samples)"
    PERFDATA="satellites=${SATS};${SATS_WARN};${SATS_CRIT};0;30 sats_min=${SATS_MIN} sats_max=${SATS_MAX}"

    send_to_icinga "GPS Health - Satellites" "$STATUS" "$MESSAGE" "$PERFDATA"
fi

# ----------------------------------------------------------------------
# 6. OVERALL HEALTH CHECK (Combined)
# ----------------------------------------------------------------------
echo ""
echo "========================================"
echo "🏥 6. OVERALL HEALTH CHECK"
echo "========================================"

# Count metrics by status
CRITICAL_COUNT=0
WARNING_COUNT=0
OK_COUNT=0
MISSING_COUNT=0

# Helper to track metric status
track_metric() {
    local value="$1"
    local warn_thresh="$2"
    local crit_thresh="$3"
    local compare_op="$4"  # "gt" or "lt"

    if [[ -z "$value" ]]; then
        MISSING_COUNT=$((MISSING_COUNT + 1))
        return
    fi

    if [[ "$compare_op" == "lt" ]]; then
        if float_compare "$value" "lt" "$crit_thresh"; then
            CRITICAL_COUNT=$((CRITICAL_COUNT + 1))
        elif float_compare "$value" "lt" "$warn_thresh"; then
            WARNING_COUNT=$((WARNING_COUNT + 1))
        else
            OK_COUNT=$((OK_COUNT + 1))
        fi
    else  # gt
        if float_compare "$value" "gt" "$crit_thresh"; then
            CRITICAL_COUNT=$((CRITICAL_COUNT + 1))
        elif float_compare "$value" "gt" "$warn_thresh"; then
            WARNING_COUNT=$((WARNING_COUNT + 1))
        else
            OK_COUNT=$((OK_COUNT + 1))
        fi
    fi
}

track_metric "$VOLTAGE" "$VOLTAGE_WARN" "$VOLTAGE_CRIT" "lt"
track_metric "$CPU" "$CPU_WARN" "$CPU_CRIT" "gt"
track_metric "$TEMP" "$TEMP_WARN" "$TEMP_CRIT" "gt"
track_metric "$DISK" "$DISK_WARN" "$DISK_CRIT" "gt"
track_metric "$SATS" "$SATS_WARN" "$SATS_CRIT" "lt"

# Determine overall status
if [[ $CRITICAL_COUNT -gt 0 ]]; then
    OVERALL_STATUS=2
    OVERALL_EMOJI="🔴"
    OVERALL_TEXT="CRITICAL"
elif [[ $WARNING_COUNT -gt 0 ]]; then
    OVERALL_STATUS=1
    OVERALL_EMOJI="⚠️"
    OVERALL_TEXT="WARNING"
elif [[ $MISSING_COUNT -eq 5 ]]; then
    OVERALL_STATUS=3
    OVERALL_EMOJI="❓"
    OVERALL_TEXT="UNKNOWN"
else
    OVERALL_STATUS=0
    OVERALL_EMOJI="✅"
    OVERALL_TEXT="OK"
fi

# Build compact status message
STATUS_PARTS=""
[[ -n "$VOLTAGE" ]] && STATUS_PARTS="${STATUS_PARTS}V:${VOLTAGE}V "
[[ -n "$CPU" ]] && STATUS_PARTS="${STATUS_PARTS}CPU:${CPU}% "
[[ -n "$TEMP" ]] && STATUS_PARTS="${STATUS_PARTS}T:${TEMP}°C "
[[ -n "$DISK" ]] && STATUS_PARTS="${STATUS_PARTS}D:${DISK}% "
[[ -n "$SATS" ]] && STATUS_PARTS="${STATUS_PARTS}S:${SATS}"

OVERALL_MESSAGE="${OVERALL_EMOJI} GPS Health ${OVERALL_TEXT}: ${OK_COUNT} OK, ${WARNING_COUNT} warning, ${CRITICAL_COUNT} critical | ${STATUS_PARTS}"
OVERALL_PERFDATA="ok_count=${OK_COUNT} warning_count=${WARNING_COUNT} critical_count=${CRITICAL_COUNT} missing_count=${MISSING_COUNT}"

send_to_icinga "GPS Health - Overall" "$OVERALL_STATUS" "$OVERALL_MESSAGE" "$OVERALL_PERFDATA"

# ======================================================================
# SUMMARY
# ======================================================================

echo ""
echo "========================================"
echo "📊 SUMMARY"
echo "========================================"
echo "Date: $DATE"
echo "Metrics Checked: 5"
echo "  ✅ OK: $OK_COUNT"
echo "  ⚠️  Warning: $WARNING_COUNT"
echo "  🔴 Critical: $CRITICAL_COUNT"
echo "  ❓ Missing: $MISSING_COUNT"
echo ""
echo "Overall Status: ${OVERALL_TEXT}"
echo "======================================"
echo "✅ All checks completed"
echo "======================================"
