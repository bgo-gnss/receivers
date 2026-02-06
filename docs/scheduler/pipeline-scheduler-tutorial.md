# Pipeline Scheduler Tutorial

This tutorial guides you through running the enhanced GPS receiver scheduler with pipeline orchestration, resource pools, and priority-based task scheduling.

> **Note**: SyncTask (rsync to permanent storage) is documented separately and not covered in this tutorial.

## Overview

The enhanced scheduler extends the bulk download system with:

- **Task Pipelines**: Download → RINEX sequences with dependency tracking
- **Resource Pools**: Separate pools for I/O-bound (network) and CPU-bound (RINEX) operations
- **Priority System**: REALTIME (1), STANDARD (5), BACKFILL (8) task ordering
- **Crash Recovery**: Resume incomplete pipelines after restart

### Pipeline Stages

| Stage | Pool | Purpose |
|-------|------|---------|
| DOWNLOAD | Network | Fetch raw data from receivers |
| RINEX | CPU | Convert raw files to RINEX format |
| HEALTH | Network | Extract health metrics from status files |

Stage dependencies:
- RINEX depends on DOWNLOAD (needs raw files)
- HEALTH depends on DOWNLOAD (needs status files)

---

## 1. Prerequisites

### Install Dependencies

```bash
cd ~/work/projects/gps/gpslibrary_new/receivers

# Install the package in development mode
pip install -e .

# Required dependencies
pip install apscheduler sqlalchemy pyyaml
```

### Set Environment

```bash
# Set PYTHONPATH for development
export PYTHONPATH=../gtimes/src:../gps_parser/src:src

# Optional: Set custom config directory
export GPS_CONFIG_PATH=~/.config/gpsconfig
```

---

## 2. Configuration

### Configuration File Location

The scheduler looks for configuration at:
1. `$GPS_CONFIG_PATH/scheduler.yaml` (if GPS_CONFIG_PATH is set)
2. `~/.config/gpsconfig/scheduler.yaml` (default)

### View Current Configuration

```bash
receivers scheduler config --show
```

### Create Default Configuration

If you don't have a configuration file:

```bash
receivers scheduler config --create
```

### Key Configuration Sections

Your `scheduler.yaml` should include these sections:

```yaml
# ==============================================================================
# SCHEDULER SETTINGS
# ==============================================================================
scheduler:
  max_workers: 5           # Maximum concurrent operations
  log_dir: ~/.cache/gps_receivers/logs
  log_level: INFO          # DEBUG for troubleshooting

# ==============================================================================
# RESOURCE POOLS (NEW)
# ==============================================================================
resource_pools:
  network_workers: 10      # I/O-bound: downloads, status checks
  cpu_workers: 4           # CPU-bound: RINEX conversion (memory-limited)

# ==============================================================================
# PIPELINE DEFINITIONS (NEW)
# ==============================================================================
pipelines:
  15s_24hr:
    stages: [download, rinex]     # Download then convert to RINEX
    priority: standard            # STANDARD (5) priority
    rinex_timing: immediate       # Convert right after download

  1Hz_1hr:
    stages: [download]            # Download only (no RINEX for high-rate)
    priority: realtime            # REALTIME (1) priority

  status_1hr:
    stages: [download, health]    # Download then extract health
    priority: standard
    health_targets: [database]    # Send health to PostgreSQL
    health_priority: backfill     # Health extraction is background task

# ==============================================================================
# SESSION SCHEDULES
# ==============================================================================
sessions:
  15s_24hr:
    enabled: true
    schedule: "00:10"             # Daily at 00:10
    distribution_window: 10       # Spread 173 stations across 10 minutes
    max_concurrent: 3
    timeout_minutes: 45

  1Hz_1hr:
    enabled: true
    schedule: ":15"               # Hourly at :15
    distribution_window: 10
    max_concurrent: 4
    timeout_minutes: 30

  status_1hr:
    enabled: true
    schedule: ":25"               # Hourly at :25
    distribution_window: 5
    max_concurrent: 5
    timeout_minutes: 15

# ==============================================================================
# REAL-TIME STATUS MONITORING (NEW)
# ==============================================================================
status_monitoring:
  enabled: true
  schedule: "*/15 * * * *"        # Every 15 minutes (cron syntax)
  priority: realtime              # Never blocked by backfill
  targets: [database, icinga]     # Equivalent to: receivers health --icinga --save-db

# ==============================================================================
# PRIORITY CONFIGURATION (NEW)
# ==============================================================================
priorities:
  realtime:
    level: 1
    sessions: [1Hz_1hr]           # High-rate data is time-critical
  standard:
    level: 5
    sessions: [15s_24hr, status_1hr]
  backfill:
    level: 8
    max_concurrent: 2             # Limit backfill workers
```

---

## 3. Testing the Setup

Before starting the scheduler in production, always test first.

### Basic Test

```bash
receivers scheduler test
```

