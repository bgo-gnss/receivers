#!/bin/bash
# =============================================================================
# Morning Recovery Report
# =============================================================================
# Reads receivers.log for the 01:25-01:50 UTC window of a given UTC date
# (default: today) and writes a plaintext summary to
# /home/gpsops/morning-recovery-reports/YYYY-MM-DD.txt
#
# Designed to run from a systemd timer at ~02:00 UTC daily, after the 01:30
# morning_recovery scheduler job completes. No mail dependency — the operator
# cat-s the file the next morning.
#
# Usage:
#   morning_recovery_report.sh             # today's date (UTC)
#   morning_recovery_report.sh 2026-05-12  # explicit date
# =============================================================================

set -euo pipefail

CACHE_DIR="${GPS_CACHE_DIR:-/home/gpsops/.cache/gps_receivers}"
LOG_DIR="$CACHE_DIR/logs"
LOG_FILE="$LOG_DIR/receivers.log"
LOG_FILE_PREV="$LOG_DIR/receivers.log.1"
REPORT_DIR="${MORNING_RECOVERY_REPORT_DIR:-/home/gpsops/morning-recovery-reports}"

# Time window — the morning_recovery job fires at 01:30 UTC. Capture a few
# minutes before and after to include the filter-summary log lines and the
# completion summary.
WINDOW_START="01:25:00"
WINDOW_END="01:50:00"

TARGET_DATE="${1:-$(date -u +%Y-%m-%d)}"

# Validate date format. `date -d` accepts almost anything, but we want to fail
# loudly on typos rather than silently report on the wrong day.
if ! date -u -d "$TARGET_DATE" >/dev/null 2>&1; then
    echo "ERROR: invalid date: $TARGET_DATE" >&2
    exit 1
fi
TARGET_DATE=$(date -u -d "$TARGET_DATE" +%Y-%m-%d)

mkdir -p "$REPORT_DIR"
REPORT_FILE="$REPORT_DIR/${TARGET_DATE}.txt"

WINDOW_FROM="${TARGET_DATE}T${WINDOW_START}"
WINDOW_TO="${TARGET_DATE}T${WINDOW_END}"

# Concatenate current + previous rotated log. jq filters by timestamp string
# (lexicographic compare works for ISO-8601). If the relevant log has rotated
# more than once during the window, the operator can rerun with --date once
# they identify the right rotation; for the common case (script runs ~10 min
# after the window closes) one rotation back is plenty of headroom.
LOG_SOURCES=()
[ -f "$LOG_FILE_PREV" ] && LOG_SOURCES+=("$LOG_FILE_PREV")
[ -f "$LOG_FILE" ] && LOG_SOURCES+=("$LOG_FILE")

