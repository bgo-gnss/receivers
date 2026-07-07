# Log Quality Tracker

Development-driven register for reviewing log messages during debugging sessions.
When a log leads us to the root cause — keep it. When it misleads or obscures — fix it.

Audience: developers debugging issues, IT operations on production server (`rek-d01`).

---

## Log File Locations

| File | What it captures | Retention |
|------|-----------------|-----------|
| `~/.cache/gps_receivers/logs/receivers.log` | All `receivers.*` output (JSON, rotating 20 MB × 3) | ~weeks |
| `~/.cache/gps_receivers/logs/stations/{STATION}.log` | Per-station output only (JSON, daily rotation) | 90 days |
| `~/.cache/gps_receivers/logs/download_audit.jsonl` | Audit trail (JSON, rotating 50 MB × 5) | ~months |

### What goes to per-station files vs receivers.log only

Per-station files capture any logger whose **last name component matches the 4-char station ID pattern** (`[A-Z][A-Z0-9]{3}`):

| Logger name | Captured in station file | Example |
|-------------|--------------------------|---------|
| `receivers.download.{STA}` | ✅ yes | `receivers.download.GJFV` |
| `receivers.health.{STA}` | ✅ yes | `receivers.health.BJTV` |
| `receivers.trimble.netr9.{STA}` | ✅ yes | `receivers.trimble.netr9.AKUR` |
| `receivers.trimble.http_download_client.{STA}` | ✅ yes | any Trimble station |
| `receivers.septentrio.polarx5.{STA}` | ✅ yes | any PolaRX5 station |
| `receivers.pipeline.{STA}` | ✅ yes | |
| `receivers.scheduler` | ❌ no — receivers.log only | scheduler core |
| `receivers.scheduler.backfill` | ❌ no — receivers.log only | |
| `receivers.scheduler.reconciler` | ❌ no — receivers.log only | |
| `receivers.septentrio.push_config` | ❌ no — 4th component is not a station ID | |
| `receivers.config_utils` | ❌ no | |

**Rule of thumb**: anything that logs with `f"receivers.{module}.{station_id}"` ends up in the station file.

---

## Quick-Start jq Queries

Station logs are JSON, one object per line. Field names: `timestamp`, `level`, `logger`, `message`, `module`, `line`, `station_id`.

```bash
# Last 20 lines for a station
tail -20 ~/.cache/gps_receivers/logs/stations/GJFV.log | jq '.'

# Show only errors and warnings for a station today
jq 'select(.level == "ERROR" or .level == "WARNING")' \
  ~/.cache/gps_receivers/logs/stations/GJFV.log

# Search for a keyword across all station logs
grep -h "corrupt\|fail\|error" ~/.cache/gps_receivers/logs/stations/*.log | jq '.'

# Show all log lines for a station from receivers.log (scheduler messages too)
jq 'select(.station_id == "GJFV")' ~/.cache/gps_receivers/logs/receivers.log

# Timeline for a station: what happened in what order
jq '[.timestamp, .level, .message]' ~/.cache/gps_receivers/logs/stations/GJFV.log

# Count by level
jq -s 'group_by(.level) | map({level: .[0].level, count: length})' \
  ~/.cache/gps_receivers/logs/stations/GJFV.log

# Find all stations that had a warning or error in the last run
grep -l '"level": "WARNING"\|"level": "ERROR"' \
  ~/.cache/gps_receivers/logs/stations/*.log | xargs -I{} basename {} .log
```

On rek-d01 the scheduler writes to `~/.cache/gps_receivers/logs/` as `gpsops`. Read from `bgo` via group membership.

---

## Known Misleading Messages

Track messages that caused confusion during real debugging sessions.
Entries added when a message led us astray or was harder to interpret than it should be.

