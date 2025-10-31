# GPS Receivers Scheduler - Docker Development Setup

**🔧 DEVELOPMENT DOCKER SETUP** - Live code updates without rebuilds!

This setup uses volume mounts to share source code between your laptop and the container.
Changes to Python files are instantly available after a simple container restart.

**Key Features**:
- ✅ Edit code on laptop, run in production-like environment
- ✅ Git branch switching - test branches by switching and restarting
- ✅ No rebuild needed for code changes
- ✅ Production parity (same OS, packages, network config)

**📚 See [Development Workflow](../../docs/development/docker-workflow.md) for full documentation.**

**For production deployment**, see [../docker-prod/](../docker-prod/) (future).

---

## Quick Start

```bash
# From the receivers repository root
cd deployment/docker

# Run installation script (recommended)
./install.sh

# Or manually:
docker compose build
docker compose up -d
```

## Installation Script

The `install.sh` script automates the complete deployment:

```bash
cd deployment/docker
./install.sh
```

**What it does:**
1. Verifies Docker and Docker Compose are installed
2. Creates required host directories (`/mnt/gpsdata`, `/var/cache/gps_receivers`)
3. Builds the Docker image with all dependencies
4. Starts the scheduler container
5. Verifies the container is running

## Accessing the Container

### Interactive Shell

Access the container to run commands or monitor activities:

```bash
# Access container shell (interactive mode)
docker exec -it gps-receivers-scheduler bash

# Once inside, you can run:
receivers scheduler status --show-jobs    # Check scheduler status
receivers health ELDC --json              # Check station health
ls -la /mnt/gpsdata/                      # List downloaded files
tail -f /var/cache/gps_receivers/logs/scheduler.log  # Follow logs

# Debug and inspection commands:
ps aux | grep receivers                   # Check if scheduler is running
cat /etc/gpsconfig/scheduler.yaml         # View configuration
sqlite3 /var/cache/gps_receivers/scheduler.db ".tables"  # Inspect database
receivers download ELDC --test-connection # Test station connectivity
which python3                             # Check Python location
pip list | grep receivers                 # Check installed packages

# The gpsops user has sudo privileges if needed:
sudo systemctl status something
sudo apt update
```

### Run Commands Without Shell

Execute commands directly without entering the container:

```bash
# Check scheduler status
docker exec gps-receivers-scheduler receivers scheduler status --show-jobs

# Check specific station health
docker exec gps-receivers-scheduler receivers health THOB --json

# List downloaded files
docker exec gps-receivers-scheduler ls -lh /mnt/gpsdata/

# View logs
docker exec gps-receivers-scheduler tail -100 /var/cache/gps_receivers/logs/scheduler.log
```

## Monitoring

### View Live Logs

```bash
# All logs
docker compose logs -f

# Last 100 lines
docker compose logs --tail=100

# Scheduler logs only (inside container)
docker exec gps-receivers-scheduler tail -f /var/cache/gps_receivers/logs/scheduler.log

# Download audit log
docker exec gps-receivers-scheduler tail -f /var/cache/gps_receivers/logs/download_audit.jsonl
```

### Check Container Status

```bash
# Container status
docker compose ps

# Detailed container info
docker inspect gps-receivers-scheduler

# Resource usage
docker stats gps-receivers-scheduler

# Health check status
docker inspect gps-receivers-scheduler | jq '.[0].State.Health'
```

### Scheduler Status

```bash
# Show all scheduled jobs
docker exec gps-receivers-scheduler receivers scheduler status --show-jobs

# Count stations loaded
docker exec gps-receivers-scheduler receivers scheduler status | grep "Loaded.*stations"
```

### Data Verification

```bash
# Check downloaded data
docker exec gps-receivers-scheduler ls -lh /mnt/gpsdata/

# Check today's downloads
docker exec gps-receivers-scheduler ls -lh /mnt/gpsdata/$(date +%Y)/$(date +%b | tr '[:upper:]' '[:lower:]')/

# Check specific station
docker exec gps-receivers-scheduler ls -lh /mnt/gpsdata/$(date +%Y)/$(date +%b | tr '[:upper:]' '[:lower:]')/ELDC/

# Count total files downloaded today
docker exec gps-receivers-scheduler find /mnt/gpsdata/$(date +%Y)/$(date +%b | tr '[:upper:]' '[:lower:]')/ -name "*.gz" | wc -l
```

