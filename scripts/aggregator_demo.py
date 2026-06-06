#!/usr/bin/env python3
"""Aggregator demo — populate the new station_*_60s tables from block_*_status.

Reads source rows from the laptop's ``gps_health`` (port 5432) and writes
60-s snapshot rows into the test database ``gps_health_test`` (port 5433),
which is the timescaledb-test sidecar from deployment/docker-dev/docker-compose.yml.

**This is not a production aggregator.** It's the smallest possible artifact
that lets the new schema be tested end-to-end against real-shaped data —
dashboards, query plans, compression behaviour, etc.

Usage::

    scripts/aggregator_demo.py                              # default: last 7 days
    scripts/aggregator_demo.py --since '2026-05-10'         # absolute cutoff
    scripts/aggregator_demo.py --days 30                    # last N days
    scripts/aggregator_demo.py --dry-run                    # print counts, no writes
    scripts/aggregator_demo.py --skip-stations              # if already seeded

What it does, in order:
  1. Copy ``stations`` rows from source → target (FK satisfaction + metadata).
  2. For each block_*_status table on source, bucket rows to the nearest minute
     (DISTINCT ON (sid, minute) ... ORDER BY ts DESC takes the last raw row
     per bucket), UPSERT into the corresponding station_*_60s table on target.
  3. Print row counts per pass + total wall time.

What it doesn't do:
  - Populate station_sat_signal_60s — no source ChannelStatus per-signal data
    is parsed into PG today. Empty table until ChannelStatus ingest is built.
  - Aggregate functions other than ``last()`` — when multiple raw rows exist
    in a minute bucket, we keep the most recent one. ``min(voltage)`` and
    similar would happen in the real aggregator.
  - Provenance (``source`` column on station_health_60s) — left as 'demo'.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import execute_values


# ── Connection defaults ──────────────────────────────────────────────────────

DEFAULT_SOURCE: dict[str, str | int] = dict(
    host="localhost", port=5432, dbname="gps_health", user="bgo"
)
DEFAULT_TARGET: dict[str, str | int] = dict(
    host="127.0.0.1", port=5433,
    dbname="gps_health_test",
    user="bgo", password="gps_health_test",
)


def _dsn_of(d: dict[str, str | int]) -> str:
    """Render a connection dict as a libpq key=value DSN string."""
    return " ".join(f"{k}={v}" for k, v in d.items())


# ── Per-block aggregation passes ─────────────────────────────────────────────
#
# Each pass: select columns from one source block table, UPSERT into one
# target snapshot table. AS-aliased columns rename or compute (e.g. boolean
# derivation from counts in station_sat_summary_60s).

PASSES: list[dict] = [
    # ───── station_health_60s — receiver-side SBF blocks ─────
    dict(
        name="power",
        source_table="block_power_status",
        target_table="station_health_60s",
        value_cols=["voltage", "power_source"],
    ),
    dict(
        name="receiver",
        source_table="block_receiver_status",
        target_table="station_health_60s",
        value_cols=[
            "cpu_load", "temperature", "uptime_seconds",
            "rx_status", "rx_error", "ext_error",
        ],
    ),
    dict(
        name="pvt",
        source_table="block_pvt_geodetic",
        target_table="station_health_60s",
        value_cols=[
            "fix_type", "nr_sv", "latitude", "longitude", "height",
            "h_accuracy", "v_accuracy", "latency", "raim_status",
        ],
    ),
    dict(
        name="disk",
        source_table="block_disk_status",
        target_table="station_health_60s",
        value_cols=[
            "used_mb AS disk_used_mb",
            "total_mb AS disk_total_mb",
            "usage_percent AS disk_usage_pct",
        ],
    ),
    dict(
        name="logging",
        source_table="block_logging_status",
        target_table="station_health_60s",
        value_cols=[
            "active_sessions", "session_15s_24hr",
            "session_1hz_1hr", "session_status_1hr",
        ],
    ),

    # ───── station_network_60s — scheduler probes ─────
    dict(
        name="ping",
        source_table="block_ping_status",
        target_table="station_network_60s",
        value_cols=[
            "is_online       AS ping_online",
            "response_time_ms AS ping_ms",
            "packet_loss     AS ping_loss_pct",
        ],
    ),
    dict(
        name="health_summary",
        source_table="block_health_summary",
        target_table="station_network_60s",
        value_cols=[
            "ftp_open     AS ftp_port_open",
            "http_open    AS http_port_open",
            "control_open AS ctrl_port_open",
            "overall_status",
        ],
    ),
    dict(
        name="port_timing",
        source_table="block_port_status",
        target_table="station_network_60s",
        value_cols=[
            # Only the timing fields here; port_open booleans come from
            # block_health_summary (above), authoritative for all 3 ports.
            "download_response_ms AS ftp_response_ms",
            "health_response_ms   AS http_response_ms",
        ],
    ),
    dict(
        name="ntrip",
        source_table="block_ntrip_server",
        target_table="station_network_60s",
        value_cols=[
            "status     AS ntrip_server_status",
            "error_code AS ntrip_error_code",
        ],
    ),

    # ───── station_sat_summary_60s — per-constellation summary ─────
    dict(
        name="sat_summary",
        source_table="block_satellite_tracking",
        target_table="station_sat_summary_60s",
        value_cols=[
            "total    AS sats_total",
            "gps      AS sats_gps",
            "glonass  AS sats_glonass",
            "galileo  AS sats_galileo",
            "beidou   AS sats_beidou",
            "sbas     AS sats_sbas",
            "qzss     AS sats_qzss",
            "irnss    AS sats_irnss",
            "(gps     > 0) AS has_gps",
            "(glonass > 0) AS has_glonass",
            "(galileo > 0) AS has_galileo",
            "(beidou  > 0) AS has_beidou",
            "(sbas    > 0) AS has_sbas",
            "(qzss    > 0) AS has_qzss",
            "(irnss   > 0) AS has_irnss",
        ],
    ),
]


# ── Helpers ─────────────────────────────────────────────────────────────────


_AS_RE = re.compile(r"\s+AS\s+", re.IGNORECASE)


def parse_col(spec: str) -> tuple[str, str]:
    """``'a AS b'`` → ``('a', 'b')``; ``'a'`` → ``('a', 'a')``."""
    parts = _AS_RE.split(spec, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return spec.strip(), spec.strip()


def source_table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)
        )
        return cur.fetchone()[0]


# ── Steps ───────────────────────────────────────────────────────────────────


def seed_stations(src_conn, dst_conn, dry_run: bool) -> int:
    """Copy stations rows source → target. Only columns common to both DBs."""
    # Introspect intersection of columns (target schema may be newer/older)
    with src_conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='stations'
        """)
        src_cols = {r[0] for r in cur.fetchall()}
    with dst_conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='stations'
        """)
        dst_cols = {r[0] for r in cur.fetchall()}
    common = sorted(src_cols & dst_cols)
    if "sid" not in common:
        raise RuntimeError("stations table missing 'sid' in source or target")
    cols_sql = ", ".join(common)
    update_cols = [c for c in common if c != "sid"]
    update_sql = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    # The target's migration 001 has receiver_type NOT NULL, but the source
    # (laptop DB) has had that constraint relaxed by a later migration. Coerce
    # NULLs to 'unknown' on the way through so inactive-station rows survive
    # the FK seeding.
    select_exprs = ", ".join(
        f"COALESCE({c}, 'unknown') AS {c}" if c == "receiver_type" else c
        for c in common
    )
    with src_conn.cursor() as cur:
        cur.execute(f"SELECT {select_exprs} FROM stations")
        rows = cur.fetchall()
    if not rows:
        print("  stations: source is empty, skipping")
        return 0
    if dry_run:
        print(f"  stations: would copy {len(rows)} rows ({len(common)} cols)")
        return len(rows)
    with dst_conn.cursor() as cur:
        execute_values(
            cur,
            f"INSERT INTO stations ({cols_sql}) VALUES %s "
            f"ON CONFLICT (sid) DO UPDATE SET {update_sql}",
            rows,
        )
    dst_conn.commit()
    print(f"  stations: copied {len(rows)} rows")
    return len(rows)


def run_pass(p: dict, src_conn, dst_conn, since: datetime, dry_run: bool) -> int:
    """Run one aggregation pass — read source, UPSERT into target."""
    if not source_table_exists(src_conn, p["source_table"]):
        print(f"  {p['name']:14s}: source {p['source_table']} not present, skipping")
        return 0

    parsed = [parse_col(c) for c in p["value_cols"]]
    select_exprs = ", ".join(f"({src_expr}) AS {tgt}" for src_expr, tgt in parsed)
    target_cols = [tgt for _, tgt in parsed]

    # DISTINCT ON keeps the last raw row per (sid, minute) bucket.
    select_sql = f"""
        SELECT DISTINCT ON (sid, date_trunc('minute', ts))
               sid,
               date_trunc('minute', ts) AS ts,
               {select_exprs}
          FROM {p['source_table']}
         WHERE ts >= %s
           AND sid IS NOT NULL
         ORDER BY sid, date_trunc('minute', ts), ts DESC
    """
    with src_conn.cursor() as cur:
        cur.execute(select_sql, (since,))
        rows = cur.fetchall()
    if not rows:
        print(f"  {p['name']:14s}: no source data since {since.isoformat()}")
        return 0
    if dry_run:
        print(f"  {p['name']:14s}: would UPSERT {len(rows):6d} rows into "
              f"{p['target_table']}")
        return len(rows)

    col_list = ", ".join(target_cols)
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in target_cols)
    upsert_sql = f"""
        INSERT INTO {p['target_table']} (sid, ts, {col_list})
        VALUES %s
        ON CONFLICT (sid, ts) DO UPDATE SET {update_clause}
    """
    with dst_conn.cursor() as cur:
        execute_values(cur, upsert_sql, rows)
    dst_conn.commit()
    print(f"  {p['name']:14s}: UPSERT {len(rows):6d} rows into {p['target_table']}")
    return len(rows)


def cutoff_since(args, src_conn) -> datetime:
    if args.since:
        try:
            return datetime.fromisoformat(args.since).replace(
                tzinfo=args.since.endswith("Z")
                and timezone.utc
                or None
            ) or datetime.fromisoformat(args.since)
        except ValueError:
            sys.exit(f"--since: invalid ISO date {args.since!r}")
    # Default: anchor to source's max(ts) so demo works on stale data too.
    with src_conn.cursor() as cur:
        cur.execute("SELECT max(ts) FROM block_power_status")
        max_ts = cur.fetchone()[0]
    if max_ts is None:
        max_ts = datetime.now(tz=timezone.utc)
    return max_ts - timedelta(days=args.days)


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--days", type=int, default=7,
                   help="Look back N days from source's max(ts) (default: 7).")
    p.add_argument("--since", help="Absolute ISO cutoff (overrides --days). "
                                   "Example: 2026-05-10T00:00:00")
    p.add_argument("--skip-stations", action="store_true",
                   help="Skip the stations seeding step (use after first run).")
    p.add_argument("--only", action="append", default=[],
                   help="Run only named passes (repeatable). e.g. --only power --only ping")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would happen, write nothing.")
    p.add_argument("--source-dsn",
                   help="Override source DSN (default: laptop's gps_health on :5432).")
    p.add_argument("--target-dsn",
                   help="Override target DSN (default: gps_health_test on :5433).")
    args = p.parse_args()

    src_conn = psycopg2.connect(args.source_dsn or _dsn_of(DEFAULT_SOURCE))
    dst_conn = psycopg2.connect(args.target_dsn or _dsn_of(DEFAULT_TARGET))

    since = cutoff_since(args, src_conn)
    print(f"\naggregator_demo — cutoff {since.isoformat()} "
          f"{'(DRY RUN)' if args.dry_run else ''}")
    print(f"  source: {src_conn.dsn}")
    print(f"  target: {dst_conn.dsn}\n")

    start = time.time()
    total_rows = 0

    if not args.skip_stations:
        total_rows += seed_stations(src_conn, dst_conn, args.dry_run)

    passes = (
        [p for p in PASSES if p["name"] in set(args.only)]
        if args.only else PASSES
    )
    for pass_def in passes:
        total_rows += run_pass(pass_def, src_conn, dst_conn, since, args.dry_run)

    elapsed = time.time() - start
    print(f"\n{'would write' if args.dry_run else 'wrote'} "
          f"{total_rows:,} rows in {elapsed:.1f}s")

    src_conn.close()
    dst_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
