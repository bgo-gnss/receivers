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
