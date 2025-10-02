# Installation Testing

This directory contains tools for testing the GPS Receivers Scheduler installation in a clean Ubuntu environment using Docker.

## Quick Start

```bash
# Ensure docker group is active (if you just installed Docker)
newgrp docker

# Run the test environment
./deployment/test/test-install.sh
```

This will:
1. Build a Docker image with fresh Ubuntu 24.04
2. Mount your local repositories as read-only sources
3. Copy sources to writable locations (simulating git clone)
4. Start an interactive shell in the container

## Testing the Installation

Once inside the container:

```bash
# Run the installation script
cd /opt/receivers
./deployment/scripts/install.sh
```

## Test Scenarios

### 1. Full Installation Test

```bash
# Inside the container
cd /opt/receivers
./deployment/scripts/install.sh

# Expected outcome:
# - System user 'gpsops' created
# - Directory structure created
# - Python venv created at /opt/receivers/venv
# - Packages installed (gtimes, gps_parser, receivers)
# - gps-config-data cloned
# - Configuration deployed to /etc/gpsconfig
# - Systemd service installed
```

### 2. Verification After Installation

```bash
# Check installed commands
/opt/receivers/venv/bin/receivers --version
/opt/receivers/venv/bin/gps-config --help

# Check deployed configuration
ls -la /etc/gpsconfig/

# Check created directories
ls -la /var/cache/gps_receivers/
ls -la /mnt/gpsdata/

# Check systemd service (may not work in container without systemd init)
cat /etc/systemd/system/gps-receivers-scheduler.service
```

### 3. Test Configuration Deployment

```bash
# Check what environment was detected
hostname

# Check deployed configuration
cat /etc/gpsconfig/scheduler.yaml
cat /etc/gpsconfig/receivers.cfg

# Verify templates were rendered correctly
grep -v "{{" /etc/gpsconfig/*.yaml /etc/gpsconfig/*.cfg
# Should return no matches (no unrendered templates)
```

## Testing Different Scenarios

### Test as Different Hostname (Environment Detection)

The test script automatically simulates a production environment. To test environment-specific configuration:

```bash
# Inside the container, check detected environment:
hostname

# The gps-config deploy command will auto-detect environment
# based on hostname when you run the install script

# To manually test environment detection:
/opt/receivers/venv/bin/gps-config deploy \
    --config-dir /opt/gps-config-data \
    --verbose
```

### Test Without gps-config-data Access

```bash
# Simulate network failure or repository access issues
docker run --rm -it \
    --privileged \
    --network none \
    -v $(pwd):/opt/receivers:ro \
    gps-receivers-test \
    /bin/bash

# Installation should fail gracefully at Step 9 (git clone)
```

### Test Idempotency (Re-run Installation)

```bash
# Run installation twice to verify it handles existing installations
cd /opt/receivers
./deployment/scripts/install.sh  # First run
./deployment/scripts/install.sh  # Second run - should handle existing user/dirs
```

## Troubleshooting

### Docker Not Installed

```bash
# Install Docker on Ubuntu
sudo apt-get update
sudo apt-get install docker.io
sudo usermod -aG docker $USER
# Log out and back in
```

### Permission Denied

```bash
# Make sure test script is executable
chmod +x deployment/test/test-install.sh
```

### systemd Not Working in Container

This is expected. The container uses a minimal Ubuntu without systemd init. The installation script will still complete, but `systemctl` commands won't work. On a real server with systemd, these commands will work properly.

## Cleanup

```bash
# Exit the container (it's automatically removed with --rm flag)
exit

# Remove the Docker image (optional)
docker rmi gps-receivers-test

# Remove any stopped containers
docker container prune
```

## Advanced Testing

### Debug Mode

```bash
# Run installation with bash debug tracing
cd /opt/receivers
bash -x ./deployment/scripts/install.sh
```

### Partial Installation Testing

```bash
# Test only specific steps by commenting out sections in install.sh
# Or manually run each step:

# Step 1-3: User and directories
useradd --system --home-dir /opt/receivers --shell /bin/bash gpsops
mkdir -p /opt/receivers /etc/gpsconfig /var/cache/gps_receivers /mnt/gpsdata
chown -R gpsops:gpsops /opt/receivers /var/cache/gps_receivers /mnt/gpsdata

# Step 7-8: Python venv and packages
sudo -u gpsops python3 -m venv /opt/receivers/venv
sudo -u gpsops /opt/receivers/venv/bin/pip install gtimes
# ... etc
```

## CI/CD Integration (Future)

This test setup can be integrated into CI/CD pipelines:

```yaml
# Example GitHub Actions workflow
name: Test Installation
on: [push, pull_request]
jobs:
  test-install:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Test installation
        run: |
          cd deployment/test
          ./test-install.sh
```

---

**Created**: 2025-10-02
**Purpose**: Validate installation process before production deployment
**Status**: Ready for testing
