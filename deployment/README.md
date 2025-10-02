# GPS Receivers Scheduler - Production Deployment

**Package**: receivers v0.1.0
**Organization**: Veðurstofa Íslands (Icelandic Met Office)
**System**: Automated GNSS data download for 173-station network
**Last Updated**: 2025-10-02

## 📦 What's in This Directory

This directory contains everything needed to deploy the GPS Receivers Scheduler to production:

```
deployment/
├── README.md                          # This file - deployment overview
├── docs/
│   ├── DEPLOYMENT_GUIDE.md           # Complete installation and configuration guide
│   └── MONITORING_GUIDE.md           # Monitoring, alerting, and health checks
├── scripts/
│   └── install.sh                    # Automated installation script
├── systemd/
│   └── gps-receivers-scheduler.service  # Systemd service definition
└── logrotate.d/
    └── gps-receivers                 # Log rotation configuration
```

## 🚀 Quick Start

### For DevOps Engineers (First-Time Deployment)

1. **Read the deployment guide**:
   ```bash
   cat docs/DEPLOYMENT_GUIDE.md
   ```

2. **Run installation script** (fully automated):
   ```bash
   sudo ./scripts/install.sh
   ```
   This will automatically:
   - Create system users and directories
   - Install Python packages (gtimes, gps_parser, receivers)
   - Clone gps-config-data repository
   - Deploy environment-specific configuration
   - Set up systemd service and log rotation

3. **Start and verify service**:
   ```bash
   sudo systemctl start gps-receivers-scheduler
   sudo systemctl status gps-receivers-scheduler
   sudo journalctl -u gps-receivers-scheduler -f
   ```

4. **Set up monitoring** (see `docs/MONITORING_GUIDE.md`):
   - Configure Icinga checks
   - Set up email alerts
   - Review monitoring dashboard

### For System Administrators (Daily Operations)

```bash
# Check service status
sudo systemctl status gps-receivers-scheduler

# View live logs
sudo journalctl -u gps-receivers-scheduler -f

# Restart service
sudo systemctl restart gps-receivers-scheduler

# Check download statistics
tail -f /var/cache/gps_receivers/logs/download_audit.jsonl | jq .
```

## 📋 Pre-Deployment Checklist

### System Prerequisites

- [ ] Operating System: Ubuntu 20.04+ or RHEL 8+
- [ ] Python 3.9+ installed with venv support
- [ ] Git installed for repository cloning
- [ ] Disk space: 10GB+ on `/var/cache`, 2TB+ on `/mnt/gpsdata`
- [ ] Network access: Outbound FTP/TCP to GPS receivers
- [ ] Network access: HTTPS to github.com, PyPI, and git.vedur.is
- [ ] System user: `gpsops` will be created by install.sh

### Configuration Repository

- [ ] Access to gps-config-data repository at git.vedur.is (via HTTPS)
- [ ] Environment file exists for target server (e.g., rek.vedur.is.env)
- [ ] Configuration templates are up to date in gps-config-data
- [ ] Repository is public or credentials available (for private repos)

**Note**: Configuration files are automatically deployed from the gps-config-data repository during installation. No manual file preparation needed.

### Testing Completed

- [ ] Overnight scheduler test successful (9+ hours, 1799+ jobs)
- [ ] Download validation passed
- [ ] Archive integrity verified
- [ ] Resource usage acceptable (< 250MB RAM, < 2% CPU average)

## 📚 Documentation Reference

### Primary Guides

