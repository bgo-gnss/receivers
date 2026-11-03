#!/usr/bin/env bash
# push-ntrcaster-sourcetable.sh — deliver the NTRIP sourcetable to ntrcaster.vedur.is
#
# Runs on rek-d01 as gpsops (user timer gps-ntrcaster-sourcetable.timer, every
# 10 min). ntrcaster cannot pull: it has no git or python, and no route to
# git.vedur.is. So rek-d01, whose gps-config-data clone gps-config-sync keeps
# current, pushes the ONE file the caster needs, over a key whose forced command
# on ntrcaster (deployment/ntrcaster/ntrcaster-sourcetable) can only report
# status or install a validated sourcetable.
#
# CONVERGENT like sync-config.sh: every tick compares the repo file with the
# hash the caster reports for the file on disk AND for the table it last
# confirmed loading, and pushes or re-rehashes only on a difference — so a hand edit on
# ntrcaster, or a failed earlier push, is reconciled too. It never generates the
# table: that is `tosGPS sourcetable` + a reviewed commit to gps-config-data.
#
# Setup and rollback: docs/deployment/ntrcaster-sourcetable.md

set -euo pipefail

REPO=${GPS_CONFIG_DATA_REPO:-/home/bgo/git/gps-config-data}
SRC=$REPO/ntrcaster/sourcetable.dat
TARGET=${NTRCASTER_TARGET:-gpsops@ntrcaster.vedur.is}
KEY=${NTRCASTER_KEY:-$HOME/.ssh/ntrcaster_sourcetable}
TAG=gps-ntrcaster-sourcetable

ssh_caster() {
    ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 \
        "$TARGET" "$@"
}

if [ ! -f "$SRC" ]; then
    logger -t "$TAG" "WARNING: $SRC missing — nothing to deliver"
    exit 0
fi

want=$(sha256sum < "$SRC" | cut -d' ' -f1)
if ! live=$(ssh_caster status 2>/dev/null); then
    # Caster host unreachable: leave it; the next tick retries.
    logger -t "$TAG" "WARNING: status query to $TARGET failed"
    exit 0
fi
have=$(echo "$live" | awk '$1 == "sha256" {print $2}')
loaded=$(echo "$live" | awk '$3 == "rehashed" {print $4}')
# The clone is bgo's; as gpsops git refuses it without safe.directory.
rev=$(git -c safe.directory="$REPO" -C "$REPO" log -1 --format=%h -- ntrcaster/sourcetable.dat 2>/dev/null || echo "?")

# Two separate questions: is the right file on disk, and has the caster
# loaded it? A file can be installed while its rehash failed or is pending
# (no admin credential yet); install would then answer "unchanged" forever.
if [ "$want" != "$have" ]; then
    verb=install
elif [ "$want" != "$loaded" ]; then
    verb=rehash
else
    exit 0
fi

if out=$(ssh_caster "$verb" < "$SRC" 2>&1); then
    logger -t "$TAG" "$verb sourcetable.dat @ $rev: $out"
else
    logger -t "$TAG" "ERROR: $verb @ $rev failed: $out"
    exit 1
fi
