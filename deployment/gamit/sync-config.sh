#!/usr/bin/env bash
# sync-config.sh — GAMIT host: pull gps-config-data and deploy station.info.
#
# Runs ON the GAMIT host (okada now, gamit-p01 later) as gpsops, the counterpart
# to deployment/server/sync-config.sh which does the same job for rek-d01. The
# point of running it HERE rather than pushing from elsewhere is that the host
# needs no inbound access and no cross-host trust: it pulls, validates, and
# writes its own local path, exactly like rek-d01's config sync.
#
# Invoked by cron (okada manages cron, not systemd user timers):
#     */15 * * * *  /path/to/sync-config.sh --quiet
#
# Safety properties (mirroring deployment/server/sync-config.sh):
#   - network/git failure            -> exit silently, deployed file untouched
#   - candidate fails validation     -> keep the current file, log an error
#   - converged already (same bytes) -> no write, no mtime bump
#   - swap is atomic (same-dir temp + mv), so nothing ever reads a torn file
#   - the previous file is always backed up first, and drift is alarmed
#
# WHY THE GUARD IS NOT A PARSER: semantic validation (fixed-column round-trip,
# date ordering/non-overlap, valid HtCod) belongs at COMMIT time, where tostools
# and the review live. Re-implementing it here would create a second parser that
# drifts from the first. This guard only proves the checkout is INTACT — that is
# what a bad transfer/checkout actually looks like on a host.
#
# Usage:
#   sync-config.sh                 # converge (cron)
#   sync-config.sh --quiet         # cron, no output unless something changed/failed
#   sync-config.sh --dry-run       # report what would change; write nothing
#   sync-config.sh --verify        # exit 1 if deployed != repo (monitoring)
#   sync-config.sh --init-clone    # create the clone if missing, then converge
set -uo pipefail

REPO="${REPO:-$HOME/git/gps-config-data}"
REPO_URL="${REPO_URL:-https://git.vedur.is/aut/gnss/gnss_configs/gps-config-data.git}"
BRANCH="${BRANCH:-main}"
SRC_REL="${SRC_REL:-gamit/station.info.sopac.apr05}"
DST="${DST:-/D/DATABASE/GAMIT/station.info.sopac.apr05}"

# Intactness guard thresholds. The file is ~4 MB / ~19.7 k lines and changes
# slowly, so a large single-tick delta means truncation or corruption, not a
# real edit.
MIN_BYTES=1000000
MAX_SIZE_DELTA_PCT=25
MAX_LINE_DELTA_PCT=20
KEEP_BACKUPS=10

QUIET=0 DRY_RUN=0 VERIFY_ONLY=0 INIT_CLONE=0
for a in "$@"; do
    case "$a" in
        --quiet|-q)     QUIET=1 ;;
        --dry-run|-n)   DRY_RUN=1 ;;
        --verify)       VERIFY_ONLY=1 ;;
        --init-clone)   INIT_CLONE=1 ;;
        --help|-h)      sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown argument: $a" >&2; exit 2 ;;
    esac
done

SRC="$REPO/$SRC_REL"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
say()  { [ "$QUIET" = 1 ] || echo "$@"; }
warn() { echo "WARN  $*" >&2; }
fail() { echo "ERROR $*" >&2; exit 1; }

# ---------------------------------------------------------------- clone ----
if [ ! -d "$REPO/.git" ]; then
    if [ "$INIT_CLONE" != 1 ]; then
        fail "no clone at $REPO (run with --init-clone, or update REPO)"
    fi
    [ "$DRY_RUN" = 1 ] && { say "[dry-run] would clone $REPO_URL -> $REPO"; exit 0; }
    mkdir -p "$(dirname "$REPO")"
    # Anonymous HTTPS: this repo is public, so no key and no known_hosts entry
    # is needed. (SSH from this host fails on host-key verification.)
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$REPO" \
        || fail "clone failed ($REPO_URL)"
    say "cloned $REPO_URL -> $REPO"
fi

# --------------------------------------------------------- fetch / converge -
if [ "${SYNC_TEST_NO_FETCH:-0}" != 1 ]; then
    if ! git -C "$REPO" fetch --quiet origin "$BRANCH" 2>/dev/null; then
        # Network or server down: the deployed file must stay as it is. We can
        # still reconcile against the clone we already have (convergent).
        warn "git fetch failed — reconciling against the local clone"
    fi
fi