| Document | Purpose | Audience |
|----------|---------|----------|
| [DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | Installation, configuration, upgrade procedures | DevOps Engineers |
| [MONITORING_GUIDE.md](docs/MONITORING_GUIDE.md) | Health checks, alerting, troubleshooting | System Administrators, SRE |

### Configuration Files

| File | Location | Description |
|------|----------|-------------|
| systemd service | `systemd/gps-receivers-scheduler.service` | Service definition with security hardening |
| Log rotation | `logrotate.d/gps-receivers` | Log retention policy |
| Installation script | `scripts/install.sh` | Automated setup |

## 🔧 System Overview

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  GPS Receivers Scheduler                     │
│                                                              │
│  ┌────────────┐    ┌──────────────┐    ┌────────────────┐ │
│  │ APScheduler│───>│ Thread Pool  │───>│ FTP Downloads  │ │
│  │  (SQLite)  │    │ 100 workers  │    │   173 stations │ │
│  └────────────┘    └──────────────┘    └────────────────┘ │
│         │                  │                     │          │
│         v                  v                     v          │
│  ┌────────────┐    ┌──────────────┐    ┌────────────────┐ │
│  │ Job        │    │ Download     │    │ Immediate      │ │
│  │ Persistence│    │ Validation   │    │ Archiving      │ │
│  └────────────┘    └──────────────┘    └────────────────┘ │
│                                                              │
│  Logging: /var/cache/gps_receivers/logs/                   │
│  Archive: /mnt/gpsdata/<station>/<session>/                │
└─────────────────────────────────────────────────────────────┘
```

### Download Sessions

| Session | Frequency | File Type | Distribution Window | Archive |
|---------|-----------|-----------|---------------------|---------|
| `15s_24hr` | Twice daily (00:01, 06:00) | Daily 15-second data | 10 minutes | 7 days lookback |
| `1Hz_1hr` | Hourly (:01) | Hourly 1Hz data | 10 minutes | 24 hours lookback |
| `status_1hr` | Hourly (:01) | Status logs | 10 minutes | 24 hours lookback |

### Resource Requirements

| Resource | Minimum | Recommended | Limit (systemd) |
|----------|---------|-------------|-----------------|
| RAM | 4GB | 8GB | 2GB |
| CPU | 4 cores | 8+ cores | 200% |
| Disk (/var/cache) | 5GB | 10GB | - |
| Disk (/mnt/gpsdata) | 1TB | 2TB+ | - |
| Network | 100Mbps | 1Gbps | - |

## 🔐 Security Considerations

### Service Hardening (systemd)

The service runs with multiple security restrictions:

- **User isolation**: Runs as dedicated `gpsops` user (no login shell)
- **Privilege restrictions**: `NoNewPrivileges=true`
- **Filesystem protection**: `ProtectSystem=strict`, `ProtectHome=true`
- **Temporary isolation**: `PrivateTmp=true`
- **Limited write access**: Only `/var/cache/gps_receivers` and `/mnt/gpsdata`

### File Permissions

```bash
/opt/receivers         → gpsops:gpsops (755)
/etc/gpsconfig         → root:root (755), files 644
/var/cache/gps_receivers → gpsops:gpsops (700)
/mnt/gpsdata          → gpsops:gpsops (755)
```

### Network Security

- All FTP connections are outbound only (no listening ports)
- Receiver credentials stored in `/etc/gpsconfig/receivers.cfg` (mode 644)
- Consider firewall rules to restrict outbound connections to known receiver IPs

## 📊 Production Validation

### Pre-Production Testing Results

**Test Date**: 2025-10-01 21:59 - 2025-10-02 07:24 (9h 25min)

| Metric | Result | Status |
|--------|--------|--------|
| Runtime | 9+ hours | ✅ PASS |
| Jobs completed | 1,799 | ✅ PASS |
| Files downloaded | 2,503 | ✅ PASS |
| Files archived | 1,992 | ✅ PASS |
| Crashes/errors | 0 | ✅ PASS |
| Memory usage | ~250MB | ✅ PASS |
| CPU usage | ~1.2% average | ✅ PASS |
| Success rate | >95% | ✅ PASS |

### Test Configuration

- **Stations tested**: Full network (173 stations)
- **Sessions tested**: All three (15s_24hr, 1Hz_1hr, status_1hr)
- **Worker count**: 10 (production: 100)
- **Test duration**: 9+ hours overnight
- **Test environment**: Development laptop

## 🎯 Key Features

### Scheduler Capabilities

✅ **Distributed downloads**: Time-distributed to prevent network congestion
✅ **Fault tolerance**: Immediate archiving, survives crashes
✅ **Gap filling**: Lookback periods download missing data automatically
✅ **Session filtering**: Skips unsupported sessions by receiver type
✅ **Production logging**: Structured JSON and human-readable formats
✅ **Job persistence**: SQLite database survives service restarts
✅ **Manual compatibility**: All manual operations remain functional
✅ **Resource limits**: systemd controls memory, CPU, file descriptors
✅ **Automatic restart**: Service restarts on failure
✅ **Health monitoring**: Watchdog timer detects hangs

### Supported Receivers

- **Septentrio PolaRX5** - Full feature support (all sessions)
- **Leica/Trimble NetR9** - Observation data only (no status)
- **Leica/Trimble NetRS** - Observation data only (no status)
- **Leica G10** - Observation data only (no status)

## 🔍 Monitoring Integration

The system provides multiple monitoring endpoints:

### Built-in Monitoring

- **systemd watchdog** - 300s timeout, automatic restart
- **Structured logs** - JSON format at `/var/cache/gps_receivers/logs/download_audit.jsonl`
- **Performance metrics** - Download times, success rates, resource usage

### External Integration Points

- **Icinga 2** - Health check script provided (see MONITORING_GUIDE.md)
- **Prometheus** - Metrics export example (see MONITORING_GUIDE.md)
- **Email alerts** - Template scripts for critical events
- **Syslog** - All logs available via `journalctl`

### Critical Alerts

The monitoring guide defines alerting thresholds for:

- Service down (immediate)
- Restart loops (>3 in 5 minutes)
- Memory leak (>1.8GB)
- Low success rate (<90%)
- Slow downloads (>2x normal)
- Disk space (>80%)

## 📞 Support and Contact

### During Deployment

- **Primary Contact**: GPS Team at Veðurstofa Íslands
- **Email**: gps-validation@vedur.is
- **Documentation**: This deployment directory
- **Issue Tracking**: Internal ticketing system

### Post-Deployment

- **Daily Monitoring**: System administrators via dashboard
- **Alert Response**: On-call DevOps rotation
- **Escalation**: GPS team manager

## 🔄 Upgrade Procedures

### Minor Upgrades (0.1.x → 0.1.y)

```bash
# Stop service
sudo systemctl stop gps-receivers-scheduler

# Update receivers code
cd /opt/receivers
sudo -u gpsops git pull
sudo -u gpsops /opt/receivers/venv/bin/pip install -e .

# Update configuration (if changed)
cd /opt/gps-config-data
sudo -u gpsops git pull
sudo -u gpsops /opt/receivers/venv/bin/gps-config deploy --verbose

# Restart service
sudo systemctl start gps-receivers-scheduler
```

### Major Upgrades (0.1.x → 0.2.x)

1. Review CHANGELOG for breaking changes
2. Backup configuration and database
3. Test in staging environment
4. Follow deployment guide for migration steps
5. Update configuration files as needed
6. Restart service and verify functionality

## 📦 Deployment Artifacts

### Installation Creates

```
/opt/receivers/                        # Application code
  └── venv/                           # Python virtual environment
/opt/gps-config-data/                 # Configuration repository
  ├── environments/                   # Environment files
  ├── *.template                      # Configuration templates
  └── *.cfg                          # Shared config files
/etc/gpsconfig/                        # Deployed configuration
  ├── stations.cfg                    # (from gps-config-data)
  ├── receivers.cfg                   # (rendered from template)
  ├── postprocess.cfg                 # (rendered from template)
  └── scheduler.yaml                  # (rendered from template)
/var/cache/gps_receivers/             # Runtime data
  ├── logs/                           # All log files
  ├── tmp/                            # Temporary downloads
  └── scheduler.db                    # Job database
/mnt/gpsdata/                         # Archive
  └── <station>/
      ├── 15s_24hr/
      ├── 1Hz_1hr/
      └── status_1hr/
/etc/systemd/system/
  └── gps-receivers-scheduler.service
/etc/logrotate.d/
  └── gps-receivers
```

### Log Files

| File | Format | Retention | Purpose |
|------|--------|-----------|---------|
| `scheduler.log` | Text | 30 days | Human-readable events |
| `download_audit.jsonl` | JSON | 90 days | Structured download metrics |
| `systemd journal` | Binary | systemd default | Service lifecycle events |

## 🎓 Additional Resources

### Related Documentation

- **CLAUDE.md** - Development context and package overview
- **DEVELOPMENT_ROADMAP.md** - Project phases and future features
- **pyproject.toml** - Package dependencies and metadata

### Example Commands

```bash
# Test configuration without starting
receivers scheduler config --show

# Test with subset of stations
receivers scheduler test --stations REYK AKUR HOFN --verbose

# Start with limited workers (development)
receivers scheduler start --stations REYK --max-workers 2 --verbose

# Manual download (always works alongside scheduler)
receivers download REYK --sync --archive --verbose

# Check specific station health
receivers health REYK --verbose

# Validate receiver configuration
receivers validate REYK --verbose
```

## ⚠️ Important Notes

### Production Considerations

1. **Configuration changes require restart** - Edit `/etc/gpsconfig/scheduler.yaml`, then `systemctl restart`
2. **Manual downloads still work** - Scheduler doesn't interfere with manual operations
3. **Database can be deleted** - Recreates job schedule on restart (loses history only)
4. **Logs rotate automatically** - No manual cleanup needed
5. **Archive grows continuously** - Monitor disk space, plan capacity
6. **Network-bound workload** - High parallelization (100 workers) is safe

### First Week Monitoring

During the first week of production:

- Check dashboard daily
- Review download success rate
- Monitor resource usage trends
- Tune alert thresholds based on observed patterns
- Verify log rotation working correctly
- Confirm backup procedures

## ✅ Deployment Checklist

### Pre-Deployment

- [ ] Read DEPLOYMENT_GUIDE.md completely
- [ ] Read MONITORING_GUIDE.md completely
- [ ] Prepare all configuration files
- [ ] Verify Python environment exists
- [ ] Check disk space requirements
- [ ] Confirm network access to receivers
- [ ] Set up monitoring system integration
- [ ] Configure email alerts

### Deployment Day

- [ ] Verify HTTPS access to git.vedur.is (if repo is private, have credentials ready)
- [ ] Run install.sh script (fully automated)
- [ ] Verify configuration deployed to /etc/gpsconfig
- [ ] Check file permissions (should be automatic)
- [ ] Test with subset of stations first
- [ ] Start full service
- [ ] Monitor logs for first hour
- [ ] Verify downloads completing successfully
- [ ] Enable service on boot

### Post-Deployment

- [ ] Monitor resource usage for 24 hours
- [ ] Review first daily report
- [ ] Verify log rotation working
- [ ] Test alert notifications
- [ ] Document any issues encountered
- [ ] Schedule first week daily checks
- [ ] Update runbook with learnings

---

**Document Version**: 1.0
**Created**: 2025-10-02
**Status**: Ready for Production Deployment
**Approved By**: [Pending DevOps Review]
