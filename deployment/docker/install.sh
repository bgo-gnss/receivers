#!/bin/bash
# GPS Receivers Scheduler - Docker Production Installation
# Simple script to deploy scheduler in Docker

set -e

echo "=== GPS Receivers Scheduler - Docker Installation ==="
echo ""

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Error: Docker is not installed"
    echo "   Install Docker: https://docs.docker.com/engine/install/"
    exit 1
fi

# Check if Docker Compose is available
if ! docker compose version &> /dev/null; then
    echo "❌ Error: Docker Compose is not available"
    echo "   Install Docker Compose: https://docs.docker.com/compose/install/"
    exit 1
fi

echo "✓ Docker found: $(docker --version)"
echo "✓ Docker Compose found: $(docker compose version)"
echo ""

# Create required host directories
echo "Step 1: Creating required directories..."
sudo mkdir -p /mnt/gpsdata \
    /var/cache/gps_receivers/logs \
    /var/cache/gps_receivers/tmp
sudo chown -R $USER:$USER /mnt/gpsdata /var/cache/gps_receivers
sudo chmod -R 777 /var/cache/gps_receivers  # Allow container user to write
echo "  ✓ Created /mnt/gpsdata"
echo "  ✓ Created /var/cache/gps_receivers/logs"
echo "  ✓ Created /var/cache/gps_receivers/tmp"
echo ""

# Check for local config
CONFIG_DIR="../../../gps-config-data"
if [[ ! -d "$CONFIG_DIR" ]]; then
    echo "⚠️  Warning: gps-config-data not found at $CONFIG_DIR"
    echo "   The container will attempt to clone from git.vedur.is"
    echo "   If git.vedur.is is unreachable, deployment will fail"
    echo ""
    read -p "Continue anyway? [y/N]: " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Installation cancelled"
        exit 1
    fi
else
    echo "✓ Found local gps-config-data"
fi
echo ""

# Build and start
echo "Step 2: Building Docker image..."
docker compose build

echo ""
echo "Step 3: Starting scheduler container..."
docker compose up -d

echo ""
echo "Step 4: Waiting for container to start..."
sleep 3

# Check if container is running
if docker ps | grep -q gps-receivers-scheduler; then
    echo "  ✓ Container is running"
else
    echo "  ✗ Container failed to start"
    echo ""
    echo "Check logs with:"
    echo "  docker logs gps-receivers-scheduler"
    exit 1
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Container: gps-receivers-scheduler"
echo "Status: Running"
echo ""
echo "Next steps:"
echo "  1. View logs:"
echo "     docker compose logs -f"
echo ""
echo "  2. Access container shell:"
echo "     docker exec -it gps-receivers-scheduler bash"
echo ""
echo "  3. Check scheduler status:"
echo "     docker exec gps-receivers-scheduler receivers scheduler status --show-jobs"
echo ""
echo "  4. Stop scheduler:"
echo "     docker compose down"
echo ""
echo "See deployment/docker/README.md for full documentation"
echo ""
