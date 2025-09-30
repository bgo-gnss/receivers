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

### Scheduler Configuration
```bash
# Create default configuration
receivers scheduler config --create

# Configuration location: ~/.config/gps_receivers/scheduler.json
# Database: ~/.cache/gps_receivers/scheduler.db
# Logs: ~/.cache/gps_receivers/logs/
```

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
- Phase 3B completion: `docs/phase3b_complete.md`
- Phase 2 completion: `docs/phase2_complete.md`
- Architecture diagrams: `docs/receivers/diagrams/`

---

**Last updated**: 2025-09-30
**Package version**: Development (gpslibrary_new)
**Phase Status**: Phase 3B Complete - Legacy code removed, Phase 1 is now default
**Maintainer**: Veðurstofan Íslands GPS Team