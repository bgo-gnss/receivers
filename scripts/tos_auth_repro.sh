#!/usr/bin/env bash
#
# tos_auth_repro.sh — reproduce the TOS backend mutating-method 401.
#
# Symptom (since the 2026-06-24 backend rollout): with ONE valid JWT,
#   * GET   requests succeed (200)
#   * POST  requests succeed (200)   ← e.g. /basic_search, create maintenance
#   * PUT / PATCH return 401 "User provided an invalid token" — the SAME token,
#     seconds later. Intermittent (clears on retry). Token has API.TOS.Admin scope.
#
# This script logs in once, confirms GET + POST succeed, then repeats the SAME
# no-op PATCH ITERATIONS times (default 50) on one token and reports how often it
# 401s. The PATCH writes an attribute value back to its current value, so it
# changes nothing whether it 401s or succeeds. Because the fault is intermittent,
# one PATCH is a coin-flip — the loop is what makes it reproducible and gives a
# failure rate (scattered failures point at one bad node in a load-balanced pool).
#
# Usage:
#   TOS_USERNAME=bgo TOS_PASSWORD='…' ./tos_auth_repro.sh
#   (prompts if the env vars are unset)
#
# Optional overrides:
#   BASE_URL    default https://vi-api.vedur.is/tos/internal
#   ENTITY_ID   station entity to read/patch (default 4448 = RIFC)
#   SEARCH_TERM term for the POST /basic_search read (default RIFC)
#   ITERATIONS  how many times to repeat the PATCH (default 50)
#
set -euo pipefail

BASE_URL="${BASE_URL:-https://vi-api.vedur.is/tos/internal}"
ENTITY_ID="${ENTITY_ID:-4448}"
SEARCH_TERM="${SEARCH_TERM:-RIFC}"
ITERATIONS="${ITERATIONS:-50}"   # times to repeat the PATCH (the 401 is intermittent)

command -v jq   >/dev/null || { echo "need: jq";   exit 1; }
command -v curl >/dev/null || { echo "need: curl"; exit 1; }

[[ -n "${TOS_USERNAME:-}" ]] || read -rp  "TOS username: " TOS_USERNAME
[[ -n "${TOS_PASSWORD:-}" ]] || read -rsp "TOS password: " TOS_PASSWORD && echo

echo "Backend: $BASE_URL"

# --- login (POST /login, HTTP Basic) → JWT in .sid ----------------------------
cred=$(printf '%s:%s' "$TOS_USERNAME" "$TOS_PASSWORD" | base64 | tr -d '\n')
login=$(curl -fsS -X POST "$BASE_URL/login" -H "Authorization: Basic $cred")
TOKEN=$(jq -r '.sid // .token // empty' <<<"$login")
[[ -n "$TOKEN" ]] || { echo "login failed: $login"; exit 1; }
echo "Login OK — scope: $(jq -rc '.profile.scope // .scope // []' <<<"$login")"
echo

# --- helper: METHOD PATH [JSON] → prints "METHOD PATH -> CODE" -----------------
req() {
  local method=$1 path=$2 data=${3:-} code
  if [[ -n "$data" ]]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$method" "$BASE_URL$path" \
             -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$data")
  else
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$method" "$BASE_URL$path" \
             -H "Authorization: Bearer $TOKEN")
  fi
  printf '  %-5s %-42s -> %s\n' "$method" "$path" "$code"
}

echo "READS — expect 2xx:"
req GET  "/history/entity/$ENTITY_ID"
req POST "/basic_search" "{\"search_term\":\"$SEARCH_TERM\"}"
echo

# --- find one open attribute value on the entity, to PATCH back unchanged ------
av=$(curl -fsS "$BASE_URL/history/entity/$ENTITY_ID" -H "Authorization: Bearer $TOKEN" \
       | jq -c 'first(.. | objects
                 | select(.id_attribute_value? != null and .date_to == null)
                 | {id: .id_attribute_value, value: .value})')
if [[ -z "$av" || "$av" == "null" ]]; then
  echo "  (no open attribute_value on entity $ENTITY_ID — set ENTITY_ID to one with attributes)"
  exit 1
fi
avid=$(jq -r '.id' <<<"$av")
body=$(jq -c '{value: .value}' <<<"$av")

# The 401 is INTERMITTENT: a single PATCH usually succeeds, so hammer the SAME
# no-op PATCH N times (same token) and report how often it 401s. Scattered
# failures (not a clean break) point at one bad node in a load-balanced pool.
echo "MUTATION — ${ITERATIONS}x no-op PATCH /attribute_value/$avid (same token):"
ok=0 e401=0 other=0 line=""
for ((i = 1; i <= ITERATIONS; i++)); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X PATCH \
           "$BASE_URL/attribute_value/$avid" \
           -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
           -d "$body")
  case "$code" in
    2*)  ok=$((ok + 1));    line+="." ;;
    401) e401=$((e401 + 1)); line+="X" ;;
    *)   other=$((other + 1)); line+="?" ;;
  esac
done
echo "  [. = 2xx   X = 401   ? = other]"
echo "  $line"
printf '  → %d/%d OK, %d/%d 401 (%d%%), %d other\n\n' \
  "$ok" "$ITERATIONS" "$e401" "$ITERATIONS" "$((e401 * 100 / ITERATIONS))" "$other"

if ((e401 > 0)); then
  echo "Reproduced: a valid Admin token 401s on some PATCH requests while GET/POST above"
  echo "stayed 2xx. Scattered X's = likely one un-updated/misconfigured node in the pool."
else
  echo "No 401 this run — it's intermittent: re-run, or raise ITERATIONS (e.g. ITERATIONS=200)."
fi
