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
# This script logs in once, then fires GET / POST / PATCH at the same token and
# prints the HTTP status of each so the pattern is visible at a glance. The PATCH
# is a NO-OP (writes an attribute value back to its current value), so it changes
# nothing whether it 401s or succeeds.
#
# Usage:
#   TOS_USERNAME=bgo TOS_PASSWORD='…' ./tos_auth_repro.sh
#   (prompts if the env vars are unset)
#
# Optional overrides:
#   BASE_URL    default https://vi-api.vedur.is/tos/internal
#   ENTITY_ID   station entity to read/patch (default 4448 = RIFC)
#   SEARCH_TERM term for the POST /basic_search read (default RIFC)
#
set -euo pipefail

BASE_URL="${BASE_URL:-https://vi-api.vedur.is/tos/internal}"
ENTITY_ID="${ENTITY_ID:-4448}"
SEARCH_TERM="${SEARCH_TERM:-RIFC}"

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
echo "MUTATION (no-op PATCH of $av) — expect 401 = the bug:"
if [[ -n "$av" && "$av" != "null" ]]; then
  avid=$(jq -r '.id'    <<<"$av")
  body=$(jq -c '{value: .value}' <<<"$av")
  req PATCH "/attribute_value/$avid" "$body"
else
  echo "  (no open attribute_value found on entity $ENTITY_ID — set ENTITY_ID to one that has attributes)"
fi
echo
echo "If GET/POST are 2xx and PATCH is 401, that's the report for IT."