if [ ${#LOG_SOURCES[@]} -eq 0 ]; then
    {
        echo "Morning recovery report — $TARGET_DATE"
        echo "Generated: $(date -u --iso-8601=seconds)"
        echo
        echo "ERROR: no receivers.log found at $LOG_FILE or $LOG_FILE_PREV"
    } > "$REPORT_FILE"
    exit 0
fi

# Filter to JSON entries from morning_recovery logger inside the window.
# Use cat | jq rather than jq -s (which loads the whole file into memory).
WINDOW_JSON=$(cat "${LOG_SOURCES[@]}" 2>/dev/null \
    | jq -c --arg from "$WINDOW_FROM" --arg to "$WINDOW_TO" \
        'select(.timestamp >= $from and .timestamp <= $to)
         | select(.logger | startswith("receivers.scheduler.morning_recovery"))' \
    2>/dev/null || true)

# Count by level (errors/warnings outside the morning_recovery logger but
# inside the same window are also useful — they often surface the *cause* of
# a recovery failure).
WINDOW_ERRORS=$(cat "${LOG_SOURCES[@]}" 2>/dev/null \
    | jq -c --arg from "$WINDOW_FROM" --arg to "$WINDOW_TO" \
        'select(.timestamp >= $from and .timestamp <= $to)
         | select(.level == "ERROR" or .level == "WARNING")' \
    2>/dev/null || true)

# === Build report ============================================================
#
# Report generation is best-effort log-scraping. A `head`-truncated pipe over a
# large error volume makes the upstream `jq`/`printf` die with SIGPIPE, which
# `pipefail` + `errexit` would otherwise turn into a unit failure *after* the
# report was already written (observed: exit 2 / perpetual "failed" state on a
# high-error morning). Relax errexit + pipefail for the block — the written file
# is the deliverable — then restore them for the chmod/verify tail below.
set +e +o pipefail
{
    echo "============================================================"
    echo "Morning recovery report — $TARGET_DATE (window 01:25-01:50 UTC)"
    echo "Generated: $(date -u --iso-8601=seconds)"
    echo "Log sources: ${LOG_SOURCES[*]}"
    echo "============================================================"
    echo

    # --- Job fire confirmation ----------------------------------------------
    JOB_LINES=$(printf '%s\n' "$WINDOW_JSON" | grep -c '^{' || true)
    if [ "$JOB_LINES" -eq 0 ]; then
        echo "❌ NO morning_recovery log entries in window."
        echo "   Possible causes:"
        echo "     - scheduler not running"
        echo "     - morning_recovery disabled in scheduler.yaml"
        echo "     - logger name mismatch (expected: receivers.scheduler.morning_recovery)"
        echo "     - log rotation moved the records — check receivers.log.* manually"
        echo
    else
        echo "✓ $JOB_LINES log entries from morning_recovery logger in window."
        FIRST_TS=$(printf '%s\n' "$WINDOW_JSON" | head -1 | jq -r '.timestamp // "?"')
        LAST_TS=$(printf '%s\n' "$WINDOW_JSON" | tail -1 | jq -r '.timestamp // "?"')
        echo "  First: $FIRST_TS"
        echo "  Last : $LAST_TS"
        echo
    fi

    # --- Filter summary (queued / passive / already_ok / etc.) --------------
    echo "── Filter summary (per session/date) ──────────────────────"
    SUMMARY_LINES=$(printf '%s\n' "$WINDOW_JSON" \
        | jq -r 'select(.message | test("filter summary:")) | .message' || true)
    if [ -z "$SUMMARY_LINES" ]; then
        echo "  (none)"
    else
        echo "$SUMMARY_LINES" | sed 's/^/  /'
    fi
    echo

    # --- Stations queued ----------------------------------------------------
    echo "── Stations queued ────────────────────────────────────────"
    QUEUED_LINES=$(printf '%s\n' "$WINDOW_JSON" \
        | jq -r 'select(.message | test("stations queued")) | .message' || true)
    if [ -z "$QUEUED_LINES" ]; then
        echo "  (none — recovery had nothing to retry)"
    else
        echo "$QUEUED_LINES" | sed 's/^/  /'
    fi
    echo

    # --- Recovery outcomes --------------------------------------------------
    echo "── Recovery outcomes ──────────────────────────────────────"
    COMPLETE_LINES=$(printf '%s\n' "$WINDOW_JSON" \
        | jq -r 'select(.message | test("complete:")) | .message' || true)
    if [ -z "$COMPLETE_LINES" ]; then
        echo "  (none — no completion log found; job may still be running or crashed)"
    else
        echo "$COMPLETE_LINES" | sed 's/^/  /'
    fi
    echo

    # --- Deferred dates (deadline guard) ------------------------------------
    DEFERRED=$(printf '%s\n' "$WINDOW_JSON" \
        | jq -r 'select(.message | test("deadline guard|deferring")) | .message' || true)
    if [ -n "$DEFERRED" ]; then
        echo "── ⚠ Deferred (deadline guard) ────────────────────────────"
        echo "$DEFERRED" | sed 's/^/  /'
        echo
    fi

    # --- Warnings / errors in window (all loggers) --------------------------
    echo "── Warnings/errors in window (any logger) ─────────────────"
    WERR_COUNT=$(printf '%s\n' "$WINDOW_ERRORS" | grep -c '^{' || true)
    if [ "$WERR_COUNT" -eq 0 ]; then
        echo "  (none)"
    else
        echo "  Count: $WERR_COUNT"
        echo "  First 20 by logger:"
        printf '%s\n' "$WINDOW_ERRORS" \
            | jq -r '"    [\(.level)] \(.logger): \(.message)"' \
            | head -20
    fi
    echo

    # --- Per-station skip-reason callouts (from queue diagnostics) ----------
    SKIP_HINTS=$(printf '%s\n' "$WINDOW_JSON" \
        | jq -r 'select(.message | test("↪ marked_missing|↪ not_targeted")) | .message' || true)
    if [ -n "$SKIP_HINTS" ]; then
        echo "── Skip-reason callouts ───────────────────────────────────"
        echo "$SKIP_HINTS" | sed 's/^/  /'
        echo
    fi

    echo "============================================================"
    echo "Raw morning_recovery entries: $JOB_LINES"
    echo "Window warnings/errors      : $WERR_COUNT"
    echo "============================================================"
} > "$REPORT_FILE"
set -e -o pipefail

chmod 644 "$REPORT_FILE"

# Print path to stdout so journal capture is useful too
echo "Report written: $REPORT_FILE"
exit 0
