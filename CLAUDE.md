# CLAUDE.md - GPS Receivers Package

This document provides guidance for working with the `receivers` package in the gpslibrary_new collection.

## Package Overview

The `receivers` package provides GPS receiver management functionality for the Icelandic Met Office's 173-station GNSS network. It includes direct receiver communication, bulk download scheduling, production logging, and comprehensive validation.

## Key Features

### Core Functionality
- **Direct receiver communication** - FTP/TCP connections to GPS receivers
- **Bulk download management** - APScheduler-based distributed downloading
- **Production logging** - Structured output for automated systems
- **Immediate archiving** - Fault-tolerant file handling
- **Comprehensive validation** - Receiver type detection and configuration validation

### Supported Receivers
- **Septentrio PolaRX5** - Primary receiver type with full feature support
- **Leica/Trimble receivers** - Basic download support
- **Generic receivers** - Configurable via type detection system

## Command-Line Interface

### Basic Commands

```bash
# Download data with sync and archiving
receivers download ELDC THOB --sync --archive

# Download specific time period
receivers download ELDC --start 20250905 --end 20250906 --session 1Hz_1hr

# Check receiver status
receivers status ELDC THOB

# Get health information
receivers health THOB --verbose

# Validate receiver configuration
receivers validate ELDC --verbose
```

### Production Mode

```bash
# Production logging with JSON output
receivers download ELDC --sync --archive --production --json-log

# Test connection before downloading
receivers download ELDC --test-connection --sync

# Phase 1 utilities are always enabled (default)
receivers download ELDC --sync --archive
```

### Bulk Scheduler

```bash
# Test scheduler configuration
receivers scheduler test

# Test with subset of stations (laptop testing)
receivers scheduler test --stations OLKE ELDC THOB --max-stations 2

# Start scheduler with limited stations
receivers scheduler start --stations OLKE ELDC --max-workers 2 --verbose

# Check scheduler status
receivers scheduler status --show-jobs

# Create/manage configuration
receivers scheduler config --create
receivers scheduler config --show
```

## Architecture

### Phase 1 Utilities (Always Enabled)
**Status**: ✅ Default in all receiver types (PolaRX5, NetR9, NetRS, G10) - Phase 3B complete

The receivers package uses modular Phase 1 utilities for core functionality:

#### Time Parameter Processor
- **Purpose**: Parse and validate session parameters (start, end, session type)
- **Usage**: Converts user input to datetime lists for file generation
- **Benefits**: Single source of truth for time processing, comprehensive validation

#### Archive Validator
- **Purpose**: Validate archive file integrity (gzip, size, corruption)
- **Usage**: Check files before/after archiving operations
- **Benefits**: Detect corrupt archives, validate downloads

#### File Archiver (IMMEDIATE Mode)
- **Purpose**: Archive files immediately after download/processing
- **Mode**: IMMEDIATE - archive one file at a time for fault tolerance
- **Benefits**: Prevents data loss on crashes, incremental progress tracking

**Phase 1 utilities are always enabled** (no configuration needed):
```bash
receivers download STATION --sync --archive  # Phase 1 is always active
```

**Why immediate archiving?**
- **Fault tolerance**: Already-downloaded files are safely archived if process crashes
- **Slow connections**: Progress saved incrementally during long downloads
- **Production reliability**: Minimizes data loss during network issues
- **Better monitoring**: Clear file-by-file progress tracking

### Scheduling System
- **Time distribution**: Downloads spread across 10-minute windows to avoid network congestion
- **Session types**:
  - `15s_24hr`: Daily downloads at 00:10-00:19
  - `1Hz_1hr`: Hourly downloads at XX:15-XX:24
  - `status_1hr`: Hourly status at XX:25-XX:29
- **Persistence**: SQLite job store survives restarts
- **Manual compatibility**: All manual operations remain fully functional
- **Extensibility**: Task interface allows scheduling any operation type (status, health, validation)
- **Testing**: 43+ comprehensive test cases (Phase 3C)

#### Scheduler Extensibility (Phase 3C)

