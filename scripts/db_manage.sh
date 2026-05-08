#!/bin/bash
# db_manage.sh — GPS Health Database lifecycle management
#
# Usage:
#   db_manage.sh dump                        # Dump local DB to timestamped file
#   db_manage.sh restore <file> [host]       # Restore dump to host (default: localhost)
#   db_manage.sh migrate [host]              # Run all migrations on host
#   db_manage.sh drop [host]                 # Drop all objects, recreate empty schema
#   db_manage.sh status [host]               # Show table counts and DB size
#
# Environment:
#   POSTGRES_USER  — DB user (default: $USER)
#   POSTGRES_DB    — DB name (default: gps_health)
#
# Examples:
#   ./scripts/db_manage.sh dump
#   ./scripts/db_manage.sh restore dumps/gps_health_20260210_1400.sql pgdev.vedur.is
#   ./scripts/db_manage.sh status pgdev.vedur.is
#   ./scripts/db_manage.sh drop pgdev.vedur.is
#   ./scripts/db_manage.sh migrate pgdev.vedur.is

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────
DB_USER="${POSTGRES_USER:-${USER}}"
DB_NAME="${POSTGRES_DB:-gps_health}"

# Resolve project root (parent of scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DUMPS_DIR="$PROJECT_ROOT/dumps"
MIGRATIONS_DIR="$PROJECT_ROOT/migrations"

# Colors (disabled if not a terminal)
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' NC=''
fi

# ── Helpers ────────────────────────────────────────────────────────────────────
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
header(){ echo -e "\n${BLUE}── $* ──${NC}"; }

usage() {
    echo "Usage: $(basename "$0") <command> [options]"
    echo ""
    echo "Commands:"
    echo "  dump                      Dump local DB to dumps/ directory"
    echo "  restore <file> [host]     Restore dump file to host (default: localhost)"
    echo "  migrate [host]            Apply all migrations in order"
    echo "  drop [host]               Drop all objects, recreate empty schema"
    echo "  status [host]             Show DB size and table row counts"
    echo ""
    echo "Options:"
    echo "  host                      PostgreSQL host (default: localhost)"
    echo ""
    echo "Environment:"
    echo "  POSTGRES_USER             DB user (default: \$USER)"
    echo "  POSTGRES_DB               DB name (default: gps_health)"
    exit 1
}

# ── Commands ───────────────────────────────────────────────────────────────────

cmd_dump() {
    local host="localhost"
    mkdir -p "$DUMPS_DIR"

    local timestamp
    timestamp="$(date +%Y%m%d_%H%M)"
    local dump_file="$DUMPS_DIR/${DB_NAME}_${timestamp}.sql"

    header "Dumping ${DB_NAME}@${host}"
    info "Output: $dump_file"

    pg_dump -h "$host" -U "$DB_USER" -d "$DB_NAME" \
        --no-owner --no-privileges \
        --clean --if-exists \
        -f "$dump_file"

    local size
    size="$(du -h "$dump_file" | cut -f1)"
    info "Dump complete: $dump_file ($size)"
    echo "$dump_file"
}

cmd_restore() {
    local dump_file="${1:?Error: dump file required. Usage: db_manage.sh restore <file> [host]}"
    local host="${2:-localhost}"

    if [[ ! -f "$dump_file" ]]; then
        error "Dump file not found: $dump_file"
        exit 1
    fi

    header "Restoring to ${DB_NAME}@${host}"
    info "Source: $dump_file"

    psql -h "$host" -U "$DB_USER" -d "$DB_NAME" \
        -f "$dump_file" \
        --single-transaction \
        -v ON_ERROR_STOP=1 \
        --quiet

    info "Restore complete"
    cmd_status "$host"
}

cmd_migrate() {
    local host="${1:-localhost}"

    if [[ ! -d "$MIGRATIONS_DIR" ]]; then
        error "Migrations directory not found: $MIGRATIONS_DIR"
        exit 1
    fi

    header "Applying migrations to ${DB_NAME}@${host}"

    local count=0
    local skipped=0
    for f in "$MIGRATIONS_DIR"/[0-9]*.sql; do
        [[ ! -f "$f" ]] && continue
        # Skip rollback files
        [[ "$f" == *_rollback.sql ]] && continue

        local migration_name
        migration_name="$(basename "$f" .sql)"

        # Skip migrations already recorded in schema_migrations
        local already_applied
        already_applied=$(psql -h "$host" -U "$DB_USER" -d "$DB_NAME" --no-align --tuples-only \
            -c "SELECT 1 FROM schema_migrations WHERE migration_name = '$migration_name' LIMIT 1;" 2>/dev/null)
        if [[ "$already_applied" == "1" ]]; then
            skipped=$((skipped + 1))
            continue
        fi

        info "Applying: $(basename "$f")"

        psql -h "$host" -U "$DB_USER" -d "$DB_NAME" \
            -f "$f" \
            -v ON_ERROR_STOP=1 \
            --single-transaction \
            --quiet

        count=$((count + 1))
    done

    info "Applied $count migrations, skipped $skipped already-applied"
}

cmd_drop() {
    local host="${1:-localhost}"

    header "Dropping all objects in ${DB_NAME}@${host}"
    warn "This will DELETE all tables, views, and data!"

    # Safety prompt for remote hosts
    if [[ "$host" != "localhost" && "$host" != "127.0.0.1" ]]; then
        echo -n "Type '$DB_NAME' to confirm drop on $host: "
        read -r confirm
        if [[ "$confirm" != "$DB_NAME" ]]; then
            error "Aborted"
            exit 1
        fi
    fi

    psql -h "$host" -U "$DB_USER" -d "$DB_NAME" \
        -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" \
        --quiet

    info "Schema dropped and recreated"
}

cmd_status() {
    local host="${1:-localhost}"

    header "Status: ${DB_NAME}@${host}"

    echo ""
    psql -h "$host" -U "$DB_USER" -d "$DB_NAME" --no-align --tuples-only -c \
        "SELECT 'Size: ' || pg_size_pretty(pg_database_size('$DB_NAME'));"

    echo ""
    echo "Tables:"
    psql -h "$host" -U "$DB_USER" -d "$DB_NAME" -c \
        "SELECT schemaname || '.' || relname AS table_name,
                n_live_tup AS rows
         FROM pg_stat_user_tables
         ORDER BY n_live_tup DESC;"

    echo ""
    echo "Views:"
    psql -h "$host" -U "$DB_USER" -d "$DB_NAME" -c \
        "SELECT table_schema || '.' || table_name AS view_name
         FROM information_schema.views
         WHERE table_schema = 'public'
         ORDER BY table_name;"
}

# ── Main ───────────────────────────────────────────────────────────────────────
[[ $# -eq 0 ]] && usage

command="$1"
shift

case "$command" in
    dump)    cmd_dump "$@" ;;
    restore) cmd_restore "$@" ;;
    migrate) cmd_migrate "$@" ;;
    drop)    cmd_drop "$@" ;;
    status)  cmd_status "$@" ;;
    -h|--help|help) usage ;;
    *)
        error "Unknown command: $command"
        usage
        ;;
esac