if [ "$DRY_RUN" != 1 ] && [ "$VERIFY_ONLY" != 1 ]; then
    # The clone is a deploy artefact that nobody should edit. Surface it if
    # someone did, so a local edit is never silently discarded.
    if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
        warn "clone has local modifications — they will be discarded:"
        git -C "$REPO" status --porcelain | head -5 >&2
    fi
    git -C "$REPO" checkout --quiet "$BRANCH" 2>/dev/null
    git -C "$REPO" reset --hard --quiet "origin/$BRANCH" 2>/dev/null \
        || warn "reset to origin/$BRANCH failed — using current checkout"
fi
COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# ---------------------------------------------------------------- verify ----
if [ "$VERIFY_ONLY" = 1 ]; then
    [ -f "$SRC" ] || fail "repo file missing: $SRC"
    [ -f "$DST" ] || fail "deployed file missing: $DST"
    if cmp -s "$SRC" "$DST"; then
        say "in sync with $COMMIT ($(stat -c%s "$DST") B)"
        exit 0
    fi
    echo "drift: $DST differs from $SRC ($COMMIT)" >&2
    exit 1
fi

# ---------------------------------------------------------------- guard -----
[ -f "$SRC" ] || fail "candidate missing: $SRC (is $SRC_REL on $BRANCH?)"
SIZE=$(stat -c%s "$SRC")
[ "$SIZE" -ge "$MIN_BYTES" ] || fail "candidate suspiciously small: $SIZE B < $MIN_BYTES B"
head -5 "$SRC" | grep -q '^\*SITE' || fail "candidate has no *SITE header in its first 5 lines"
LINES=$(wc -l < "$SRC")

if [ -f "$DST" ]; then
    CUR_SIZE=$(stat -c%s "$DST"); CUR_LINES=$(wc -l < "$DST")
    if [ "$CUR_SIZE" -gt 0 ]; then
        pct=$(( (SIZE - CUR_SIZE) * 100 / CUR_SIZE )); pct=${pct#-}
        [ "$pct" -le "$MAX_SIZE_DELTA_PCT" ] \
            || fail "size moved ${pct}% in one tick ($CUR_SIZE -> $SIZE) — refusing"
    fi
    if [ "$CUR_LINES" -gt 0 ]; then
        pct=$(( (LINES - CUR_LINES) * 100 / CUR_LINES )); pct=${pct#-}
        [ "$pct" -le "$MAX_LINE_DELTA_PCT" ] \
            || fail "line count moved ${pct}% in one tick ($CUR_LINES -> $LINES) — refusing"
    fi
fi

# ---------------------------------------------------------------- converge --
if [ -f "$DST" ] && cmp -s "$SRC" "$DST"; then
    say "unchanged ($COMMIT, $LINES lines)"
    exit 0
fi

if [ "$DRY_RUN" = 1 ]; then
    say "[dry-run] would deploy $COMMIT: $SRC ($SIZE B, $LINES lines) -> $DST"
    exit 0
fi

DST_DIR="$(dirname "$DST")"
[ -d "$DST_DIR" ] || fail "destination dir missing: $DST_DIR"

# backup (only if we are replacing an existing file)
if [ -f "$DST" ]; then
    cp -p "$DST" "$DST.backup.$TS" || fail "backup failed"
    say "backed up -> $DST.backup.$TS"
    # prune, newest KEEP_BACKUPS kept
    ls -1t "$DST".backup.* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | while read -r old; do
        rm -f "$old" && say "pruned $(basename "$old")"
    done
fi

# atomic swap: temp in the SAME directory so mv is a rename, never a copy
tmp="$DST_DIR/.station.info.new.$$"
cp "$SRC" "$tmp" || { rm -f "$tmp"; fail "staging copy failed"; }
mv -f "$tmp" "$DST" || { rm -f "$tmp"; fail "atomic swap failed"; }

# prove the write: bytes on disk must equal the repo
cmp -s "$SRC" "$DST" || fail "DRIFT after swap — $DST != $SRC ($COMMIT)"
say "deployed $COMMIT: $LINES lines, $SIZE B -> $DST"

# machine-readable breadcrumb so a GAMIT result can be traced to a revision
printf '%s %s %s %s\n' "$(date -u +%FT%TZ)" "$COMMIT" "$SIZE" "$(sha256sum <"$DST" | cut -d' ' -f1)" \
    >> "$DST_DIR/.station.info.deployed"
exit 0
