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

### Scheduling System
- **Time distribution**: Downloads spread across 10-minute windows to avoid network congestion
- **Session types**: 
  - `15s_24hr`: Daily downloads at 00:10-00:19
  - `1Hz_1hr`: Hourly downloads at XX:15-XX:24  
  - `status_1hr`: Hourly status at XX:25-XX:29
- **Persistence**: SQLite job store survives restarts
- **Manual compatibility**: All manual operations remain fully functional

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

---

**Last updated**: 2025-09-10  
**Package version**: Development (gpslibrary_new)  
**Maintainer**: Veðurstofan Íslands GPS Team