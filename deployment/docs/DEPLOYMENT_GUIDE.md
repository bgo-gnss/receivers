# GPS Receivers Scheduler - Production Deployment Guide

**Version**: 0.1.0
**Target System**: Veðurstofa Íslands Production Server
**Last Updated**: 2025-10-02

## Overview

This guide covers the deployment of the GPS Receivers Scheduler system for automated GNSS data downloads from the 173-station Icelandic Met Office network. The scheduler runs continuously, downloading three session types (15s_24hr, 1Hz_1hr, status_1hr) with fault-tolerant archiving and comprehensive monitoring.

## Prerequisites

### System Requirements

- **Operating System**: Linux (Ubuntu 20.04+ or RHEL 8+ recommended)
- **Architecture**: x86_64
- **RAM**: Minimum 4GB (8GB recommended for 100 workers)
- **CPU**: Multi-core (4+ cores recommended)
- **Disk Space**:
  - `/opt/receivers`: 500MB for application
  - `/var/cache/gps_receivers`: 10GB for logs and temporary files
  - `/mnt/gpsdata`: 2TB+ for archived data
- **Network**: Stable connection to GPS receivers (FTP/TCP)

### Required Software

```bash
# System Python
python3 >= 3.9        # System Python with venv support
python3-venv          # Virtual environment module
python3-pip           # Package installer
python3-dev           # Development headers
git                   # For cloning repositories

# System packages
- systemd (service management)
- logrotate (log rotation)
- sqlite3 (job persistence)
```

### Network Access

- Outbound FTP/TCP connections to GPS receivers
- Receiver IP addresses accessible from production server
- DNS resolution for receiver hostnames (if configured)
- Outbound HTTPS to github.com (for gps_parser package)
- Outbound HTTPS to PyPI (for gtimes package)
- Outbound HTTPS to git.vedur.is (for gps-config-data repository)

### Dependencies

The installation script automatically installs all required packages:

```bash
# Python packages (installed via pip in venv)
gtimes                     # From PyPI - GPS time calculations
gps_parser                 # From GitHub - Configuration management
receivers                  # From source - This package

# Python dependencies (automatically installed)
apscheduler >= 3.10.0     # Job scheduling
sqlalchemy >= 2.0.0       # Database backend
# Plus all dependencies from pyproject.toml
```

## Installation

### Step 1: Clone Repository

```bash
# Clone the receivers repository to /opt
cd /opt
sudo git clone https://github.com/bennigo/receivers.git
cd receivers
```

### Step 2: Run Installation Script

**Note**: If the gps-config-data repository is private, you may be prompted for git.vedur.is credentials during installation. For automated deployments, consider making the repository accessible via HTTPS without authentication or using a deploy token.

```bash
# Run the installation script as root from the repository root
sudo ./deployment/scripts/install.sh
```

The script will:
1. Check if running as root
2. Create `gpsops` system user and group
3. Create directory structure:
   - `/opt/receivers` - Application installation
   - `/opt/gps-config-data` - Configuration repository
   - `/etc/gpsconfig` - Deployed configuration files
   - `/var/cache/gps_receivers` - Cache, logs, temporary files
   - `/mnt/gpsdata` - Data archive
4. Set proper permissions (gpsops:gpsops ownership)
5. Install systemd service
6. Configure log rotation
7. Check system Python (install if needed)
8. Create Python virtual environment at `/opt/receivers/venv/`
9. Install Python packages:
   - `gtimes` from PyPI
   - `gps_parser` from GitHub (includes `gps-config` CLI tool)
   - `receivers` from current directory
10. Clone `gps-config-data` repository from git.vedur.is via HTTPS
11. Deploy configuration using `gps-config deploy`:
    - Auto-detects environment from hostname (e.g., rek.vedur.is)
    - Renders templates with environment-specific values
    - Deploys receivers.cfg, scheduler.yaml, postprocess.cfg to /etc/gpsconfig
12. Copy shared configuration files (stations.cfg, database.cfg, etc.)
13. Verify installation (commands and config files)