| # | Pattern | Level | Where | Verdict | Proposed fix | Status |
|---|---------|-------|-------|---------|--------------|--------|
| 1 | `Archived file failed validation: /mnt/data/.../STATION.T02.gz` | WARNING | download startup | **Misleading** — sounds like an archiving failure but actually means "existing archive is corrupt, will re-download" | `Corrupt archive ({size} bytes), will re-download: {path}` | **Fixed** — `tostools/src/tostools/utils/archive.py:166,176` |
| 2 | `⚠️  [BJTV] Archived file failed validation: ...` (console output) | WARNING | CLI stdout/stderr | The emoji warning looks like a download job outcome. A reader monitoring console output might think the download failed, not that it's about to re-download. | Now shows size + "will re-download" (from fix #1 above) | **Fixed** (same fix) |
| 3 | `❌ Failed: GSIG (15s_24hr) - File not found` | ERROR | scheduler download job | **Actionable but silent about category** — "not found" is ambiguous: is the file missing permanently or just not ready yet at midnight? Same error for a decommissioned path and a timing issue. | Added `[file_not_ready]` category tag: `❌ Failed: GSIG (15s_24hr) [file_not_ready] - File not found (8.4s)` | **Fixed** — `bulk_scheduler.py:_categorize_failure` |
| 4 | `📋 15s_24hr batch: 142 ✅  25 ❌  (167 total) — failed: ARHO, GAKE, ...` | INFO | batch summary | **Not actionable** — lists station names but no failure categories, so the operator can't distinguish "broken hardware" (needs field service) from "timing issue" (auto-retried). | Added category breakdown: `📋 15s_24hr batch: 142 ✅  25 ❌  (167 total) — conn_refused:12 file_not_ready:5 unreachable:8 — ARHO, GAKE, ...` | **Fixed** — `bulk_scheduler.py:_log_batch_summary_job` |
| 5 | `🔁 Second-chance 15s_24hr: 42 stations queued` (then silent for 2 hours) | INFO | second-chance retry start | **Dangerously silent** — 42 stations × sequential download = 1.5-2 hour run. An operator seeing this message would expect completion in minutes, not hours. The job also ran as a single thread on the `backfill` executor, which has a 1-slot pool on production. | Rewrote as parallel (8 workers). Now completes in ~10 min for 42 stations. Start log includes category breakdown; completion log shows `N/M recovered`. | **Fixed** — `bulk_scheduler.py:_retry_failed_daily_job` |
| 6 | `Downloading STATION.gz: 14%\|█▍        \| 826k/5.80M [00:13<01:33...]` | (stdout/stderr) | log file / journal | **Log pollution** — tqdm progress bars use carriage returns that corrupt structured log files and journald output. Only useful on interactive TTYs. | Added `disable=not sys.stderr.isatty()` to all tqdm calls. Progress bars now only render when a human is watching; suppressed in scheduled/background runs. | **Fixed** — `polarx5.py`, `download_manager.py` |
| 7 | `❌ Failed: GSIG (15s_24hr) [file_not_ready] - All file downloads failed` | ERROR | second-chance retry | **Category gives false hope** — `file_not_ready` implies the file will be available later (a timing issue). But some stations fail with 404 both at midnight AND hours later; they're genuinely broken. The category is correct for midnight 404s but misleading on the second attempt. | No code fix yet. Future: check if the same station also failed yesterday with the same category — if so, escalate to `file_missing_persistent` category. | **Open** |
| 8 | `❌ Failed: GAKE (15s_24hr) [file_not_ready] - HTTP port 8060 not responding` | ERROR | midnight + second-chance | **False negative** — appeared in both the midnight run and the 17:04 second-chance retry, yet the user was able to download GAKE manually shortly after. Confirmed: `curl rek-d01 → GAKE:8060` returns HTTP 200 (TRMB/1.2) currently. Root cause: transient HTTP service outage on the Trimble receiver, happened to coincide with both our attempt windows. No code fix — this is the class of failure the second-chance retry exists for. The 2-attempt model accepts one such miss per day. A third retry window (e.g. 04:00) would catch it but adds complexity. | Log as-is; document in this file. | **Accepted / no fix** |
| 9 | `❌ [job] epos-disseminate RHOF 2026-07-05: run failed` (no traceback anywhere) | ERROR | EPOS dissemination sweep (`logger.exception`) | **Traceback silently swallowed** — the custom `ProductionFormatter` and `JSONFormatter` both override `format()` and only emit `record.getMessage()`, dropping `exc_info`. So `logger.exception(...)` wrote **no traceback** to either the console/journal or `receivers.log`. The first automated EPOS 08:30 sweep (2026-07-07) failed 6/6 with only this bare line; the cause could not be read from the logs and had to be reproduced by hand. | Both formatters now append `formatException(record.exc_info)` (JSON as an `exc_info` field, console/journal as a trailing block) — mirrors the stdlib `logging.Formatter` behaviour they had overridden away. | **Fixed** — `base/production_logging.py:ProductionFormatter.format`, `JSONFormatter.format`; regression test `tests/test_logging_config.py::TestFormatterExceptionInfo` |

### Notes on entry #1 (Archived file failed validation)

Discovered during 2026-04-30 investigation of BJTV/GJFV/KVIS/RJUC/SIFJ showing `status=2` after the midnight run.

The message appears during download startup when `ArchiveValidator` checks the existing archive. The actual situation is:
- Corrupt 64 KB truncated archive exists at path
- The system is about to delete it and re-download (correct behavior)
- But the log message alone, without the surrounding context, looks like something went wrong *during* this run

**Root cause of the corrupt archives (separate from the logging issue)**: `tostools.utils.archive.ArchiveValidator._validate_tmp_file_integrity()` passes truncated gzip files that are exactly one TCP buffer (65,536 bytes). See `memory/project_tmp_flush_bug.md`.

---

## How to Use This Document

When debugging production issues:
1. Find the log messages that told you where to look
2. Find the messages you had to discard as noise or that pointed in the wrong direction
3. Add an entry to the table above

When fixing a message:
1. Change the log call in code, commit with `fix(logging): <what was misleading>`
2. Update the Status column to `Fixed in <commit>`

The goal is slow, steady improvement: each real incident leaves the logging one step better for the next one.

---

*Started: 2026-04-30*
