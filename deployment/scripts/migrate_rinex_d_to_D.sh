#!/usr/bin/env bash
#
# One-off migration: rename archived RINEX files from lowercase .YYd.Z to the
# historical uppercase .YYD.Z convention, and remove old-rek 2.11 duplicates.
#
# WHY: at the old-rek -> rek_new cutover (DOY 172, 2026) the converter started
# emitting lowercase .d.Z (rnx2crx default). okada's getimorinex.py requests
# uppercase .D.Z and broke. The code fix (converter_base) makes FUTURE files
# uppercase; this script fixes the BACKLOG already on disk.
#
# Runs on BOTH archive roots (run once per host):
#   * rek-d01 local archive : --root /mnt/data/gpsdata
#   * rawdata gateway        : --root ~/gpsdata        (run AS gpsops@rawdata)
#
# Per station-day, for each lowercase  STA<doy><s>.YYd.Z :
#   - if an uppercase STA<doy><s>.YYD.Z ALSO exists, it is the OLD rek 2.11
#     duplicate -> it is removed (we keep the new 3.04 content), then
#   - the lowercase file is renamed to uppercase.
# If only the lowercase exists, it is simply renamed.
# If only an uppercase file exists (pure historical day), it is left untouched.
#
# SAFE BY DEFAULT: dry-run unless --apply is given. Idempotent: re-running after
# a completed pass is a no-op (no lowercase files left).
#
# After the file rename, update the catalog + local index with the companion SQL
# (see deployment/scripts/migrate_rinex_d_to_D.sql) so file_path / filename stop
# pointing at the old .d.Z names. canonical_key is case-insensitive, so the
# catalog *key* is unaffected — only the stored path string needs the s/d.Z/D.Z/.
#
set -euo pipefail

ROOT=""
SESSION="15s_24hr"     # daily path first; pass --session 1Hz_1hr for the hourly tier
APPLY=0

usage() { sed -n '2,40p' "$0"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)    ROOT="$2"; shift 2;;
    --session) SESSION="$2"; shift 2;;
    --apply)   APPLY=1; shift;;
    -h|--help) usage;;
    *) echo "unknown arg: $1" >&2; usage;;
  esac
done

[[ -n "$ROOT" ]] || { echo "ERROR: --root is required" >&2; usage; }
ROOT="${ROOT/#\~/$HOME}"
[[ -d "$ROOT" ]] || { echo "ERROR: root not a directory: $ROOT" >&2; exit 1; }

echo "# root=$ROOT  session=$SESSION  apply=$APPLY"
renamed=0; removed_dup=0; skipped=0

# glob: <root>/<YYYY>/<mon>/<STA>/<session>/rinex/<STA><doy><s>.<YY>d.Z
# the type letter is the char immediately before '.Z'; restrict to *[0-9][0-9]d.Z
while IFS= read -r -d '' low; do
  dir="$(dirname "$low")"
  base="$(basename "$low")"
  up="${base%d.Z}D.Z"            # STA...26d.Z -> STA...26D.Z
  upath="$dir/$up"

  if [[ -e "$upath" ]]; then
    # uppercase already present = old-rek 2.11 duplicate; drop it, keep new 3.04
    echo "DUP  rm $upath  (old 2.11)  &&  mv $base -> $up"
    if [[ "$APPLY" == 1 ]]; then rm -f -- "$upath"; mv -- "$low" "$upath"; fi
    removed_dup=$((removed_dup+1)); renamed=$((renamed+1))
  else
    echo "REN  mv $base -> $up"
    if [[ "$APPLY" == 1 ]]; then mv -- "$low" "$upath"; fi
    renamed=$((renamed+1))
  fi
done < <(find "$ROOT" -type f -path "*/$SESSION/rinex/*" -name '*[0-9][0-9]d.Z' -print0)

echo "# done: renamed=$renamed (of which old-2.11 dups removed=$removed_dup) skipped=$skipped"
[[ "$APPLY" == 1 ]] || echo "# DRY-RUN — re-run with --apply to make changes"
