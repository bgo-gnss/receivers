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
sudo git clone https://github.com/bennigo/receivers.git /opt/receivers
cd /opt/receivers
sudo ./deployment/server/install.sh
sudo systemctl start gps-receivers-scheduler
```

✅ **Advantages**:
- Systemd service integration
- Automatic startup on boot
- System-level logging (journalctl)
- Native performance

📖 **Full guide**: [deployment/server/README.md](server/README.md)

---

## 📊 Feature Comparison

| Feature | Docker | Server |
|---------|--------|--------|
| **Installation** | docker-compose up | Run install.sh |
| **Service Management** | docker-compose | systemd |
| **Logs** | docker logs | journalctl |
| **Auto-start** | restart: unless-stopped | systemctl enable |
| **Updates** | Rebuild image | git pull + restart |
| **Isolation** | ✅ Containerized | ❌ System-wide |
| **Resource Limits** | ✅ Built-in | Manual (systemd) |
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

```bash
# Start
sudo systemctl start gps-receivers-scheduler

# Logs
sudo journalctl -u gps-receivers-scheduler -f

# Stop
sudo systemctl stop gps-receivers-scheduler

# Execute command
receivers <command>
```

## 🆘 Support

- **Issues**: https://github.com/bennigo/receivers/issues
- **Documentation**: See README.md files in each deployment directory
- **Contact**: bgo@vedur.is

---

**Choose your deployment method above and follow the corresponding README for detailed instructions.**
