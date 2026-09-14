#!/usr/bin/env python3
"""Live-DB check that the long-term-backfill classifier is sane fleet-wide.

Run this before touching anything in `long_term_backfill.py`, and again before
each stage of the #174 re-enable. It imports the SHIPPED classifier, so it
proves the real code path rather than a reimplementation of it.

Read-only and **serial** by design: `query_long_term_gaps` opens ~6 short-lived
connections per station/session, and rek-d01's midnight burst was measured at
97 of 100 connections on 2026-09-14. There is no headroom to parallelise this.
A full 180-station sweep takes ~130 s.

    # on rek-d01, against the deployed code
    ssh gpsops@rek-d01.vedur.is 'receivers-dev-check-ltb'   # if installed
    /home/bgo/git/receivers/venv/bin/python3 scripts/dev/check_ltb_catalog_oracle.py

    # against a branch that is NOT deployed (main tree stays clean):
    #   git worktree add /tmp/ltb-x <branch>          (as bgo)
    #   PYTHONPATH=/tmp/ltb-x/src /home/bgo/git/receivers/venv/bin/python3 \
    #       /tmp/ltb-x/scripts/dev/check_ltb_catalog_oracle.py
    #   git worktree remove --force /tmp/ltb-x

What "good" looks like (measured 2026-09-14, lookback 30, at `294eb0e`):

    15s_24hr  163/180 stations queued=0, 219 total queued
    1Hz_1hr   146/180 stations queued=0, 6963 total queued

The single most important line is NOT the total. It is the **--probe** block:
a healthy station must report gaps only on the trailing edge, AND a station
with a known outage must still report a real queue. Without the second,
`queued=0` is indistinguishable from an oracle that blindly reports everything
present — which is how the pre-S1 filesystem oracle failed, in the other
direction.

The trailing edge, and why "clean" is not "queued == 0"
-------------------------------------------------------
The window ends at `today - 1`, but the presence oracle is the LONG-TERM
archive, which lags local production by up to the archive-sync interval (hourly
at :45) plus push latency. So the final day's last hour is routinely downloaded
and on local disk while not yet in `archive_catalog`. Measured 2026-09-14
00:36: THOB, OLKE and GONH each queued exactly `(2026-09-13, 23)`, ~50 min
after that hour was collected.

"Clean" therefore means **every queued slot falls on the window's last day**.
That still fails loudly on the pre-S1 shape (216 slots spread over 30 days) but
does not flap on the trailing hour.

KNOWN GAP for the #174 re-enable: the classifier does not itself exclude that
trailing edge, so at ~180 stations a run would attempt ~180 downloads for files
already on local disk and about to be pushed. Small, but close it in S2/S3 —
either end the window at `today - 2` or clamp it to the last successful
archive-sync watermark.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

# Stations with a known, stable expectation. Keep this list honest: when one of
# these is repaired or decommissioned, update the expectation rather than
# deleting the row, or the check quietly stops discriminating.
PROBES = [
    # (sid, session, expectation, why)
    ("THOB", "1Hz_1hr", "clean", "healthy PolaRX5; was queued=216/already_ok=0 pre-S1"),
    ("ELDC", "1Hz_1hr", "clean", "healthy PolaRX5"),
    ("OLKE", "1Hz_1hr", "clean", "healthy PolaRX5"),
    (
        "GONH",
        "1Hz_1hr",
        "clean",
        "mosaic-X5 STREAM station: almost no raw, rinex-only. "
        "Was queued=684 until the #Rin2 rinex-key fix",
    ),
    ("HRIC", "1Hz_1hr", "gappy", "genuinely losing data, receivers todo #167"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--lookback",
        type=int,
        default=30,
        help="days (default 30 — the 1Hz receiver ring is ~30d, so a "
        "wider window is unrecoverable anyway)",
    )
    ap.add_argument("--sessions", nargs="+", default=["15s_24hr", "1Hz_1hr"])
    ap.add_argument(
        "--probe-only",
        action="store_true",
        help="skip the fleet sweep; just run the PROBES",
    )
    ap.add_argument(
        "--json", dest="json_out", help="write the full per-station result to this path"
    )
    args = ap.parse_args()

    from receivers.scheduling.long_term_backfill import (
        ARCHIVE_STORAGE_LOCATION,
        query_long_term_gaps,
    )

    if ARCHIVE_STORAGE_LOCATION != "imo_archive":
        print(
            f"FAIL: storage tier is {ARCHIVE_STORAGE_LOCATION!r}, not 'imo_archive'. "
            "local_raw/local_rinex are the pruned rolling window in DB form — "
            "reading them reintroduces the bug S1 removed."
        )
        return 2

    failures = 0

    # ---- the discriminating check --------------------------------------
    print(f"=== probes (lookback {args.lookback}) ===")
    for sid, session, expect, why in PROBES:
        try:
            r = query_long_term_gaps(sid, session, lookback_days=args.lookback)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {sid}/{session}: {e}")
            failures += 1
            continue
        n = len(r.queued)
        # Only the window's LAST DAY may carry gaps on a healthy station — see
        # "The trailing edge" in the module docstring. Anything older is real.
        stale = [g for g in r.queued if g.file_date < r.end]
        if expect == "clean":
            ok = not stale and r.already_ok > 0
        else:
            ok = n > 0
        failures += 0 if ok else 1
        tail = n - len(stale)
        print(
            f"  [{'ok  ' if ok else 'FAIL'}] {sid}/{session}: "
            f"queued={n} (stale={len(stale)} trailing={tail}) "
            f"already_ok={r.already_ok} (expect {expect}) — {why}"
        )
        if stale:
            print(f"         oldest stale: {stale[0].file_date} h{stale[0].file_hour}")

    if args.probe_only:
        return 1 if failures else 0

    # ---- fleet sweep ---------------------------------------------------
    from receivers.cli.main import get_all_station_configs

    active = [
        sid
        for sid, cfg in sorted(get_all_station_configs().items())
        if cfg.get("enabled", True)
        and cfg.get("station_status") not in ("discontinued", "inactive")
        and cfg.get("health_check") != "passive"
    ]
    print(f"\n=== fleet sweep: {len(active)} active stations, serial ===")

    rows = []
    t0 = time.time()
    for sid in active:
        for session in args.sessions:
            try:
                r = query_long_term_gaps(sid, session, lookback_days=args.lookback)
                rows.append(
                    dict(
                        sid=sid,
                        session=session,
                        queued=len(r.queued),
                        already_ok=r.already_ok,
                        gone=r.confirmed_gone,
                        prov=r.provisional_absent,
                    )
                )
            except Exception as e:  # noqa: BLE001
                rows.append(dict(sid=sid, session=session, error=str(e)[:160]))
    print(f"swept in {time.time() - t0:.0f}s")

    errs = [r for r in rows if "error" in r]
    for session in args.sessions:
        ok_rows = [r for r in rows if r["session"] == session and "queued" in r]
        if not ok_rows:
            continue
        zero = sum(1 for r in ok_rows if r["queued"] == 0)
        print(
            f"\n{session}: {zero}/{len(ok_rows)} stations queued=0, "
            f"total queued={sum(r['queued'] for r in ok_rows)}"
        )
        # already_ok == 0 across a whole window is the shape worth eyeballing:
        # either the station is genuinely dead, or the oracle has a blind spot
        # for its naming. The #Rin2 rinex-key defect looked exactly like this.
        blind = [r for r in ok_rows if r["queued"] > 0 and r["already_ok"] == 0]
        if blind:
            print(
                f"  {len(blind)} station(s) with already_ok=0 — confirm each is "
                "genuinely dead, not a naming blind spot:"
            )
            for r in sorted(blind, key=lambda r: -r["queued"])[:10]:
                print(
                    f"    {r['sid']:5s} queued={r['queued']:5d} gone={r['gone']} "
                    f"prov={r['prov']}"
                )

    if errs:
        failures += len(errs)
        print(f"\nERRORS: {len(errs)}")
        for r in errs[:5]:
            print("  ", r["sid"], r["session"], r["error"])

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(rows, fh, indent=1)
        print(f"\nwrote {args.json_out}")

    print("\nRESULT:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