**Task Interface Architecture**:
```python
from receivers.scheduling.task_interface import ScheduledTask, TaskType
from receivers.scheduling.tasks import DownloadTask

# Current: DownloadTask implements ScheduledTask
# Future: StatusTask, HealthTask, ValidateTask
```

**Adding New Task Types** (Future):
```python
class StatusTask(ScheduledTask):
    def execute(self) -> TaskResult:
        # Check receiver status
        ...
```

See `docs/scheduler/scheduler-guide.md` for complete details.

### Path Building System
- **Unified approach**: Single `build_path()` method handles all path generation using gtimes templates
- **Dynamic frequency**: Respects session frequency (1H for hourly, 1D for daily) instead of hardcoded values
- **Consistent formatting**: Both remote and archive paths use same gtimes-based datetime formatting
- **Separation of concerns**: Path generation completely separated from download mechanics
- **Year-future-proof**: Automatic year handling prevents hardcoding bugs (e.g., `.25_` format)
- **Multiple input types**: Supports single datetime, datetime lists, or start/end time ranges
- **IGS filename accuracy**: Uses gtimes `#Rin2` format for correct hour-to-letter mapping

### Production Logging
- **Concise output**: Timestamp, level icon, station, message format
- **JSON mode**: Structured logs for monitoring system integration
- **Audit trail**: Separate download statistics and performance metrics
- **Log rotation**: Automatic rotation with size limits

### File Management
- **Immediate archiving**: Files archived after each download for fault tolerance
- **Compression**: Automatic .gz compression
- **Sync strategy**: Only download new/partial files
- **Clean restart**: Option to clear partial downloads

## Configuration

### Station Configuration
```bash
# Configuration loaded from gps_parser package
# Uses ~/.config/gpsconfig/stations.cfg and postprocess.cfg
# Environment: GPS_CONFIG_PATH or default paths
```

#### Station Lifecycle Fields

Two separate fields in `stations.cfg` control station visibility:

| Field | Purpose | Values | Default (NULL) |
|-------|---------|--------|----------------|
| `station_status` | Station lifecycle | `inactive`, `discontinued` | active |
| `health_check` | Monitoring mode | `passive` | active (directly checked) |

- **`station_status`**: Controls whether the station is operational
  - `inactive` — no receiver installed or temporarily out of service
  - `discontinued` — station decommissioned, no longer operational
  - Not set (NULL) — active, fully operational
- **`health_check`**: Controls how the station is monitored
  - `passive` — data arrives externally, not directly health-checked
  - Not set (NULL) — active, scheduler runs health checks

A station can have both fields set (e.g., GRVM: `station_status = inactive` + `health_check = passive`).

**Config change detection**: The scheduler watches `stations.cfg` for changes (mtime-based, every 5 minutes) and automatically syncs `station_status` and `health_check` to the PostgreSQL database. No scheduler restart needed for config changes.

**Auto-detection**: Stations with `receiver_type` set to None/empty/unknown are automatically flagged as `station_status = inactive` by the scheduler.

See `stations.cfg` header comments for the complete field reference.

### Scheduler Configuration
```bash
# Create default configuration
receivers scheduler config --create

# Configuration location (respects GPS_CONFIG_PATH environment variable):
# - If GPS_CONFIG_PATH is set: $GPS_CONFIG_PATH/scheduler.yaml
# - Otherwise: ~/.config/gpsconfig/scheduler.yaml
# Database: ~/.cache/gps_receivers/scheduler.db
# Logs: ~/.cache/gps_receivers/logs/
```

#### Flexible Schedule Syntax

The scheduler now supports flexible schedule formats in addition to the legacy `schedule_minute` + `frequency` format:

**Supported formats:**
1. **Single time (daily)**: `schedule: "00:10"` - Runs daily at 00:10
2. **Hourly at minute**: `schedule: ":15"` - Runs every hour at :15
3. **Interval (hours)**: `schedule: "6h"` - Runs every 6 hours
4. **Interval (minutes)**: `schedule: "45m"` - Runs every 45 minutes
5. **Multiple times**: `schedule: ["06:00", "14:00", "22:00"]` - Runs 3 times daily
6. **Raw cron**: `schedule: "cron: */15 * * * *"` - Full cron expression support

