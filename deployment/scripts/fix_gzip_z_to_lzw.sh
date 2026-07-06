#!/usr/bin/env bash
#
# One-off remediation: recompress gzip-mislabeled .Z RINEX files to real
# LZW compress(1) output.
#
# WHY: from the old-rek -> rek_new cutover (DOY 172, 2026) until 2026-07-06 the
# converter wrote ".Z" files with Python gzip (magic 1f 8b) instead of Unix
# compress LZW (magic 1f 9d) — the IGS/GAMIT convention and the format of the
# entire legacy archive. Consumers using genuine ncompress uncompress choke on
# the gzip impostors. The code fix (converter_base, receivers 6d0e45b) makes
# FUTURE files LZW; this script fixes the BACKLOG already on disk.
#
# WHERE TO RUN — rek-d01 ONLY (as gpsops):
#     fix_gzip_z_to_lzw.sh --root /mnt/data/gpsdata            # dry-run
#     fix_gzip_z_to_lzw.sh --root /mnt/data/gpsdata --apply
#
#   Do NOT run an --apply pass on rawdata: it has no compress(1) binary, and it
#   doesn't need one — recompression here bumps mtime above the archive-sync
#   watermark, so the next sync pushes the LZW file to rawdata (rinex tier uses
#   rsync --update; the rawdata copy is older, so it is replaced) and re-upserts
#   the catalog. content_sha256 is computed over DECOMPRESSED bytes, so the
#   catalog row is unchanged by recompression.
#
#   To VERIFY rawdata afterwards (read-only, works without compress):
#     fix_gzip_z_to_lzw.sh --root ~/gpsdata --scan-only        # as gpsops@rawdata
#
# SAFETY:
#   * Magic-byte gated: ONLY files starting 1f 8b are touched; genuine LZW
#     (1f 9d) and anything else is never rewritten. Idempotent by construction.
#   * Round-trip verified: the recompressed file must decompress (zcat) to a
#     byte stream with the SAME sha256 as the original gzip payload, or the
#     original is kept and the file is reported as ERROR.
#   * Atomic: the fixed file replaces the original via mv on the same fs.
#   * Files modified in the last 15 minutes are skipped (may be mid-write by
#     the scheduler/backfill); re-run to catch them.
#   * Dry-run unless --apply.
#
set -uo pipefail

ROOT=""
SINCE="2026-06-14"   # cutover safety margin; gzip-.Z can only have mtime >= cutover
APPLY=0
SCAN_ONLY=0

usage() { sed -n '2,38p' "$0"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)      ROOT="$2"; shift 2;;
    --since)     SINCE="$2"; shift 2;;
    --apply)     APPLY=1; shift;;
    --scan-only) SCAN_ONLY=1; shift;;
    *) usage;;
  esac
done
[[ -n "$ROOT" && -d $(eval echo "$ROOT") ]] || usage
ROOT=$(eval echo "$ROOT")

if [[ $APPLY -eq 1 && $SCAN_ONLY -eq 0 ]]; then
  command -v compress >/dev/null || {
    echo "FATAL: compress(1) not installed (apt install ncompress) — refusing --apply" >&2
    exit 1
  }
fi

n_gzip=0 n_lzw=0 n_fixed=0 n_recent=0 n_err=0

# -newermt prunes by WRITE time, not data date: a backfilled 2013 file written
# last week is caught, a legacy 2013 LZW file is not even opened.
while IFS= read -r -d '' f; do
  magic=$(head -c2 "$f" | od -An -tx1 | tr -d ' ')
  if [[ "$magic" == "1f9d" ]]; then
    n_lzw=$((n_lzw + 1)); continue
  fi
  [[ "$magic" == "1f8b" ]] || continue
  n_gzip=$((n_gzip + 1))

  if [[ -n $(find "$f" -mmin -15 2>/dev/null) ]]; then
    echo "SKIP_RECENT|$f"
    n_recent=$((n_recent + 1)); continue
  fi

  if [[ $APPLY -ne 1 || $SCAN_ONLY -eq 1 ]]; then
    echo "WOULD_FIX|$f"
    continue
  fi

  tmp="${f%.Z}.fixtmp"
  if ! gzip -dc "$f" > "$tmp" 2>/dev/null; then
    echo "ERROR|gunzip failed|$f"; rm -f "$tmp"; n_err=$((n_err + 1)); continue
  fi
  sha_plain=$(sha256sum < "$tmp" | cut -d' ' -f1)
  if ! compress -f "$tmp" 2>/dev/null || [[ ! -f "$tmp.Z" ]]; then
    echo "ERROR|compress failed|$f"; rm -f "$tmp" "$tmp.Z"; n_err=$((n_err + 1)); continue
  fi
  sha_back=$(zcat "$tmp.Z" | sha256sum | cut -d' ' -f1)
  if [[ "$sha_plain" != "$sha_back" ]]; then
    echo "ERROR|round-trip sha mismatch|$f"; rm -f "$tmp" "$tmp.Z"; n_err=$((n_err + 1)); continue
  fi
  if mv "$tmp.Z" "$f"; then
    echo "FIXED|$f"
    n_fixed=$((n_fixed + 1))
  else
    echo "ERROR|replace failed|$f"; rm -f "$tmp.Z"; n_err=$((n_err + 1))
  fi
  rm -f "$tmp"
done < <(find "$ROOT" -type f -name '*.Z' -path '*rinex*' -newermt "$SINCE" -print0 2>/dev/null)

echo "----------------------------------------------------------------------"
echo "gzip-.Z found: $n_gzip   fixed: $n_fixed   skipped-recent: $n_recent" \
     "  errors: $n_err   (already-LZW in window: $n_lzw)"
[[ $APPLY -eq 1 && $SCAN_ONLY -eq 0 ]] || echo "(dry-run — nothing was modified; add --apply)"
[[ $n_err -eq 0 ]] || exit 2
