#!/usr/bin/env bash
# sync-config.sh — pull gps-config-data from git and deploy safe config files
#
# Runs as bgo (owns the repo; bgo is in the gpsops group so it can write the
# config dir). Invoked by gps-config-sync.timer every 10 minutes.
#
# Safety guarantees:
#   - network failure on git fetch → exit silently, local config unchanged
#   - pull failure (diverged branch) → logged, no files touched
#   - database.cfg is NEVER synced — it contains server-local credentials
#   - cmp before cp — only touches files that actually changed (avoids
#     spurious mtime bumps that would trigger unnecessary scheduler reloads)

set -euo pipefail

REPO=/home/bgo/git/gps-config-data
CONFIG_DIR=/home/gpsops/.config/gpsconfig

# Fail silently if git server unreachable — local config stays in effect
git -C "$REPO" fetch --quiet 2>/dev/null || exit 0

LOCAL=$(git -C "$REPO" rev-parse HEAD)
REMOTE=$(git -C "$REPO" rev-parse '@{u}' 2>/dev/null) || exit 0

# Nothing new upstream
[ "$LOCAL" = "$REMOTE" ] && exit 0

# Fast-forward only — never merge; if diverged, log and bail without touching configs
if ! git -C "$REPO" pull --ff-only --quiet; then
    logger -t gps-config-sync \
        "ERROR: pull failed (repo may have diverged) — skipping sync, local config unchanged"
    exit 0
fi

NEW_REV=$(git -C "$REPO" rev-parse --short HEAD)

# Files safe to auto-sync.
# database.cfg is intentionally absent — it holds server-local DB credentials.
SYNC_FILES=(stations.cfg receivers.cfg scheduler.yaml icinga.cfg)

CHANGED=()
for f in "${SYNC_FILES[@]}"; do
    src="$REPO/$f"
    dst="$CONFIG_DIR/$f"
    [ -f "$src" ] || continue
    # Only copy when content differs — preserves mtime on unchanged files so
    # the scheduler's config watcher does not reload unnecessarily
    if ! cmp -s "$src" "$dst" 2>/dev/null; then
        cp "$src" "$dst"
        CHANGED+=("$f")
    fi
done

if [ ${#CHANGED[@]} -gt 0 ]; then
    logger -t gps-config-sync "Synced to $NEW_REV: ${CHANGED[*]}"
fi