## Container Management

### Start/Stop

```bash
# Start container
docker compose up -d

# Stop container
docker compose down

# Restart container (stops and starts scheduler)
docker compose restart

# Stop without removing container
docker compose stop

# Start after stop
docker compose start
```

### Stop/Restart Scheduler

You can now stop or restart the scheduler without restarting the entire container:

```bash
# Stop the scheduler gracefully (waits for active downloads)
docker exec gps-receivers-scheduler receivers scheduler stop

# Stop the scheduler immediately (force kill)
docker exec gps-receivers-scheduler receivers scheduler stop --force

# Restart the scheduler (reload configuration)
docker exec gps-receivers-scheduler receivers scheduler restart

# Restart with force stop
docker exec gps-receivers-scheduler receivers scheduler restart --force

# Restart with custom options
docker exec gps-receivers-scheduler receivers scheduler restart --max-workers 10 --verbose
```

**Note**: The `stop` and `restart` commands require `psutil`. If not installed, restart the container instead:

```bash
docker compose restart
```

### Update/Rebuild

```bash
# Pull latest code
cd /path/to/receivers
git pull

# Rebuild and restart
cd deployment/docker
docker compose build --no-cache
docker compose down
docker compose up -d
```

## Configuration

### Environment Variables

Edit `docker-compose.yml` to configure:

```yaml
environment:
  - GPS_CONFIG_REPO_URL=https://git.vedur.is/bgo/gps-config-data.git  # Config repo
  - GPS_ENVIRONMENT=production                                         # Environment name
  - TZ=Atlantic/Reykjavik                                             # Timezone
```

### Data Volumes

Data is stored in two main volumes:

- `/mnt/gpsdata` - Downloaded GPS data files
- `/var/cache/gps_receivers` - Logs and cache
  - `logs/scheduler.log` - Main scheduler log
  - `logs/download_audit.jsonl` - Download audit trail (if configured)
  - `scheduler.db` - APScheduler job database
  - `tmp/` - Temporary download files

Update volume paths in `docker-compose.yml`:

```yaml
volumes:
  gps-data:
    driver_opts:
      device: /mnt/gpsdata  # Change this path

  gps-cache:
    driver_opts:
      device: /var/cache/gps_receivers  # Change this path
```

## Commands

### Start Scheduler (All Stations)

```bash
docker-compose up -d
```

### Start with Specific Stations

```bash
docker-compose run --rm gps-scheduler \
  scheduler start --stations ELDC THOB ORFC --max-workers 3 --verbose
```

### Test Configuration

```bash
docker exec gps-receivers-scheduler \
  receivers scheduler test --verbose
```

### Manual Download

```bash
docker exec gps-receivers-scheduler \
  receivers download ELDC --sync --archive -v
```

### View Logs

```bash
# Live scheduler logs
docker-compose logs -f

# Inside container
docker exec gps-receivers-scheduler tail -f /var/cache/gps_receivers/logs/scheduler.log

# Download audit log
docker exec gps-receivers-scheduler tail -f /var/cache/gps_receivers/logs/download_audit.jsonl
```

## Monitoring

### Health Check

Docker includes automatic health checks:

```bash
docker inspect gps-receivers-scheduler | jq '.[0].State.Health'
```

### Scheduler Status

```bash
docker exec gps-receivers-scheduler receivers scheduler status --show-jobs
```

### Check Downloaded Data

```bash
docker exec gps-receivers-scheduler ls -lh /mnt/gpsdata/
```

## Troubleshooting

### Container Won't Start

```bash
# Check logs
docker-compose logs gps-scheduler

# Check if config repo is accessible
docker-compose run --rm gps-scheduler git ls-remote https://git.vedur.is/bgo/gps-config-data.git
```

### Configuration Issues

```bash
# Check deployed configuration
docker exec gps-receivers-scheduler ls -la /etc/gpsconfig/

# Manually update config
docker exec gps-receivers-scheduler bash -c 'cd /opt/gps-config-data && git pull'
```