**Examples:**
```yaml
sessions:
  15s_24hr:
    schedule: "00:10"              # Daily at 00:10
    distribution_window: 10

  1Hz_1hr:
    schedule: ":15"                # Hourly at :15
    distribution_window: 10

  custom_6h:
    schedule: "6h"                 # Every 6 hours
    distribution_window: 10

  rush_hour:
    schedule: ["06:00", "12:00", "18:00"]  # Three times daily
    distribution_window: 5

  business_hours:
    schedule: "cron: 0 8-17 * * 1-5"  # Every hour, 8am-5pm, Mon-Fri
    distribution_window: 5
```

**Legacy format (still supported):**
```yaml
sessions:
  15s_24hr:
    schedule_minute: 10
    frequency: daily
    distribution_window: 10
```

**Note**: The `distribution_window` spreads stations evenly across time to avoid network congestion. For example, with 3 stations and a 10-minute window starting at :15, they schedule at :15, :18, and :21.

## Development

### Package Installation
```bash
cd receivers
pip install -e .

# Dependencies
pip install apscheduler sqlalchemy  # For scheduler functionality
```

### Testing
```bash
# Test receiver communication
python -m pytest tests/ -v

# Test scheduler (Phase 3C)
pytest tests/test_scheduler_basic.py tests/test_scheduler_execution.py -v

# Test scheduler without starting
receivers scheduler test --stations TEST

# Test production logging
receivers download TEST --production --json-log --test-connection
```

### Environment Setup
```bash
# Required PYTHONPATH for development
export PYTHONPATH=../gtimes/src:../gps_parser/src:src

# Configuration directory
mkdir -p ~/.config/gpsconfig
# Add stations.cfg and postprocess.cfg from gps_parser
```

## Integration Points

### Dependencies
- **gps_parser**: Station configuration and path management
- **gtimes**: GPS time calculations and conversions
- **APScheduler**: Job scheduling and persistence
- **SQLAlchemy**: Database backend for job storage

### Monitoring Integration
- **Grafana Dashboards** (`docs/grafana/`), see `docs/grafana/README.md`
  - Runs via Docker Compose (`deployment/docker-dev/docker-compose.yml`) on port 3001
  - **Overview**: `gps_health_dashboard.json` — count boxes, station table, map panel
  - **Map**: `gps_map_dashboard.json` — full-screen geomap with count boxes
  - **Station Detail**: `gps_station_detail_dashboard.json` — per-station deep dive
  - Datasource: PostgreSQL `gps_health` database
  - Auto-provisioned dashboards and datasources via YAML configs
  - To update: edit the JSON file and `docker restart gps-grafana-dev`
  - Default refresh: 10s, filters: Area, Station, Receiver (NONE=missing), Antenna (NONE=missing), Status Filter
  - "Reset Filters" button in nav bar resets all filters to defaults
  - **Ping tolerance**: `station_connectivity` view requires 2 consecutive failed pings before reporting offline (prevents false-offline on lossy 3G/4G links)
- **Icinga 2**: Health data can be sent to monitoring endpoints
- **JSON logging**: Structured output for log aggregation systems
- **Email alerts**: Integration with gps-validation@vedur.is
- **Audit trails**: Performance metrics and failure analysis

### Manual Operation Compatibility
All scheduler functionality maintains complete compatibility with manual operations:
- Single station downloads work alongside scheduled operations
- Configuration changes apply to both manual and scheduled downloads
- Same validation and error handling for both modes
- Shared logging and audit systems

## Troubleshooting

