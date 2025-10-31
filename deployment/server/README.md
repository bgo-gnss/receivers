# GPS Receivers Scheduler - Server Production Deployment

Production server deployment for GPS receivers scheduler with systemd service.

## Quick Start

```bash
# Clone receivers repository
sudo git clone https://github.com/bennigo/receivers.git /opt/receivers
cd /opt/receivers

# Run installation
sudo ./deployment/server/install.sh

# Start service
sudo systemctl start gps-receivers-scheduler
sudo systemctl status gps-receivers-scheduler

# Enable on boot
sudo systemctl enable gps-receivers-scheduler

# View logs
sudo journalctl -u gps-receivers-scheduler -f
```

## Prerequisites

- **Operating System**: Ubuntu 20.04+ or RHEL 8+
- **Python**: 3.9+ with venv support
- **Git**: For cloning repositories
- **Disk Space**:
  - 10GB+ on `/var/cache`
  - 2TB+ on `/mnt/gpsdata`
- **Network**:
  - Outbound FTP/TCP to GPS receivers
  - HTTPS to github.com, PyPI
  - Access to git.vedur.is (for config)

## Installation Process

The `install.sh` script performs the following:

1. **Creates system user**: `gpsops`
2. **Creates directories**:
   - `/opt/receivers` - Application
   - `/etc/gpsconfig` - Configuration
   - `/var/cache/gps_receivers` - Logs and cache
   - `/mnt/gpsdata` - GPS data files
3. **Installs Python packages**: gtimes, gps_parser, receivers
4. **Clones configuration**: From git.vedur.is/bgo/gps-config-data
5. **Deploys configuration**: Auto-detects environment
6. **Installs systemd service**: `gps-receivers-scheduler.service`
7. **Configures log rotation**: `/etc/logrotate.d/gps-receivers`

## Configuration

### Auto-Detection

The installer auto-detects the environment based on hostname:
- `rek.vedur.is` → uses `rek.vedur.is.env`
- Other hostnames → looks for matching `.env` file

### Manual Environment

Override auto-detection:

```bash
# After installation, manually deploy config
cd /opt/gps-config-data
sudo -u gpsops python3 deploy.py --env production --target /etc/gpsconfig
```

### Configuration Files

Deployed to `/etc/gpsconfig/`:
- `stations.cfg` - Station definitions (shared)
- `receivers.cfg` - Receiver settings (templated)
- `scheduler.yaml` - Scheduler configuration (templated)
- `postprocess.cfg` - Post-processing (templated)
- `database.cfg` - Database settings (shared)
- `icinga.cfg` - Monitoring config (shared)

## Service Management

### Start/Stop/Restart

```bash
# Start
sudo systemctl start gps-receivers-scheduler

# Stop
sudo systemctl stop gps-receivers-scheduler

# Restart
sudo systemctl restart gps-receivers-scheduler

# Status
sudo systemctl status gps-receivers-scheduler
```

### Enable/Disable Auto-Start

```bash
# Enable on boot
sudo systemctl enable gps-receivers-scheduler

# Disable auto-start
sudo systemctl disable gps-receivers-scheduler
```

### View Logs

```bash
# Live logs
sudo journalctl -u gps-receivers-scheduler -f

# Recent logs
sudo journalctl -u gps-receivers-scheduler -n 100

# Since specific time
sudo journalctl -u gps-receivers-scheduler --since "2 hours ago"
```

## Manual Operations

All manual commands work alongside the scheduler:

```bash
# Use the venv
source /opt/receivers/venv/bin/activate

# Or use full path
/opt/receivers/venv/bin/receivers <command>

# Examples:
receivers download ELDC --sync --archive -v
receivers status THOB
receivers health SKFC --json --save-db
receivers scheduler status --show-jobs
```

## Monitoring

### Scheduler Status

```bash
receivers scheduler status --show-jobs
```

### Check Logs

```bash
# Scheduler log
tail -f /var/cache/gps_receivers/logs/scheduler.log

# Download audit
tail -f /var/cache/gps_receivers/logs/download_audit.jsonl | jq .

# Main log
tail -f /var/cache/gps_receivers/logs/receivers.log
```

### Data Verification

```bash
# Check downloaded data
ls -lh /mnt/gpsdata/2025/oct/

# Check specific station
ls -lh /mnt/gpsdata/2025/oct/ELDC/
```

## Updates

### Update Scheduler Code

```bash
cd /opt/receivers
sudo git pull
sudo -u gpsops /opt/receivers/venv/bin/pip install -e .
sudo systemctl restart gps-receivers-scheduler
```

### Update Configuration

```bash
cd /opt/gps-config-data
sudo git pull
sudo -u gpsops python3 deploy.py --env production --target /etc/gpsconfig
sudo systemctl restart gps-receivers-scheduler
```

### Update Python Packages

```bash
sudo -u gpsops /opt/receivers/venv/bin/pip install --upgrade gtimes gps_parser
sudo systemctl restart gps-receivers-scheduler
```

## Troubleshooting

### Service Won't Start

```bash
# Check service status
sudo systemctl status gps-receivers-scheduler

# Check full logs
sudo journalctl -u gps-receivers-scheduler -xe

# Test manually
sudo -u gpsops /opt/receivers/venv/bin/receivers scheduler test --verbose
```

### Configuration Issues

```bash
# Verify config files
ls -la /etc/gpsconfig/

# Check configuration validity
receivers validate --verbose

# Re-deploy configuration
cd /opt/gps-config-data
sudo -u gpsops python3 deploy.py --env <your-env> --target /etc/gpsconfig
```

### Disk Space Issues

```bash
# Check disk usage
df -h /mnt/gpsdata
df -h /var/cache/gps_receivers

# Clean old logs (automatic via logrotate, but manual if needed)
sudo find /var/cache/gps_receivers/logs -type f -mtime +30 -delete
```

### Network/Receiver Issues

```bash
# Test receiver connectivity
receivers status ELDC --verbose

# Test download
receivers download ELDC -D 1 --test-connection --verbose
```

## Uninstallation

```bash
# Stop and disable service
sudo systemctl stop gps-receivers-scheduler
sudo systemctl disable gps-receivers-scheduler

# Remove service file
sudo rm /etc/systemd/system/gps-receivers-scheduler.service
sudo systemctl daemon-reload

# Remove application (CAUTION: This deletes data!)
sudo rm -rf /opt/receivers
sudo rm -rf /opt/gps-config-data
sudo rm -rf /var/cache/gps_receivers

# Optionally remove data
# sudo rm -rf /mnt/gpsdata

# Remove user
sudo userdel gpsops
```

## Production Checklist

Before going to production:

- [ ] Test on staging/development server first
- [ ] Verify all station configurations
- [ ] Test downloads for critical stations
- [ ] Set up monitoring (Icinga/Grafana)
- [ ] Configure alerting
- [ ] Document environment-specific settings
- [ ] Plan backup strategy for `/mnt/gpsdata`
- [ ] Review log rotation settings
- [ ] Test service restart after reboot