Output:
```
🧪 Testing scheduler setup...
✅ Loaded 173 station configurations
✅ Successfully scheduled 519 jobs

📊 Job distribution:
  15s_24hr: 173 stations (daily at 10:XX)
           Stations: AKUR, ARHO, BALD, BERG, BIRN +168 more
  1Hz_1hr: 173 stations (hourly at 15:XX)
           Stations: AKUR, ARHO, BALD, BERG, BIRN +168 more
  status_1hr: 103 stations (hourly at 25:XX)
           Stations: AKUR, ARHO, BALD, BERG, BIRN +98 more

⏰ Next few scheduled runs:
  AKUR (15s_24hr): 2026-02-07 00:10:00
  ARHO (15s_24hr): 2026-02-07 00:10:03
  BALD (15s_24hr): 2026-02-07 00:10:06

✅ Scheduler test completed successfully
   Use 'receivers scheduler start' to run the scheduler
```

### Test with Specific Stations

For development/testing, limit to a few stations:

```bash
# Test with only 3 stations
receivers scheduler test --stations ELDC THOB OLKE

# Test with max 2 stations per session
receivers scheduler test --max-stations 2
```

### Verify Pipeline Configuration

Check that pipelines are configured correctly:

```bash
# Show full configuration
receivers scheduler config --show | grep -A 10 pipelines
```

---

## 4. Starting the Scheduler

### Development Mode (Verbose)

```bash
# Start with verbose output, limited stations
receivers scheduler start --stations ELDC THOB OLKE --verbose --show-jobs
```

### Production Mode

```bash
# Start full scheduler with 5 workers
receivers scheduler start --max-workers 5

# Or with limited stations for gradual rollout
receivers scheduler start --max-stations 20 --max-workers 3
```

### What Happens at Startup

1. **Resource pools initialized**: Network and CPU pools created
2. **Crash recovery**: Any incomplete pipelines from previous run are resumed
3. **Jobs scheduled**: All station/session combinations scheduled
4. **Scheduler starts**: APScheduler begins executing jobs at scheduled times

Example output:
```
✅ Scheduled 519 download jobs
🚀 Starting scheduler with 5 workers...
   Press Ctrl+C to stop

Scheduled jobs:
  15s_24hr_AKUR: 2026-02-07 00:10:00
  15s_24hr_ARHO: 2026-02-07 00:10:03
  ...
```

---

## 5. Understanding Pipeline Execution

### Download → RINEX Pipeline (15s_24hr)

When a 15s_24hr job runs:

```
┌─────────────────────────────────────────────────────────────┐
│ Pipeline: 15s_24hr for station ELDC                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  [1] DOWNLOAD Stage (Network Pool)                          │
│      └── FTP download from receiver                         │
│      └── Files: ELDC0370.26d.gz                             │
│      └── Archived immediately after download                │
│                                                             │
│           │                                                 │
│           ▼                                                 │
│                                                             │
│  [2] RINEX Stage (CPU Pool)                                 │
│      └── Depends on: DOWNLOAD completed                     │
│      └── Convert raw SBF → RINEX 3                          │
│      └── Apply Hatanaka compression                         │
│      └── Files: ELDC00ISL_R_20260370000_01D_15S_MO.crx.gz   │
│                                                             │
│           │                                                 │
│           ▼                                                 │
│                                                             │
│  [✓] Pipeline Complete                                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Status → Health Pipeline (status_1hr)

When a status_1hr job runs:

```
┌─────────────────────────────────────────────────────────────┐
│ Pipeline: status_1hr for station ELDC                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  [1] DOWNLOAD Stage (Network Pool)                          │
│      └── FTP download status file                           │
│      └── Files: ELDC037k.26_.gz                             │
│                                                             │
│           │                                                 │
│           ▼                                                 │
│                                                             │
│  [2] HEALTH Stage (Network Pool, BACKFILL priority)         │
│      └── Depends on: DOWNLOAD completed                     │
│      └── Extract health metrics from SBF                    │
│      └── Write to PostgreSQL gps_health database            │
│                                                             │
│           │                                                 │
│           ▼                                                 │
│                                                             │
│  [✓] Pipeline Complete                                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 6. Real-Time Status Monitoring

The StatusTask runs every 15 minutes for **live** receiver checks (separate from the status_1hr file downloads).

### What StatusTask Does

```bash
# Equivalent to running manually:
receivers health STATION --icinga --save-db
```

1. **Connect to receiver** - Live TCP/FTP connection
2. **Get current status** - Battery, temperature, tracking, etc.
3. **Write to PostgreSQL** - `gps_health` database
4. **Send to Icinga** - Passive check results for alerting

### Configuration

```yaml
status_monitoring:
  enabled: true
  schedule: "*/15 * * * *"     # Every 15 minutes
  priority: realtime           # Never blocked by backfill
  targets: [database, icinga]
```

### StatusTask vs HealthTask

| Task | Runs | Priority | Data Source | Purpose |
|------|------|----------|-------------|---------|
| **StatusTask** | Every 15 min | REALTIME | Live receiver connection | Real-time monitoring, Icinga alerts |
| **HealthTask** | After status_1hr download | BACKFILL | Downloaded status files | Historical health data extraction |

---

## 7. Priority System

Tasks are executed based on priority level (lower = higher priority):