**Note**: Configuration deployment is fully automated. The `gps-config` tool automatically detects the environment and renders templates with the correct values for production, staging, or development.

### Step 3: Verify Installation

After installation completes, verify the deployment:

```bash
# Verify receivers command
sudo -u gpsops /opt/receivers/venv/bin/receivers --version

# Verify gps-config command
sudo -u gpsops /opt/receivers/venv/bin/gps-config --help

# Check deployed configuration files
ls -la /etc/gpsconfig/
# Should show: stations.cfg, receivers.cfg, postprocess.cfg, scheduler.yaml, etc.

# View deployed configuration
sudo -u gpsops /opt/receivers/venv/bin/receivers scheduler config --show

# Test with subset of stations (from /opt/receivers directory)
cd /opt/receivers
sudo -u gpsops venv/bin/receivers scheduler test \
  --stations REYK AKUR HOFN \
  --max-stations 3 \
  --verbose
```

### Updating Configuration

To update configuration after deployment:

```bash
# Pull latest configuration changes
cd /opt/gps-config-data
sudo -u gpsops git pull

# Redeploy configuration
sudo -u gpsops /opt/receivers/venv/bin/gps-config deploy --verbose

# Restart service to apply changes
sudo systemctl restart gps-receivers-scheduler
```

## Service Management

### Starting the Service

```bash
# Start the service
sudo systemctl start gps-receivers-scheduler

# Check status
sudo systemctl status gps-receivers-scheduler

# View live logs
sudo journalctl -u gps-receivers-scheduler -f

# Enable on boot
sudo systemctl enable gps-receivers-scheduler
```

### Stopping the Service

```bash
# Stop gracefully (allows jobs to finish)
sudo systemctl stop gps-receivers-scheduler

# Force stop (immediate)
sudo systemctl kill -s SIGKILL gps-receivers-scheduler
```

### Restarting the Service

```bash
# Restart (stop + start)
sudo systemctl restart gps-receivers-scheduler

# Reload configuration (graceful restart)
sudo systemctl reload gps-receivers-scheduler
```

### Checking Service Status

```bash
# Service status
sudo systemctl status gps-receivers-scheduler

# Show scheduled jobs
sudo -u gpsops /opt/miniforge3/envs/gpslibrary/bin/receivers scheduler status --show-jobs

# Check recent logs
sudo journalctl -u gps-receivers-scheduler -n 100

# Monitor resource usage
top -u gpsops
```

## Configuration Management

### Updating Scheduler Configuration

```bash
# 1. Edit configuration
sudo vim /etc/gpsconfig/scheduler.yaml

# 2. Validate configuration
sudo -u gpsops /opt/miniforge3/envs/gpslibrary/bin/receivers scheduler config --show

# 3. Restart service to apply changes
sudo systemctl restart gps-receivers-scheduler
```

### Updating Station Configuration

```bash
# 1. Edit station configuration
sudo vim /etc/gpsconfig/stations.cfg

# 2. Validate configuration
sudo -u gpsops /opt/miniforge3/envs/gpslibrary/bin/receivers validate <STATION>

# 3. Restart service
sudo systemctl restart gps-receivers-scheduler
```

### Adding New Stations

```bash
# 1. Add station to stations.cfg
# 2. Add receiver configuration to receivers.cfg
# 3. Test connection
sudo -u gpsops receivers download <STATION> --test-connection --verbose

# 4. Restart scheduler
sudo systemctl restart gps-receivers-scheduler
```

### Disabling Specific Sessions

To disable a session type for all stations, edit `/etc/gpsconfig/scheduler.yaml`:

```yaml
sessions:
  status_1hr:
    enabled: false  # Disable status downloads
```

To disable for specific stations:

```yaml
stations:
  OLDSTATION:
    sessions:
      status_1hr:
        enabled: false
```

## Monitoring

### Log Files

All logs are written to `/var/cache/gps_receivers/logs/`:

```bash
# Main scheduler log (human-readable)
tail -f /var/cache/gps_receivers/logs/scheduler.log

# Download audit trail (JSON structured)
tail -f /var/cache/gps_receivers/logs/download_audit.jsonl

# Individual station logs
tail -f /var/cache/gps_receivers/logs/<STATION>_*.log
```

### Log Rotation

Logs are automatically rotated via `/etc/logrotate.d/gps-receivers`:

- **Text logs (.log)**: Daily rotation, 30 days retention, compressed
- **JSON logs (.jsonl)**: Daily rotation, 90 days retention, 100MB max size

Manual rotation:

```bash
sudo logrotate -f /etc/logrotate.d/gps-receivers
```

### Systemd Journal

```bash
# View service logs
sudo journalctl -u gps-receivers-scheduler

# Follow live logs
sudo journalctl -u gps-receivers-scheduler -f

# Logs from last hour
sudo journalctl -u gps-receivers-scheduler --since "1 hour ago"

# Logs with errors only
sudo journalctl -u gps-receivers-scheduler -p err
```

### Performance Metrics

Check download statistics:

```bash
# Parse download audit log for statistics
cat /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -r '.download_time_seconds' | \
  awk '{sum+=$1; count++} END {print "Average download time:", sum/count, "seconds"}'

# Count successful downloads today
grep "$(date +%Y-%m-%d)" /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -r 'select(.status=="success")' | wc -l

# Failed downloads today
grep "$(date +%Y-%m-%d)" /var/cache/gps_receivers/logs/download_audit.jsonl | \
  jq -r 'select(.status=="failed")'
```

### Resource Usage

```bash
# Memory usage
ps aux | grep gps-scheduler | awk '{print $6/1024 " MB"}'

# CPU usage
top -u gpsops -b -n 1 | grep receivers

# Disk usage
du -sh /var/cache/gps_receivers
du -sh /mnt/gpsdata

# Database size
ls -lh ~/.cache/gps_receivers/scheduler.db

# Active connections
sudo lsof -u gpsops -i -a
```

## Troubleshooting

### Service Won't Start

```bash
# Check service status
sudo systemctl status gps-receivers-scheduler

# Check logs for errors
sudo journalctl -u gps-receivers-scheduler -n 100

# Common issues:
# 1. Python environment not found
ls -la /opt/miniforge3/envs/gpslibrary/bin/receivers

# 2. Configuration files missing
ls -la /etc/gpsconfig/*.yaml

# 3. Permission issues
sudo chown -R gpsops:gpsops /var/cache/gps_receivers
sudo chown -R gpsops:gpsops /mnt/gpsdata
```

### Downloads Failing

```bash
# Test connection to specific station
sudo -u gpsops receivers download <STATION> --test-connection --verbose

# Check network connectivity
ping <receiver-ip>
telnet <receiver-ip> 21  # FTP port

# Check credentials
cat /etc/gpsconfig/receivers.cfg | grep <STATION>

# Verify session support
sudo -u gpsops receivers validate <STATION> --verbose
```

### Database Issues

```bash
# Check database integrity
sqlite3 ~/.cache/gps_receivers/scheduler.db "PRAGMA integrity_check;"

# View scheduled jobs
sqlite3 ~/.cache/gps_receivers/scheduler.db "SELECT * FROM apscheduler_jobs;"

# Clear corrupted database (will lose job history)
sudo systemctl stop gps-receivers-scheduler
sudo -u gpsops rm ~/.cache/gps_receivers/scheduler.db
sudo systemctl start gps-receivers-scheduler
```

### High Resource Usage

```bash
# Reduce max_workers in configuration
sudo vim /etc/gpsconfig/scheduler.yaml
# Set max_workers to lower value (e.g., 50)

# Restart service
sudo systemctl restart gps-receivers-scheduler

# Check memory limits
systemctl show gps-receivers-scheduler | grep Memory

# Adjust systemd resource limits
sudo vim /etc/systemd/system/gps-receivers-scheduler.service
# Modify MemoryMax, CPUQuota as needed
sudo systemctl daemon-reload
sudo systemctl restart gps-receivers-scheduler
```

