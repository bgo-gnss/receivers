# Development Workflow with Docker

## Overview

This document describes the **brilliant** development workflow where you edit code on your laptop with all your tools (Neovim, IDE, etc.), but the code runs inside a Docker container that mirrors the production server environment.

## The Magic: Live Code Updates Without Rebuilds

### How It Works

The Docker setup uses **volume mounts** to share code between your laptop and the container:

```yaml
# In deployment/docker-dev/docker-compose.yml
volumes:
  # Source code mounted from host
  - ../../src:/opt/receivers/src:ro
  - ../../pyproject.toml:/opt/receivers/pyproject.toml:ro

  # Sibling packages also mounted
  - ../../../gtimes:/opt/gtimes:ro
  - ../../../gps_parser:/opt/gps_parser:ro

  # Configuration mounted
  - ../../../gps-config-data:/opt/gps-config-data:ro
```

**What this means**:
1. Files are **not copied** into the container - they're **mounted** (shared)
2. When you edit a file on your laptop, the container sees the change immediately
3. Python packages installed with `-e` (editable mode) load from mounted directories
4. A simple `docker restart` reloads all Python modules

### Why This is Brilliant

✅ **Best of both worlds**:
- Edit with your favorite tools (Neovim, VSCode, etc.)
- Run in production-like environment (Ubuntu 24.04, system packages, network config)

✅ **Instant feedback loop**:
- Edit code → Save → Restart container (5 seconds) → Test
- No rebuild (which takes minutes)
- No image push/pull cycles

✅ **Git branch switching**:
- Switch branches on laptop → Restart container → Run different code
- Perfect for A/B testing features

✅ **Production parity**:
- Same Python version
- Same system dependencies
- Same network configuration (DNS, etc.)
- Same file paths

## Basic Workflow

### 1. Start the Development Container

```bash
cd /home/bgo/work/projects/gps/gpslibrary_new/receivers/deployment/docker-dev
docker-compose up -d
```

This starts the container with:
- Scheduler running automatically
- All 173 stations loaded
- Logs streaming to `/var/cache/gps_receivers/logs/`

### 2. Edit Code on Your Laptop

```bash
# Use your preferred editor
nvim src/receivers/scheduling/bulk_scheduler.py

# Or any IDE
code src/receivers/scheduling/
```

Edit Python files normally. Changes are **immediately visible** inside the container.

### 3. Restart to Apply Changes

```bash
# Simple restart (takes 5-10 seconds)
docker restart gps-receivers-scheduler-dev

# Container restarts with your new code!
```

**Why restart?** Python modules are cached in memory. Restart clears the cache and reloads all imports.

### 4. Monitor Results

```bash
# Watch logs live
docker logs -f gps-receivers-scheduler-dev

# Or check specific logs inside container
docker exec gps-receivers-scheduler-dev tail -f /var/cache/gps_receivers/logs/scheduler.log
```

### 5. Repeat!

```
Edit → Save → Restart → Test → Repeat
```

## Advanced Workflows

### Testing Configuration Changes

Configuration files are also mounted, so changes apply immediately:

```bash
# Edit configuration
vim /home/bgo/work/projects/gps/gpslibrary_new/gps-config-data/scheduler.yaml

# Restart to apply
docker restart gps-receivers-scheduler-dev
```

**Note**: The entrypoint script automatically removes `scheduler.db` on restart, forcing configuration reload from YAML.

### Git Branch Testing

See [git-branch-testing.md](git-branch-testing.md) for detailed workflow on testing different branches.

Quick version:
```bash
# Switch to feature branch
git checkout feature/new-timeout-logic

# Restart container
docker restart gps-receivers-scheduler-dev

# Container now runs feature branch code!
```

### Testing Multiple Packages

Changes to sibling packages (`gtimes`, `gps_parser`) are also live:

```bash
# Edit gtimes
vim /home/bgo/work/projects/gps/gpslibrary_new/gtimes/src/gtimes/timefunc.py

# Restart receivers container
docker restart gps-receivers-scheduler-dev

# Container now uses updated gtimes code!
```

### Debugging Inside Container

Sometimes you need to inspect state inside the container:

```bash
# Enter container shell
docker exec -it gps-receivers-scheduler-dev bash

# Once inside, you can:
python3                                    # Interactive Python with your code
receivers scheduler status --show-jobs     # Check scheduler state
ls -la /mnt/gpsdata/                      # Check downloaded files
cat /etc/gpsconfig/scheduler.yaml          # Verify configuration
ps aux | grep receivers                    # Check running processes

# Exit container
exit
```

### Running One-off Commands

Test manual downloads without affecting the running scheduler:

```bash
# Run manual download (scheduler keeps running)
docker exec gps-receivers-scheduler-dev \
  receivers download ELDC -D 1 --session 1Hz_1hr --sync --archive -v
```

## When to Restart vs Rebuild

### Just Restart (99% of the time)

Restart when you change:
- ✅ Python source code (`.py` files)
- ✅ Configuration files (`.yaml`, `.cfg`)
- ✅ pyproject.toml (dependencies already installed)
- ✅ Git branches

```bash
docker restart gps-receivers-scheduler-dev
```

### Rebuild (Rare)

Rebuild only when you:
- ❌ Add new pip package dependencies (not previously installed)
- ❌ Change Dockerfile (system packages, user setup)
- ❌ Change install.sh or entrypoint.sh scripts

```bash
cd deployment/docker-dev
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## Container Management

### Starting

```bash
cd deployment/docker-dev
docker-compose up -d              # Start in background
docker-compose up                 # Start with logs visible
```

### Stopping

```bash
# Stop but keep container (can restart fast)
docker-compose stop