| Priority | Level | Use Case |
|----------|-------|----------|
| REALTIME | 1 | 1Hz hourly data, live status checks |
| STANDARD | 5 | 15s daily data, status_1hr downloads |
| BACKFILL | 8 | Health extraction, recovery operations |
| MAINTENANCE | 10 | Low priority background tasks |

### Priority in Action

When resources are limited:
1. **REALTIME tasks always run first** - 1Hz data downloads, live status
2. **STANDARD tasks run next** - Daily downloads, scheduled operations
3. **BACKFILL tasks wait** - Only run when higher-priority work is done

This ensures time-critical data (1Hz for real-time applications) is never delayed by background work.

---

## 8. Monitoring the Scheduler

### Check Status

```bash
receivers scheduler status --show-jobs
```

Output:
```
📊 Scheduler Status
==================================================
Running: True
Total jobs: 519
Active downloads: 3
Current jobs: 15s_24hr_ELDC, 15s_24hr_THOB, 15s_24hr_OLKE

📅 Scheduled Jobs (519)
--------------------------------------------------

15s_24hr (173 stations):
  AKUR: 00:10:00
  ARHO: 00:10:03
  BALD: 00:10:06
  BERG: 00:10:09
  BIRN: 00:10:12
  ... and 168 more
```

### Log Files

```bash
# Main scheduler log
tail -f ~/.cache/gps_receivers/logs/scheduler.log

# Download audit trail (JSON)
tail -f ~/.cache/gps_receivers/logs/download_audit.jsonl
```

### Pipeline State Database

Pipeline state is persisted in SQLite for crash recovery:

```bash
# Location
~/.cache/gps_receivers/logs/pipeline.db

# Inspect with sqlite3
sqlite3 ~/.cache/gps_receivers/logs/pipeline.db ".tables"
sqlite3 ~/.cache/gps_receivers/logs/pipeline.db "SELECT * FROM pipeline_jobs WHERE completed = 0;"
```

---

## 9. Stopping and Restarting

### Graceful Stop

```bash
# Wait for active downloads to complete
receivers scheduler stop
```

### Force Stop

```bash
# Immediate shutdown (may interrupt downloads)
receivers scheduler stop --force
```

### Restart

```bash
# Stop and start with same configuration
receivers scheduler restart

# Restart with different settings
receivers scheduler restart --max-workers 3 --verbose
```

---

## 10. Troubleshooting

### Common Issues

**APScheduler not found:**
```bash
pip install apscheduler sqlalchemy
```

**YAML not available:**
```bash
pip install pyyaml
```

**No stations scheduled:**
- Check `GPS_CONFIG_PATH` is set correctly
- Verify `stations.cfg` exists in config directory
- Run `receivers scheduler test --verbose` for details

**Pipeline stuck:**
```bash
# Check pipeline database for incomplete jobs
sqlite3 ~/.cache/gps_receivers/logs/pipeline.db \
  "SELECT job_id, station_id, session_type FROM pipeline_jobs WHERE completed = 0;"

# Restart scheduler - incomplete pipelines auto-resume
receivers scheduler restart
```

**Resource pool exhaustion:**
- Reduce `cpu_workers` in config (RINEX is memory-intensive)
- Reduce `max_concurrent` per session
- Default CPU workers: `min(4, cpu_count - 2)`

### Debug Mode

```bash
# Set log level to DEBUG in scheduler.yaml
scheduler:
  log_level: DEBUG

# Or start with verbose flag
receivers scheduler start --verbose
```

---

## 11. Quick Reference

### CLI Commands

| Command | Purpose |
|---------|---------|
| `receivers scheduler test` | Verify setup without starting |
| `receivers scheduler start` | Start the scheduler |
| `receivers scheduler status` | Check running status |
| `receivers scheduler stop` | Graceful shutdown |
| `receivers scheduler restart` | Stop and start |
| `receivers scheduler config --show` | View configuration |
| `receivers scheduler config --create` | Create default config |

### Testing Flags

| Flag | Purpose |
|------|---------|
| `--stations ELDC THOB` | Only schedule specific stations |
| `--max-stations 5` | Limit stations per session |
| `--max-workers 2` | Limit concurrent operations |
| `--verbose` | Enable debug logging |
| `--show-jobs` | Display scheduled jobs |

### Configuration Files

| File | Purpose |
|------|---------|
| `scheduler.yaml` | Main scheduler configuration |
| `scheduler.db` | Job persistence (SQLite) |
| `pipeline.db` | Pipeline state (SQLite) |
| `scheduler.log` | Scheduler logs |
| `download_audit.jsonl` | Download audit trail |

---

## Next Steps

Once comfortable with the basic scheduler:

1. **Enable all sessions** - Set `enabled: true` for all session types
2. **Configure station overrides** - Custom settings for problem stations
3. **Monitor with Grafana** - See `docs/grafana/` for dashboard setup
4. **Set up Icinga alerts** - Configure `icinga.cfg` for alerting

For production deployment, consider:
- Running scheduler as a systemd service
- Setting up log rotation
- Configuring backup for pipeline.db
