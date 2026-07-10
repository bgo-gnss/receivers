# Git Branch Testing in Docker

## Overview

One of the most powerful features of the development Docker setup is the ability to test different git branches by simply switching branches and restarting the container.

This document covers workflows for testing features, comparing branches, and safely experimenting with code.

## Basic Branch Testing

### Test a Feature Branch

```bash
cd /home/bgo/work/projects/gpslibrary/receivers

# Check current branch
git branch --show-current  # main

# Create and switch to feature branch
git checkout -b feature/adaptive-timeout

# Make changes
vim src/receivers/scheduling/bulk_scheduler.py

# Restart Docker to test
docker restart gps-receivers-scheduler-dev

# Monitor results
docker logs -f gps-receivers-scheduler-dev | grep "⏱️"
```

### Quick Rollback

If the feature branch has issues:

```bash
# Immediately rollback to main
git checkout main

# Restart with main branch code
docker restart gps-receivers-scheduler-dev

# System back to known-good state!
```

## Comparing Branches

### A/B Testing Two Approaches

Test two different implementations:

```bash
# Test approach A
git checkout feature/approach-a
docker restart gps-receivers-scheduler-dev
# ... let it run, collect metrics ...

# Test approach B
git checkout feature/approach-b
docker restart gps-receivers-scheduler-dev
# ... compare results ...

# Choose winner
git checkout feature/approach-a  # Or approach-b
```

### Bisecting to Find Bugs

Use git bisect with Docker testing:

```bash
# Start bisect
git bisect start
git bisect bad           # Current version has bug
git bisect good main     # Main version works

# Git checks out a middle commit
docker restart gps-receivers-scheduler-dev
# ... test if bug exists ...

# Mark result
git bisect good  # Or git bisect bad

# Repeat until git finds the problem commit
```

## Safe Experimentation

### Stash and Switch

Test something without losing current work:

```bash
# You're working on feature A
git status  # Shows uncommitted changes

# Need to quickly test feature B
git stash save "WIP: feature A progress"

# Switch to feature B
git checkout feature/feature-b
docker restart gps-receivers-scheduler-dev
# ... test feature B ...

# Return to feature A
git checkout feature/feature-a
git stash pop
docker restart gps-receivers-scheduler-dev
```

### Testing Pull Requests

Test someone else's PR:

```bash
# Fetch PR branch
git fetch origin pull/123/head:pr-123

# Test it
git checkout pr-123
docker restart gps-receivers-scheduler-dev
# ... evaluate PR ...

# Return to your work
git checkout main
docker restart gps-receivers-scheduler-dev
```

## Advanced Workflows

### Testing Dependent Branches

Test changes across multiple packages (receivers, gtimes, gps_parser):

```bash
# All three repos have feature branches
cd /home/bgo/work/projects/gpslibrary

# Switch all to feature branches
cd receivers && git checkout feature/new-logic
cd ../gtimes && git checkout feature/new-time-func
cd ../gps_parser && git checkout feature/new-config

# Restart container (picks up all three changes!)
docker restart gps-receivers-scheduler-dev

# Test integrated changes
docker logs -f gps-receivers-scheduler-dev
```

Return all to main:
```bash
cd receivers && git checkout main
cd ../gtimes && git checkout main
cd ../gps_parser && git checkout main
docker restart gps-receivers-scheduler-dev
```

### Cherry-Picking for Testing

Test specific commits without full branch:

```bash
# On main branch, want to test one commit from feature branch
git checkout main
git cherry-pick abc123def  # Specific commit

# Test the change
docker restart gps-receivers-scheduler-dev

# If not suitable, undo
git reset --hard HEAD~1
docker restart gps-receivers-scheduler-dev
```

### Testing Release Tags

Test specific versions:

```bash
# List tags
git tag -l

# Checkout specific version
git checkout v1.2.3
docker restart gps-receivers-scheduler-dev

# Return to main
git checkout main
docker restart gps-receivers-scheduler-dev
```

## Troubleshooting Branch Switches

### Uncommitted Changes

**Problem**: Can't switch branches due to uncommitted changes

```bash
error: Your local changes to the following files would be overwritten by checkout:
	src/receivers/scheduling/bulk_scheduler.py
Please commit your changes or stash them before you switch branches.
```

**Solutions**:

```bash
# Option 1: Stash changes
git stash save "WIP: description"
git checkout other-branch
# ... later ...
git checkout original-branch
git stash pop

# Option 2: Commit changes
git add -A
git commit -m "WIP: work in progress"
git checkout other-branch
# ... later ...
git checkout original-branch
git reset HEAD~1  # Undo WIP commit if needed

# Option 3: Force checkout (loses changes!)
git checkout -f other-branch  # ⚠️ Discards uncommitted changes!
```

### Import Errors After Switch

**Problem**: Container crashes with `ModuleNotFoundError` after branch switch

**Cause**: New branch has different dependencies

**Solution**:

```bash
# Check if dependencies changed
git diff main...feature-branch -- pyproject.toml

# If yes, rebuild container
cd deployment/docker-dev
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

### File Conflicts After Switch

**Problem**: Branch switch fails due to conflicts

```bash
error: The following untracked working tree files would be overwritten by checkout:
	src/receivers/new_file.py