### Log Files Growing Too Large

```bash
# Check log sizes
du -sh /var/cache/gps_receivers/logs/*

# Force log rotation
sudo logrotate -f /etc/logrotate.d/gps-receivers

# Adjust retention in logrotate config
sudo vim /etc/logrotate.d/gps-receivers
# Change 'rotate 30' to lower value

# Clean old compressed logs manually
find /var/cache/gps_receivers/logs -name "*.gz" -mtime +90 -delete
```

## Backup and Recovery

### Backup Critical Data

```bash
# Configuration files
sudo tar -czf gps-scheduler-config-$(date +%Y%m%d).tar.gz /etc/gpsconfig/

# Database (job history)
sudo -u gpsops cp ~/.cache/gps_receivers/scheduler.db \
  /backup/scheduler-db-$(date +%Y%m%d).db

# Archive data
rsync -av /mnt/gpsdata/ /backup/gpsdata/
```

### Restore from Backup

```bash
# Stop service
sudo systemctl stop gps-receivers-scheduler

# Restore configuration
sudo tar -xzf gps-scheduler-config-20250102.tar.gz -C /

# Restore database
sudo -u gpsops cp /backup/scheduler-db-20250102.db ~/.cache/gps_receivers/scheduler.db

# Start service
sudo systemctl start gps-receivers-scheduler
```

### Disaster Recovery

```bash
# Complete reinstallation from backup
# 1. Run installation script
sudo ./deployment/scripts/install.sh

# 2. Restore configuration
sudo tar -xzf backup/gps-scheduler-config.tar.gz -C /

# 3. Restore data archive
rsync -av /backup/gpsdata/ /mnt/gpsdata/

# 4. Start service
sudo systemctl start gps-receivers-scheduler
sudo systemctl enable gps-receivers-scheduler
```

## Upgrade Procedure

### Minor Version Upgrades (e.g., 0.1.0 → 0.1.1)

```bash
# 1. Stop service
sudo systemctl stop gps-receivers-scheduler

# 2. Backup current installation
sudo tar -czf receivers-backup-$(date +%Y%m%d).tar.gz /opt/receivers/

# 3. Update code
cd /opt/receivers
sudo -u gpsops git pull origin main

# 4. Reinstall package
sudo -u gpsops /opt/miniforge3/envs/gpslibrary/bin/pip install -e .

# 5. Start service
sudo systemctl start gps-receivers-scheduler

# 6. Verify
sudo systemctl status gps-receivers-scheduler
```

### Major Version Upgrades (e.g., 0.1.x → 0.2.0)

```bash
# 1. Review changelog for breaking changes
cat /opt/receivers/CHANGELOG.md

# 2. Backup everything
sudo tar -czf gps-scheduler-full-backup-$(date +%Y%m%d).tar.gz \
  /opt/receivers /etc/gpsconfig ~/.cache/gps_receivers/scheduler.db

# 3. Test in staging environment first

# 4. Follow minor upgrade procedure
# 5. Update configuration files as needed
# 6. Verify all functionality
```

## Security Considerations

### File Permissions

```bash
# Verify permissions are correct
ls -la /opt/receivers  # Should be gpsops:gpsops, 755
ls -la /etc/gpsconfig  # Config files 644, directory 755
ls -la /var/cache/gps_receivers  # Should be gpsops:gpsops, 700
```

### Network Security

- Receiver credentials stored in `/etc/gpsconfig/receivers.cfg` (mode 644)
- FTP connections unencrypted (standard for GPS receivers)
- Consider firewall rules to restrict outbound connections
- Monitor failed login attempts in logs

### Systemd Security Features

The service includes security hardening:
- `NoNewPrivileges=true` - Prevents privilege escalation
- `PrivateTmp=true` - Isolated /tmp directory
- `ProtectSystem=strict` - Read-only filesystem except explicit paths
- `ProtectHome=true` - No access to user home directories
- `ReadWritePaths=/var/cache/gps_receivers /mnt/gpsdata` - Limited write access

