# GPS Receivers Scheduler - Production Deployment

Production deployment options for GPS receivers scheduler.

## 🚀 Deployment Options

### Option 1: Docker Production Deployment

**Best for**: Containerized environments, easy deployment, testing

```bash
cd deployment/docker
docker-compose up -d
```

✅ **Advantages**:
- Isolated environment
- Easy to start/stop
- No system-wide changes
- Portable across machines

📖 **Full guide**: [deployment/docker/README.md](docker/README.md)

---

### Option 2: Server Production Deployment

**Best for**: Production servers, systemd integration, IMO infrastructure

```bash
# As bgo (owns repo + venv):
git clone https://github.com/bennigo/receivers.git ~/git/receivers
cd ~/git/receivers
sudo bash deployment/server/install.sh

# As gpsops (owns service + data):
ssh gpsops@host 'systemctl --user start gps-receivers-scheduler'
```

✅ **Advantages**:
- User-level systemd service (no system-wide daemon, runs as `gpsops`)
- Automatic startup on boot via user-linger
- Per-user logging (`journalctl --user-unit`)
- Native performance

📖 **Full guide**: [deployment/server/README.md](server/README.md)

---

## 📊 Feature Comparison

| Feature | Docker | Server |
|---------|--------|--------|
| **Installation** | docker-compose up | Run install.sh |
| **Service Management** | docker-compose | systemd (user unit) |
| **Logs** | docker logs | `journalctl --user-unit` |
| **Auto-start** | restart: unless-stopped | `loginctl enable-linger gpsops` + `systemctl --user enable` |
| **Updates** | Rebuild image | git pull + restart |
| **Isolation** | ✅ Containerized | Per-user (runs as `gpsops`, no root daemon) |
| **Resource Limits** | ✅ Built-in | Manual (systemd, requires user@.service delegation) |
| **Portability** | ✅ Very portable | ⚠️ OS-dependent |

## 🧪 Testing Deployment

For testing the installation process without production data:

```bash
cd deployment/test
./test-install.sh  # Test in Docker
```

📖 **Test documentation**: [deployment/test/README.md](test/README.md)

## 📁 Directory Structure

```
deployment/
├── README.md              # This file - deployment overview
│
├── docker/                # Docker production deployment
│   ├── Dockerfile         # Production image
│   ├── docker-compose.yml # Service definition
│   ├── entrypoint.sh      # Container startup script
│   └── README.md          # Docker deployment guide
│
├── server/                # Server production deployment
│   ├── install.sh         # Installation script
│   └── README.md          # Server deployment guide
│
├── test/                  # Testing/validation
│   ├── Dockerfile.test    # Test image
│   ├── test-install.sh    # Installation test
│   └── run-scheduler-*.sh # Scheduler tests
│
├── systemd/               # Systemd service files
│   └── gps-receivers-scheduler.service
│
├── logrotate.d/           # Log rotation
│   └── gps-receivers
│
└── docs/                  # Additional documentation
    ├── DEPLOYMENT_GUIDE.md
    └── MONITORING_GUIDE.md
```

## 🔧 Configuration

Both deployment methods fetch configuration from:
```
https://git.vedur.is/bgo/gps-config-data.git
```

The configuration includes:
- Station definitions (173 stations)
- Receiver settings
- Scheduler configuration
- Environment-specific settings

Environment is auto-detected based on hostname or can be set manually.

## 🏥 Health Monitoring

Both deployments support health monitoring:

```bash
# Check health (Docker)
docker exec gps-receivers-scheduler receivers health ELDC --json

# Check health (Server)
receivers health ELDC --json --save-db

# Icinga/Nagios plugin
/opt/receivers/src/receivers/monitoring/check_gps_receiver.py --station ELDC
```

## 📝 Quick Commands

### Docker

```bash
# Start
docker-compose up -d

# Logs
docker-compose logs -f

# Stop
docker-compose down

# Execute command
docker exec gps-receivers-scheduler receivers <command>
```

### Server

The scheduler runs as a **user systemd unit owned by `gpsops`** — no `sudo` needed.
Run these as `gpsops` (e.g. `ssh gpsops@host '...'`):

```bash
# Start
systemctl --user start gps-receivers-scheduler

# Logs
journalctl --user-unit gps-receivers-scheduler -f

# Stop
systemctl --user stop gps-receivers-scheduler

# Restart (most common after a config change or git pull)
systemctl --user restart gps-receivers-scheduler

# Status
systemctl --user status gps-receivers-scheduler

# Execute one-off CLI command
receivers <command>
```

## 🆘 Support

- **Issues**: https://github.com/bennigo/receivers/issues
- **Documentation**: See README.md files in each deployment directory
- **Contact**: bgo@vedur.is

---

**Choose your deployment method above and follow the corresponding README for detailed instructions.**