```

**Solutions**:

```bash
# Option 1: Remove untracked files
git clean -fd  # ⚠️ Deletes untracked files!

# Option 2: Stash including untracked
git stash -u

# Option 3: Add and commit
git add src/receivers/new_file.py
git commit -m "Add new file"
```

### Database State Issues

**Problem**: Scheduler behaves oddly after branch switch

**Cause**: Different branch expects different database schema/state

**Solution**: Container already handles this! The entrypoint script removes `scheduler.db` on every restart, so database is always fresh.

```bash
# Just restart - database auto-recreated
docker restart gps-receivers-scheduler-dev

# If still issues, check logs
docker logs --tail=50 gps-receivers-scheduler-dev
```

## Best Practices

### 1. Always Check Current Branch

Before making changes:

```bash
# What branch am I on?
git branch --show-current

# Or with more context
git status
```

### 2. Use Descriptive Branch Names

```bash
# Good names
git checkout -b feature/adaptive-timeout-calculation
git checkout -b fix/dns-resolution-issue
git checkout -b test/compare-scheduling-strategies

# Less clear
git checkout -b new-stuff
git checkout -b test
git checkout -b wip
```

### 3. Clean Up After Testing

```bash
# Delete local test branches
git branch -d feature/tested-and-merged

# Delete remote test branches
git push origin --delete feature/temporary-test

# Prune deleted remote branches
git fetch --prune
```

### 4. Document Branch Purpose

```bash
# First commit on branch explains purpose
git checkout -b feature/adaptive-timeout
git commit --allow-empty -m "feat: adaptive timeout calculation

Purpose: Dynamically adjust timeout based on file count
Testing: Run with lookback_periods=7 and monitor warnings"
```

### 5. Test on Main Before Merging

```bash
# On feature branch, merge main first
git checkout feature/my-feature
git merge main

# Resolve any conflicts
# Test with Docker
docker restart gps-receivers-scheduler-dev

# If works, merge to main
git checkout main
git merge feature/my-feature
```

## Workflows by Scenario

### Scenario 1: Developing a New Feature

```bash
# 1. Create feature branch from main
git checkout main
git pull
git checkout -b feature/new-monitoring

# 2. Develop with iterations
vim src/receivers/...
docker restart gps-receivers-scheduler-dev
# ... test ...
vim src/receivers/...  # Refine
docker restart gps-receivers-scheduler-dev
# ... test again ...

# 3. Commit when satisfied
git add src/receivers/...
git commit -m "feat: add advanced monitoring"

# 4. Test once more
docker restart gps-receivers-scheduler-dev

# 5. Merge to main
git checkout main
git merge feature/new-monitoring

# 6. Test main
docker restart gps-receivers-scheduler-dev
```

### Scenario 2: Investigating a Bug

```bash
# 1. Reproduce on main
git checkout main
docker restart gps-receivers-scheduler-dev
# ... confirm bug exists ...

# 2. Create fix branch
git checkout -b fix/timeout-calculation

# 3. Make targeted fix
vim src/receivers/scheduling/bulk_scheduler.py
docker restart gps-receivers-scheduler-dev
# ... verify fix ...

# 4. Test doesn't break anything else
docker logs -f gps-receivers-scheduler-dev
# ... let run for a while ...

# 5. Commit and merge
git commit -am "fix: correct timeout calculation for hourly sessions"
git checkout main
git merge fix/timeout-calculation
```

### Scenario 3: Code Review

```bash
# 1. Fetch colleague's branch
git fetch origin
git checkout -b review/colleague-feature origin/colleague-feature

# 2. Test their code
docker restart gps-receivers-scheduler-dev
docker logs -f gps-receivers-scheduler-dev

# 3. Leave feedback
# ... add comments on GitHub/GitLab ...

# 4. Return to your work
git checkout main
docker restart gps-receivers-scheduler-dev
```

### Scenario 4: Emergency Rollback

Production has an issue:

```bash
# 1. Identify last good version
git log --oneline
git checkout abc123  # Last good commit

# 2. Test it
docker restart gps-receivers-scheduler-dev
# ... verify issue gone ...

# 3. If good, tag as hotfix
git tag -a hotfix-v1.2.4 -m "Rollback timeout changes"
git push origin hotfix-v1.2.4

# 4. Create branch from good version
git checkout -b hotfix/revert-timeout-changes abc123
git push origin hotfix/revert-timeout-changes

# 5. Fix underlying issue on separate branch
git checkout -b fix/timeout-root-cause main
# ... proper fix ...
```

## Summary

**Key advantages**:
- ✅ Test branches without separate environments
- ✅ Instant rollback with `git checkout`
- ✅ A/B test different approaches
- ✅ Safe experimentation with stash
- ✅ Test PRs before merging

**Remember**:
- Always `docker restart` after `git checkout`
- Stash or commit before switching branches
- Clean up test branches when done
- Test on main before deploying

**The workflow**:
```
git checkout feature → docker restart → test → git checkout main
```

---

**See Also**:
- [Docker Workflow](docker-workflow.md)
- [Docker Development Setup](../../deployment/docker-dev/README.md)