### Log Security

- Logs may contain receiver connection details
- Rotate and compress logs regularly
- Consider log aggregation to secure logging server
- Set appropriate retention periods

## Performance Tuning

### Worker Configuration

Adjust `max_workers` based on system resources:

```yaml
# Conservative (4GB RAM, 4 cores)
scheduler:
  max_workers: 30

# Moderate (8GB RAM, 8 cores)
scheduler:
  max_workers: 50

# Aggressive (16GB+ RAM, 16+ cores)
scheduler:
  max_workers: 100
```

### Distribution Window

Adjust `distribution_window` to spread load:

```yaml
sessions:
  1Hz_1hr:
    distribution_window: 15  # Increase to 15 minutes for more spread
```

### Database Optimization

```bash
# Vacuum database periodically
sqlite3 ~/.cache/gps_receivers/scheduler.db "VACUUM;"

# Analyze for query optimization
sqlite3 ~/.cache/gps_receivers/scheduler.db "ANALYZE;"
```

## Contact and Support

- **Technical Issues**: GPS Team at Veðurstofa Íslands
- **Email**: gps-validation@vedur.is
- **Documentation**: https://github.com/bennigo/receivers
- **Issue Tracker**: Internal ticketing system

## Appendix

### Service File Reference

Location: `/etc/systemd/system/gps-receivers-scheduler.service`

```ini
[Unit]
Description=GPS Receivers Scheduler - Automated GNSS Data Download
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=gpsops
Group=gpsops
WorkingDirectory=/opt/receivers

# Environment setup
Environment="GPS_CONFIG_PATH=/etc/gpsconfig"
Environment="GPS_CACHE_DIR=/var/cache/gps_receivers"

# Command to run
ExecStart=/opt/receivers/venv/bin/receivers scheduler start \
    --max-workers 100 \
    --config /etc/gpsconfig/scheduler.yaml

# Restart policy
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

### Directory Structure

```
/opt/receivers/           # Application installation
├── src/                  # Python source code
├── venv/                 # Python virtual environment
│   ├── bin/
│   │   └── receivers    # Command entry point
│   └── lib/             # Installed packages
├── deployment/           # Deployment artifacts
└── pyproject.toml        # Package metadata

/etc/gpsconfig/           # Configuration files
├── stations.cfg          # Station definitions
├── receivers.cfg         # Receiver credentials
├── postprocess.cfg       # Postprocessing settings
└── scheduler.yaml        # Scheduler configuration

/var/cache/gps_receivers/ # Cache and runtime data
├── logs/                 # All log files
│   ├── scheduler.log
│   ├── download_audit.jsonl
│   └── <station>_*.log
├── tmp/                  # Temporary downloads
└── scheduler.db          # SQLite job database

/mnt/gpsdata/             # Data archive
└── <station>/            # Per-station directories
    ├── 15s_24hr/
    ├── 1Hz_1hr/
    └── status_1hr/
```

### Quick Reference Commands

```bash
# Service management
sudo systemctl start gps-receivers-scheduler
sudo systemctl stop gps-receivers-scheduler
sudo systemctl restart gps-receivers-scheduler
sudo systemctl status gps-receivers-scheduler

# View logs
sudo journalctl -u gps-receivers-scheduler -f
tail -f /var/cache/gps_receivers/logs/scheduler.log

# Test configuration (using venv)
sudo -u gpsops /opt/receivers/venv/bin/receivers scheduler config --show
sudo -u gpsops /opt/receivers/venv/bin/receivers scheduler test --stations REYK --verbose

# Manual download
sudo -u gpsops /opt/receivers/venv/bin/receivers download <STATION> --sync --archive --verbose

# Check status
sudo -u gpsops /opt/receivers/venv/bin/receivers scheduler status --show-jobs
ps aux | grep gps-scheduler