# Stop and remove container (clean shutdown)
docker-compose down

# Stop with timeout (give downloads time to finish)
docker-compose down --timeout 60
```

### Restarting

```bash
# Quick restart (keeps container, reloads Python)
docker-compose restart
docker restart gps-receivers-scheduler-dev  # Direct command

# Full restart (stops + starts)
docker-compose down && docker-compose up -d
```

### Checking Status

```bash
# Container status
docker-compose ps
docker ps | grep gps-receivers

# Is it running?
docker inspect gps-receivers-scheduler-dev | jq '.[0].State.Status'

# Resource usage
docker stats gps-receivers-scheduler-dev

# Health check
docker inspect gps-receivers-scheduler-dev | jq '.[0].State.Health'
```

## Persistence

### What Survives Restarts

✅ **Persists across restarts**:
- Downloaded GPS data (`/mnt/gpsdata/`)
- Logs (`/var/cache/gps_receivers/logs/`)
- Scheduler database (SQLite) - **but deleted on restart by design**

❌ **Cleared on restart**:
- Python module cache (good! picks up code changes)
- Scheduler database (good! picks up config changes)
- Temporary files (`/var/cache/gps_receivers/tmp/`)

### What Survives System Reboots

The container has `restart: unless-stopped` in docker-compose.yml:

```yaml
restart: unless-stopped
```

This means:
- ✅ Container auto-starts after system reboot
- ✅ Container restarts if it crashes
- ❌ Container **does not** restart if you manually stopped it

Test it:
```bash
# Start container
docker-compose up -d

# Reboot system
sudo reboot

# After reboot - container is automatically running!
docker ps | grep gps-receivers
```

To prevent auto-start:
```bash
# Stop with docker-compose (sets "unless-stopped" state)
docker-compose down

# Now it won't auto-start on reboot
```

## Troubleshooting

### Changes Not Showing Up

**Problem**: Edited code but behavior didn't change

**Solutions**:
1. Did you save the file? (Check file timestamp)
2. Did you restart the container?
   ```bash
   docker restart gps-receivers-scheduler-dev
   ```
3. Are you editing the right file? (Check mount paths)
   ```bash
   # Verify mounted path
   docker exec gps-receivers-scheduler-dev ls -la /opt/receivers/src/receivers/scheduling/
   ```

### Container Won't Start After Code Change

**Problem**: Container exits immediately after restart

**Likely cause**: Syntax error in Python code

**Solutions**:
```bash
# Check logs for error
docker logs --tail=50 gps-receivers-scheduler-dev

# Likely see Python traceback with syntax error
# Fix the error, then try again
```

### Import Errors After Branch Switch

**Problem**: `ModuleNotFoundError` or `ImportError` after switching branches

**Likely cause**: New branch has different dependencies

**Solutions**:
```bash
# Check if new dependencies needed
cat pyproject.toml | grep dependencies

# If yes, rebuild container
cd deployment/docker-dev
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

### Permission Errors

**Problem**: Container can't write to `/mnt/gpsdata/` or `/var/cache/`

**Solutions**:
```bash
# Check host directory permissions
ls -la /mnt/gpsdata /var/cache/gps_receivers

# Fix ownership (run on host)
sudo chown -R $USER:$USER /mnt/gpsdata /var/cache/gps_receivers

# Restart container
docker restart gps-receivers-scheduler-dev
```

### Container Survived Reboot (Unexpected)

**Problem**: Thought you stopped it, but it's running after reboot

**Explanation**: `restart: unless-stopped` policy

**To really stop it**:
```bash
# Method 1: docker-compose down
cd deployment/docker-dev
docker-compose down

# Method 2: Update restart policy
docker update --restart=no gps-receivers-scheduler-dev
docker stop gps-receivers-scheduler-dev
```

## Best Practices

### 1. Use Git Branches for Features

```bash
# Don't edit main directly
git checkout -b feature/my-improvement

# Edit and test
vim src/receivers/...
docker restart gps-receivers-scheduler-dev

# When working, commit
git commit -am "feat: my improvement"

# Merge to main when tested
git checkout main
git merge feature/my-improvement
```

### 2. Test Before Committing

```bash
# Edit code
vim src/receivers/scheduling/bulk_scheduler.py

# Test in Docker
docker restart gps-receivers-scheduler-dev
docker logs -f gps-receivers-scheduler-dev

# If works, commit
git commit -am "fix: monitoring improvements"
```

### 3. Keep Container Running

The container is lightweight - no need to stop/start constantly:
- Let it run 24/7
- Just restart when you change code
- Monitor with `docker logs -f`

### 4. Use Stash for Quick Experiments

```bash
# Save current work
git stash

# Test something else
git checkout other-branch
docker restart gps-receivers-scheduler-dev

# Restore work
git checkout my-branch
git stash pop
docker restart gps-receivers-scheduler-dev
```

## Summary

**The workflow**:
1. ✅ Start container once: `docker-compose up -d`
2. ✅ Edit code on laptop with your tools
3. ✅ Restart container: `docker restart gps-receivers-scheduler-dev`
4. ✅ Test and iterate
5. ✅ Commit when satisfied

**Key insight**: Source code is **shared**, not **copied**. Changes on host = changes in container.

**When to rebuild**: Almost never (only for dependency/Dockerfile changes).

---

**See Also**:
- [Git Branch Testing Workflow](git-branch-testing.md)
- [Docker Development Setup](../../deployment/docker-dev/README.md)
- [Troubleshooting Guide](troubleshooting.md) (TODO)