### Network Issues (git.vedur.is unreachable)

Use local config mount:

```yaml
volumes:
  - ../../gps-config-data:/opt/gps-config-data:ro
```

Then rebuild:

```bash
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## Production Deployment

### At IMO (git.vedur.is accessible)

```bash
cd deployment/docker
docker-compose up -d
```

### External (no git.vedur.is access)

1. Clone config locally first:
```bash
git clone https://git.vedur.is/bgo/gps-config-data.git ../../gps-config-data
```

2. Uncomment local mount in `docker-compose.yml`:
```yaml
volumes:
  - ../../gps-config-data:/opt/gps-config-data:ro
```

3. Start:
```bash
docker-compose up -d
```

## Resource Management

Adjust resources in `docker-compose.yml`:

```yaml
deploy:
  resources:
    limits:
      cpus: '4.0'       # Max CPUs
      memory: 2G        # Max memory
    reservations:
      cpus: '1.0'       # Reserved CPUs
      memory: 512M      # Reserved memory
```

## Troubleshooting

### Container Won't Start

```bash
# Check logs
docker compose logs gps-scheduler

# Check if ports are in use (if not using host networking)
sudo netstat -tulpn | grep LISTEN

# Check if volumes exist
ls -la /mnt/gpsdata /var/cache/gps_receivers
```

### No Stations Loaded

```bash
# Check if config files exist
docker exec gps-receivers-scheduler ls -la /etc/gpsconfig/

# Verify GPS_CONFIG_PATH
docker exec gps-receivers-scheduler env | grep GPS

# Check configuration
docker exec gps-receivers-scheduler cat /etc/gpsconfig/stations.cfg | head -20
```

### Downloads Not Working

```bash
# Check scheduler is running
docker exec gps-receivers-scheduler receivers scheduler status

# Test single station download
docker exec gps-receivers-scheduler receivers download ELDC -D 1 --session 1Hz_1hr --test-connection -v

# Check network connectivity from container
docker exec gps-receivers-scheduler ping -c 3 8.8.8.8
```

### Container Keeps Restarting

```bash
# Check last 50 log lines
docker logs --tail=50 gps-receivers-scheduler

# Check exit code
docker inspect gps-receivers-scheduler | jq '.[0].State.ExitCode'

# Check health status
docker inspect gps-receivers-scheduler | jq '.[0].State.Health.Status'
```

### Permission Issues

```bash
# Fix host directory permissions
sudo chown -R $USER:$USER /mnt/gpsdata /var/cache/gps_receivers

# Verify container user
docker exec gps-receivers-scheduler whoami  # Should be gpsops

# Check directory ownership inside container
docker exec gps-receivers-scheduler ls -la /mnt/gpsdata /var/cache/gps_receivers
```

## Maintenance

### Update Scheduler Code

```bash
cd /path/to/receivers
git pull
cd deployment/docker
docker compose build --no-cache
docker compose down
docker compose up -d
```

### Update Configuration

```bash
# If using mounted config
cd /path/to/gps-config-data
git pull
docker compose restart

# If config is inside container
docker exec gps-receivers-scheduler bash -c 'cd /opt/gps-config-data && git pull'
docker compose restart
```

### Clean Up Old Data

```bash
# Clean old logs (older than 30 days)
docker exec gps-receivers-scheduler find /var/cache/gps_receivers/logs -type f -mtime +30 -delete

# Check disk usage
docker exec gps-receivers-scheduler df -h /mnt/gpsdata

# Remove stopped containers and unused images
docker system prune -a
```

### Backup

```bash
# Backup downloaded data
sudo rsync -av /mnt/gpsdata/ /backup/gpsdata/

# Backup logs
sudo rsync -av /var/cache/gps_receivers/logs/ /backup/gps_logs/

# Backup configuration
docker exec gps-receivers-scheduler tar czf /tmp/config-backup.tar.gz /etc/gpsconfig
docker cp gps-receivers-scheduler:/tmp/config-backup.tar.gz ./config-backup.tar.gz
```
