#!/usr/bin/env python3
"""One-time script to recover VARG data with DD directory layout.

VARG (NetR9) switched its directory layout from YYYYMM/Session/ to
YYYYMM/DD/Session/ starting Feb 9, 2026. The data on the receiver
uses the DD subdirectory format for these dates.

Receiver data status (as of 2026-02-25):
  Jan 2-11:  Normal format (202601/15s_24hr/) — degraded after Jan 8
  Jan 12 - Feb 8:  No data (receiver was down)
  Feb 9-25:  DD format (202602/DD/15s_24hr/) — healthy data

This script downloads day-by-day using the DD path format.

Usage:
    # Dry run — just print what would be downloaded
    python scripts/recover_varg_january.py --dry-run

    # Download Feb 9-25 DD format data (default)
    python scripts/recover_varg_january.py

    # Download specific date range
    python scripts/recover_varg_january.py --start 20260209 --end 20260225

    # Also recover January normal-format data
    python scripts/recover_varg_january.py --start 20260102 --end 20260211 --normal
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta


def main():
    parser = argparse.ArgumentParser(
        description="Recover VARG data (DD directory layout change)"
    )
    parser.add_argument(
        "--start", default="20260209",
        help="Start date YYYYMMDD (default: 20260209)",
    )
    parser.add_argument(
        "--end", default="20260225",
        help="End date YYYYMMDD (default: 20260225)",
    )
    parser.add_argument(
        "--session", default="15s_24hr",
        help="Session type (default: 15s_24hr)",
    )
    parser.add_argument(
        "--normal", action="store_true",
        help="Use normal YYYYMM format (for Jan 2-11 data)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be downloaded without executing",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose output",
    )
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y%m%d")
    end = datetime.strptime(args.end, "%Y%m%d")

    # VARG Feb 9+ 2026: /Internal/YYYYMM/DD/session/
    DATE_FORMAT = "%Y%m/%d"
    STATION = "VARG"
    BASE_PATH = "/Internal/"

    if args.normal:
        fmt_label = "YYYYMM (normal)"
    else:
        fmt_label = "YYYYMM/DD"

    print(f"VARG Data Recovery")
    print(f"  Date range: {args.start} -> {args.end}")
    print(f"  Session: {args.session}")
    print(f"  Path format: {BASE_PATH}{fmt_label}/{args.session}/")
    print()

    if args.dry_run:
        current = start
        while current <= end:
            if args.normal:
                date_dir = current.strftime("%Y%m")
            else:
                date_dir = current.strftime(DATE_FORMAT)
            print(f"  {current.strftime('%Y-%m-%d')}: {BASE_PATH}{date_dir}/{args.session}/")
            current += timedelta(days=1)
        return

    # Import receivers machinery
    from receivers.config_utils import get_station_config
    from receivers.trimble.netr9 import NetR9
    from receivers.logging_config import setup_logging

    logger = setup_logging(
        component="recover_varg",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    station_info = get_station_config(STATION)
    if not station_info:
        logger.error("Station %s not found in config", STATION)
        sys.exit(1)

    if not args.normal:
        # Override the date format to use DD subdirectories
        station_info["receiver"]["remote_date_format"] = DATE_FORMAT

    receiver = NetR9(STATION, station_info)

    logger.info("Downloading %s %s from %s to %s", STATION, args.session, args.start, args.end)

    result = receiver.download_data(
        start=args.start,
        end=args.end,
        session=args.session,
        sync=True,
        archive=True,
    )

    files = result.get("downloaded_files", []) if isinstance(result, dict) else []
    print(f"\nDone. {len(files)} file(s) downloaded and archived.")


if __name__ == "__main__":
    main()
