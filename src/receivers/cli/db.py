"""CLI subcommand for database management.

Provides `receivers db` commands for setup, migration, seeding,
status, dump, restore, and station removal for the gps_health database.

Usage:
    receivers db setup [--host HOST]
    receivers db migrate [--host HOST] [--dry-run]
    receivers db seed [--only stations|coordinates|areas] [--dry-run]
    receivers db status [--host HOST]
    receivers db dump
    receivers db restore FILE [--host HOST]
    receivers db drop-station STATION [--dry-run] [--force] [--host HOST]
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Project-relative paths
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
DUMPS_DIR = PROJECT_ROOT / "dumps"


# ── Command handlers ──────────────────────────────────────────────────────────


def cmd_db_setup(args: argparse.Namespace) -> int:
    """Drop schema, apply consolidated migration, seed all data."""
    host = getattr(args, "host", None)

    # Safety check for remote hosts
    if host and host not in ("localhost", "127.0.0.1"):
        confirm = input(f"Type 'gps_health' to confirm DROP + SETUP on {host}: ")
        if confirm != "gps_health":
            print("Aborted.")
            return 1

    print("=== GPS Health Database Setup ===\n")

    # Step 1: Drop schema
    print("--- Dropping existing schema ---")
    try:
        from ..db.connection import get_connection

        conn = get_connection(host_override=host)
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        conn.commit()
        conn.close()
        print("Schema dropped and recreated.\n")
    except Exception as e:
        print(f"Error dropping schema: {e}")
        return 1

    # Step 2: Apply consolidated migration
    print("--- Applying consolidated schema ---")
    try:
        from ..db.migrator import Migrator

        migrator = Migrator(host_override=host)
        applied = migrator.migrate()
        if applied:
            print(f"Applied {len(applied)} migration(s).\n")
        else:
            print("No migrations to apply.\n")
    except Exception as e:
        print(f"Error applying migrations: {e}")
        return 1

    # Step 3: Seed all data
    print("--- Seeding data ---")
    try:
        from ..db.seeder import Seeder

        seeder = Seeder(host_override=host)
        results = seeder.seed_all()
        print("\n=== Setup complete ===")
        _print_seed_summary(results)
    except Exception as e:
        print(f"Error seeding data: {e}")
        return 1

    return 0


def cmd_db_migrate(args: argparse.Namespace) -> int:
    """Apply pending database migrations."""
    host = getattr(args, "host", None)
    dry_run = getattr(args, "dry_run", False)

    from ..db.migrator import Migrator

    migrator = Migrator(host_override=host)

    print("=== Database Migration ===\n")

    if dry_run:
        print("(dry run — no changes will be made)\n")

    try:
        applied = migrator.migrate(dry_run=dry_run)
        if applied:
            print(
                f"\n{'Would apply' if dry_run else 'Applied'} {len(applied)} migration(s)"
            )
        else:
            print("All migrations already applied.")
        return 0
    except Exception as e:
        print(f"Error: {e}")
        logger.exception("Migration failed")
        return 1


def cmd_db_seed(args: argparse.Namespace) -> int:
    """Seed database with station data."""
    host = getattr(args, "host", None)
    dry_run = getattr(args, "dry_run", False)
    only = getattr(args, "only", None)

    from ..db.seeder import Seeder

    seeder = Seeder(host_override=host)

    if dry_run:
        print("(dry run — no changes will be made)\n")

    try:
        if only == "stations":
            seeder.seed_stations(dry_run=dry_run)
        elif only == "coordinates":
            seeder.seed_coordinates(dry_run=dry_run)
        elif only == "areas":
            seeder.seed_areas(dry_run=dry_run)
        elif only == "storage":
            if dry_run:
                print("Storage location seeding does not support dry-run.")
            else:
                seeder.seed_storage_locations()
        else:
            results = seeder.seed_all(dry_run=dry_run)
            _print_seed_summary(results)
        return 0
    except Exception as e:
        print(f"Error: {e}")
        logger.exception("Seeding failed")
        return 1


def cmd_db_status(args: argparse.Namespace) -> int:
    """Show database status: tables, rows, migration state."""
    host = getattr(args, "host", None)

    from ..db.connection import get_connection
    from ..db.migrator import Migrator

    print("=== Database Status ===\n")

    try:
        conn = get_connection(host_override=host)
    except Exception as e:
        print(f"Cannot connect: {e}")
        return 1

    try:
        with conn.cursor() as cur:
            # Database size
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            size = cur.fetchone()[0]
            print(f"Database size: {size}")

            # Connection info
            cur.execute(
                "SELECT current_database(), inet_server_addr(), inet_server_port()"
            )
            db, addr, port = cur.fetchone()
            print(f"Connected to: {db} @ {addr or 'localhost'}:{port or 5432}\n")

            # Table row counts
            cur.execute("""
                SELECT relname AS table_name, n_live_tup AS rows
                FROM pg_stat_user_tables
                WHERE schemaname = 'public'
                ORDER BY n_live_tup DESC
            """)
            rows = cur.fetchall()
            if rows:
                print("Tables:")
                max_name = max(len(r[0]) for r in rows)
                for name, count in rows:
                    print(f"  {name:<{max_name}}  {count:>8} rows")
            else:
                print("No tables found.")

            # Views
            cur.execute("""
                SELECT table_name FROM information_schema.views
                WHERE table_schema = 'public' ORDER BY table_name
            """)
            views = [r[0] for r in cur.fetchall()]
            if views:
                print(f"\nViews ({len(views)}):")
                for v in views:
                    print(f"  {v}")

        # Migration status
        migrator = Migrator(host_override=host)
        status = migrator.status()
        print(
            f"\nMigrations: {len(status['applied'])} applied, {len(status['pending'])} pending"
        )
        if status["pending"]:
            print("Pending:")
            for name in status["pending"]:
                print(f"  - {name}")

        conn.close()
        return 0

    except Exception as e:
        print(f"Error: {e}")
        conn.close()
        return 1


def cmd_db_dump(args: argparse.Namespace) -> int:
    """Dump database to SQL file."""
    import os

    DUMPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    db_name = os.environ.get("POSTGRES_DB", "gps_health")
    db_user = os.environ.get("POSTGRES_USER", os.environ.get("USER", "postgres"))
    dump_file = DUMPS_DIR / f"{db_name}_{timestamp}.sql"

    print(f"Dumping {db_name} to {dump_file}...")

    try:
        subprocess.run(
            [
                "pg_dump",
                "-h",
                "localhost",
                "-U",
                db_user,
                "-d",
                db_name,
                "--no-owner",
                "--no-privileges",
                "--clean",
                "--if-exists",
                "-f",
                str(dump_file),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        size = dump_file.stat().st_size
        print(f"Dump complete: {dump_file} ({size / 1024:.0f} KB)")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"pg_dump failed: {e.stderr}")
        return 1
    except FileNotFoundError:
        print("Error: pg_dump not found. Is PostgreSQL client installed?")
        return 1


def cmd_db_restore(args: argparse.Namespace) -> int:
    """Restore database from SQL dump file."""
    import os

    dump_file = Path(args.file)
    host = getattr(args, "host", None) or "localhost"

    if not dump_file.exists():
        print(f"Error: File not found: {dump_file}")
        return 1

    # Safety check for remote hosts
    if host not in ("localhost", "127.0.0.1"):
        confirm = input(f"Type 'gps_health' to confirm RESTORE on {host}: ")
        if confirm != "gps_health":
            print("Aborted.")
            return 1

    db_name = os.environ.get("POSTGRES_DB", "gps_health")
    db_user = os.environ.get("POSTGRES_USER", os.environ.get("USER", "postgres"))

    print(f"Restoring {dump_file} to {db_name}@{host}...")

    try:
        subprocess.run(
            [
                "psql",
                "-h",
                host,
                "-U",
                db_user,
                "-d",
                db_name,
                "-f",
                str(dump_file),
                "--single-transaction",
                "-v",
                "ON_ERROR_STOP=1",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        print("Restore complete.")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"psql restore failed: {e.stderr}")
        return 1
    except FileNotFoundError:
        print("Error: psql not found. Is PostgreSQL client installed?")
        return 1


def cmd_db_drop_station(args: argparse.Namespace) -> int:
    """Remove a station and all its data from the database."""
    from ..db.connection import get_connection

    station_id = args.station_id.upper()
    host = getattr(args, "host", None)
    dry_run = getattr(args, "dry_run", False)
    force = getattr(args, "force", False)

    try:
        conn = get_connection(host_override=host)
    except Exception as e:
        print(f"Cannot connect: {e}")
        return 1

    try:
        with conn.cursor() as cur:
            # Verify station exists
            cur.execute("SELECT sid FROM stations WHERE sid = %s", (station_id,))
            if not cur.fetchone():
                print(f"Station '{station_id}' not found in database.")
                conn.close()
                return 1

            # Find all tables with a sid column (except stations itself)
            cur.execute("""
                SELECT table_name FROM information_schema.columns
                WHERE column_name = 'sid' AND table_schema = 'public'
                  AND table_name != 'stations'
                ORDER BY table_name
            """)
            tables = [row[0] for row in cur.fetchall()]

            # Count rows per table
            if tables:
                union_sql = " UNION ALL ".join(
                    f"SELECT '{t}' AS tbl, COUNT(*) FROM {t} WHERE sid = %s"
                    for t in tables
                )
                cur.execute(union_sql, tuple(station_id for _ in tables))
                counts = [(row[0], row[1]) for row in cur.fetchall()]
            else:
                counts = []

            # Display summary
            total = sum(c for _, c in counts)
            non_zero = [(t, c) for t, c in counts if c > 0]

            print(f"Station: {station_id}")
            print(f"Tables with data: {len(non_zero)} / {len(counts)}")
            if non_zero:
                max_name = max(len(t) for t, _ in non_zero)
                for tbl, cnt in sorted(non_zero, key=lambda x: -x[1]):
                    print(f"  {tbl:<{max_name}}  {cnt:>8} rows")
            print(f"  {'TOTAL':<20}  {total:>8} rows")
            print("  + 1 row in stations")

            if dry_run:
                print("\n(dry run — no changes made)")
                conn.close()
                return 0

            # Confirmation
            if not force:
                confirm = input(f"\nType '{station_id}' to confirm deletion: ")
                if confirm.strip().upper() != station_id:
                    print("Aborted.")
                    conn.close()
                    return 1

            # Delete station_area_members explicitly (no FK cascade)
            if "station_area_members" in tables:
                cur.execute(
                    "DELETE FROM station_area_members WHERE sid = %s",
                    (station_id,),
                )

            # Delete from stations (cascades to block_* and other FK tables)
            cur.execute("DELETE FROM stations WHERE sid = %s", (station_id,))

        conn.commit()
        conn.close()
        print(f"\nDeleted station {station_id} and {total} related rows.")
        return 0

    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"Error: {e}")
        logger.exception("drop-station failed")
        return 1


# ── Parser registration ───────────────────────────────────────────────────────


def create_db_parser(subparsers) -> None:
    """Add db subcommands to the main parser."""
    db_parser = subparsers.add_parser(
        "db",
        help="Manage GPS health database",
        description="Database setup, migration, seeding, and maintenance",
    )

    db_subparsers = db_parser.add_subparsers(
        dest="db_command",
        help="Database commands",
    )

    # setup
    setup_parser = db_subparsers.add_parser(
        "setup",
        help="Drop + migrate + seed (fresh install)",
    )
    setup_parser.add_argument("--host", help="PostgreSQL host (default: from config)")
    setup_parser.set_defaults(func=cmd_db_setup)

    # migrate
    migrate_parser = db_subparsers.add_parser(
        "migrate",
        help="Apply pending migrations",
    )
    migrate_parser.add_argument("--host", help="PostgreSQL host (default: from config)")
    migrate_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be applied"
    )
    migrate_parser.set_defaults(func=cmd_db_migrate)

    # seed
    seed_parser = db_subparsers.add_parser(
        "seed",
        help="Seed database with station data",
    )
    seed_parser.add_argument("--host", help="PostgreSQL host (default: from config)")
    seed_parser.add_argument(
        "--only",
        choices=["stations", "coordinates", "areas", "storage"],
        help="Only run a specific seed operation",
    )
    seed_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done"
    )
    seed_parser.set_defaults(func=cmd_db_seed)

    # status
    status_parser = db_subparsers.add_parser(
        "status",
        help="Show database status",
    )
    status_parser.add_argument("--host", help="PostgreSQL host (default: from config)")
    status_parser.set_defaults(func=cmd_db_status)

    # dump
    dump_parser = db_subparsers.add_parser(
        "dump",
        help="Dump database to SQL file",
    )
    dump_parser.set_defaults(func=cmd_db_dump)

    # restore
    restore_parser = db_subparsers.add_parser(
        "restore",
        help="Restore database from dump file",
    )
    restore_parser.add_argument("file", help="SQL dump file to restore")
    restore_parser.add_argument("--host", help="PostgreSQL host (default: localhost)")
    restore_parser.set_defaults(func=cmd_db_restore)

    # drop-station
    drop_parser = db_subparsers.add_parser(
        "drop-station",
        help="Remove a station and all its data",
    )
    drop_parser.add_argument("station_id", help="Station ID to remove (e.g. SFEH)")
    drop_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be deleted"
    )
    drop_parser.add_argument(
        "--force", action="store_true", help="Skip confirmation prompt"
    )
    drop_parser.add_argument("--host", help="PostgreSQL host (default: from config)")
    drop_parser.set_defaults(func=cmd_db_drop_station)


def handle_db_command(args: argparse.Namespace) -> int:
    """Handle db subcommands."""
    if not hasattr(args, "db_command") or not args.db_command:
        print("No db command specified.")
        print(
            "Available commands: setup, migrate, seed, status, dump, restore, drop-station"
        )
        print("Run 'receivers db <command> --help' for details.")
        return 1

    return args.func(args)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _print_seed_summary(results: dict) -> None:
    """Print a summary of seed results."""
    print("\nSeed summary:")
    if "stations" in results:
        s = results["stations"]
        print(
            f"  Stations:    {s.get('inserted', 0)} inserted, {s.get('updated', 0)} updated"
        )
    if "coordinates" in results:
        c = results["coordinates"]
        print(f"  Coordinates: {c.get('updated', 0)} updated")
    if "areas" in results:
        a = results["areas"]
        print(
            f"  Areas:       {a.get('areas', 0)} areas, {a.get('members', 0)} members"
        )
    if "storage_locations" in results:
        print(f"  Storage:     {results['storage_locations']} inserted")
