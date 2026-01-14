#!/bin/bash
# Live monitoring of concurrent downloads

watch -n 5 'echo "=== GPS Downloads Monitor - $(date +"%H:%M:%S") ==="; \
echo ""; \
echo "Currently downloading:"; \
docker logs --tail 100 gps-receivers-scheduler-dev 2>&1 | \
  grep "Downloading.*\.gz:" | tail -10 | \
  sed -n "s/.*Downloading \([A-Z0-9]\{4\}\)[0-9].*\.gz:.*\([0-9]\+%\).*\([0-9.]\+[kMG]B\/s\).*/  \1: \2 @ \3/p" | \
  sort -u; \
echo ""; \
echo "Unique stations downloading:"; \
docker logs --tail 100 gps-receivers-scheduler-dev 2>&1 | \
  grep "Downloading.*\.gz:" | \
  grep -oP "[A-Z0-9]{4}(?=\d{7})" | \
  sort -u | wc -l | awk "{print \"  \" \$1 \" concurrent stations\"}"; \
echo ""; \
echo "Last 3 completions:"; \
docker logs --tail 50 gps-receivers-scheduler-dev 2>&1 | \
  grep "Completed:" | tail -3 | \
  sed "s/.*Completed: \([A-Z0-9]\+\) (\([^)]\+\)) - \([0-9]\+\) files in \([0-9.]\+s\)/  ✓ \1 (\2) - \3 files in \4/"'