### Common Issues
```bash
# APScheduler not available
pip install apscheduler sqlalchemy

# Station configuration not found
export GPS_CONFIG_PATH=~/.config/gpsconfig

# Connection failures
receivers download STATION --test-connection --verbose

# Scheduler debugging
receivers scheduler test --stations STATION --verbose

# Debug verbose output
receivers download STATION --sync --archive -v

# Check Phase 1 utilities are working (look for these log messages):
# "Using Phase 1 TimeParameterProcessor"
# "Using Phase 1 FileArchiver (IMMEDIATE mode)"
# "Archiving complete: X/Y files archived"
```

### Log Locations
- **Main logs**: `~/.cache/gps_receivers/logs/receivers.log`
- **Scheduler logs**: `~/.cache/gps_receivers/logs/scheduler.log`  
- **Audit trail**: `~/.cache/gps_receivers/logs/download_audit.jsonl`
- **Console output**: Concise production format or JSON

## Performance Notes

- **Concurrent downloads**: Default 5 workers, configurable via `--max-workers`
- **Station limits**: Use `--max-stations` for testing subsets
- **Network efficiency**: Time-distributed scheduling prevents congestion
- **Fault tolerance**: Immediate archiving prevents data loss on failures
- **Resource usage**: Production logging optimized for automated systems

## Phase 1 Integration Status

**Status**: ✅ Complete - All 4 receiver types integrated and tested

### Completed Receivers
- ✅ **PolaRX5** - Phase 1 default, tested with ELDC, OLKE, THOB
- ✅ **NetR9** - Phase 1 default, tested with MANA
- ✅ **NetRS** - Phase 1 default, tested with BLEI
- ✅ **G10** - Phase 1 default, tested with SKFC

### Implementation Pattern
All receivers use Phase 1 utilities by default:

1. **Time Processing**: TimeParameterProcessor validates and parses session parameters
2. **Download**: Protocol-specific clients (FTP/HTTP/TCP) download files
3. **Immediate Archiving**: FileArchiver archives each file right after download/processing
4. **Validation**: ArchiveValidator checks file integrity before and after archiving

### Benefits
- **Code Consolidation**: ~540 lines of duplicate code eliminated (Phase 3B)
- **Fault Tolerance**: Immediate archiving prevents data loss on crashes
- **Maintainability**: Single source of truth for common operations
- **Testing**: 72 comprehensive unit tests for Phase 1 utilities
- **Simplicity**: No feature flags, single code path

### Documentation
- **Phase 3C completion**: `docs/phase3c_complete.md` - Scheduler testing and extensibility
- **Scheduler guide**: `docs/scheduler/scheduler-guide.md` - Complete operational guide
- Phase 3B completion: `docs/phase3b_complete.md`
- Phase 2 completion: `docs/phase2_complete.md`
- Architecture diagrams: `docs/receivers/diagrams/`

---

**Last updated**: 2026-02-09
**Package version**: Development (gpslibrary_new)
**Phase Status**: Phase 3C Partial Complete - Flexible scheduling implemented, extensible architecture ready

## TODO / Known Issues

A systematic review is needed to address recurring patterns of issues found during dashboard and health monitoring development. See **`docs/CODE_REVIEW_TRACKER.md`** for the full tracking document with details, priorities, and status.

### High Priority
- **Protocol-agnostic data model**: Views and SQL assume PolaRX5 (FTP+HTTP+Control); Trimble HTTP-only receivers produce NULL/unknown in dashboards
- **Status value vocabulary**: Inconsistent use of `'open'`/`'ok'`/`'active'` across `block_port_status`, `station_port_status`, and health summary
- **Receiver capability awareness**: Dashboard should know what each receiver type supports (status session, control port, NTRIP) instead of per-field NULL checks

### Medium Priority
- **Codebase review**: Full audit of db_writer.py, connectivity_writer.py, and all extractors for protocol assumptions
- **Test coverage**: Integration tests for Trimble health flow end-to-end (extractor → db_writer → dashboard views)
- **Error handling patterns**: Standardize SAVEPOINT usage, transaction management, and value truncation across all DB writers

### Tracking
- Full issue tracker: `docs/CODE_REVIEW_TRACKER.md`
- Updated: 2026-02-09

**Maintainer**: Veðurstofa Íslands GPS Team