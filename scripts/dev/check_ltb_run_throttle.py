#!/usr/bin/env python3
"""What WOULD an LTB run do? Evidence for re-enabling, gathered while it is off.

`long_term_backfill` is disabled, and the whole point of #174/S3 is that it
cannot be re-enabled on trust. But a throttle demonstrated only by running the
thing it throttles is no evidence at all — so this reports, read-only, what a
run would cost and what each throttle would save.

READ-ONLY. Classification queries plus one `station_connectivity` read per
station. It never downloads, never pings a receiver, and never writes.

    scripts/dev/check_ltb_run_throttle.py                    # both sessions
    scripts/dev/check_ltb_run_throttle.py --session 1Hz_1hr
    scripts/dev/check_ltb_run_throttle.py --stations SKDA SVIN THNA

The number that matters is `worst-case ping hours`: queued slots on stations
the offline gate would NOT skip, times the measured 5-9 s per-slot reachability
cost, divided by `max_workers`. That is the figure the Aug 10-11 collapse was
made of, and it is what the budget has to bound.
"""

from __future__ import annotations

import argparse
import sys

# Measured in production (module docstring of long_term_backfill).
PING_SECONDS_LOW = 5.0
PING_SECONDS_HIGH = 9.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--session", action="append", dest="sessions")
    ap.add_argument("--stations", nargs="*")
    ap.add_argument("--lookback-days", type=int, default=30)
    ap.add_argument("--max-workers", type=int, default=2)
    ap.add_argument("--max-run-seconds", type=float, default=1800.0)
    ap.add_argument("--max-slots-per-run", type=int, default=600)
    args = ap.parse_args()

    from receivers.cli.main import get_all_station_configs
    from receivers.scheduling.long_term_backfill import (
        RunBudget,
        _station_is_offline,
        query_long_term_gaps,
    )

    sessions = args.sessions or ["15s_24hr", "1Hz_1hr"]
    if args.stations:
        stations = [s.upper() for s in args.stations]
    else:
        stations = sorted(
            sid
            for sid, cfg in get_all_station_configs().items()
            if cfg.get("enabled", True)
            and cfg.get("station_status") not in ("discontinued", "inactive")
            and cfg.get("health_check") != "passive"
        )

    print(
        f"{len(stations)} station(s) x {len(sessions)} session(s), "
        f"lookback={args.lookback_days}d — read-only\n"
    )

    # One connectivity read per station, shared across its sessions.
    offline = {sid: _station_is_offline(sid) for sid in stations}

    rows = []
    for session in sessions:
        for sid in stations:
            try:
                r = query_long_term_gaps(sid, session, args.lookback_days)
                rows.append((sid, session, len(r.queued), offline.get(sid)))
            except Exception as e:  # noqa: BLE001
                print(f"  ERROR {sid}/{session}: {e}", file=sys.stderr)

    skipped = [r for r in rows if r[3] is True and r[2]]
    unknown = [r for r in rows if r[3] is None and r[2]]
    running = [r for r in rows if r[3] is False and r[2]]

    saved = sum(r[2] for r in skipped)
    would_run = sum(r[2] for r in running) + sum(r[2] for r in unknown)

    def hours(slots):
        lo = slots * PING_SECONDS_LOW / args.max_workers / 3600
        hi = slots * PING_SECONDS_HIGH / args.max_workers / 3600
        return f"{lo:.1f}-{hi:.1f} h"

    print("WITHOUT the throttle")
    print(f"  queued slots attempted : {saved + would_run}")
    print(
        f"  worst-case ping time   : {hours(saved + would_run)}"
        f"  (at {args.max_workers} workers)"
    )

    print("\nOFFLINE SHORT-CIRCUIT")
    print(f"  station/sessions skipped: {len(skipped)}")
    print(f"  slots not attempted     : {saved}   -> saves {hours(saved)}")
    for sid, session, n, _ in sorted(skipped, key=lambda r: -r[2])[:10]:
        print(f"    {sid} {session:9s} {n:5d} slot(s)")

    budget = RunBudget(
        max_seconds=args.max_run_seconds, max_slots=args.max_slots_per_run
    )
    granted = min(would_run, args.max_slots_per_run or would_run)
    print("\nRUN BUDGET")
    print(f"  {budget.describe()}")
    print(f"  slots reaching the receiver: {would_run} -> capped to {granted}")
    print(f"  worst-case ping time       : {hours(granted)}")
    if would_run > granted:
        print(f"  deferred to later runs     : {would_run - granted}")

    if unknown:
        print(
            f"\n  note: {len(unknown)} station/session had UNKNOWN connectivity "
            "(missing or stale row) and are counted as running — the gate "
            "never treats absent evidence as offline."
        )

    print(
        "\nRESULT: a run is bounded at "
        f"{args.max_run_seconds:.0f}s / {args.max_slots_per_run} slots "
        f"({hours(granted)} of ping worst-case), against an unthrottled "
        f"{hours(saved + would_run)}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
