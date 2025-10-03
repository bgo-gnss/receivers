# GPS Receivers Scheduler - Docker Production Deployment

Production Docker deployment for GPS receivers scheduler with automatic configuration from git.vedur.is.

## Quick Start

```bash
# From the receivers repository root
cd deployment/docker

# Build and start
docker-compose up -d

# View logs
docker-compose logs -f

# Check status
docker-compose ps
docker exec gps-receivers-scheduler receivers scheduler status --show-jobs

# Stop
docker-compose down
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

## Maintenance

### Update Scheduler Code

```bash
cd /path/to/receivers
git pull
cd deployment/docker
docker-compose build --no-cache
docker-compose up -d
```

### Update Configuration

```bash
docker exec gps-receivers-scheduler bash -c 'cd /opt/gps-config-data && git pull'
docker-compose restart
```

### Clean Up Old Data

```bash
# Clean old logs (older than 30 days)
docker exec gps-receivers-scheduler find /var/cache/gps_receivers/logs -type f -mtime +30 -delete

# Check disk usage
docker exec gps-receivers-scheduler df -h /mnt/gpsdata
```
