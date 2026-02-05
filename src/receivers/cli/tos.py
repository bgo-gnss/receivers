#!/usr/bin/env python3
"""
TOS (GPS metadata system) integration CLI for receivers.

Provides commands to extract station metadata from TOS and update
local configuration files (stations.cfg).

Commands:
    coordinates  - Extract station coordinates from TOS
    antennas     - Extract antenna information from TOS (future)
    sync         - Sync all metadata from TOS (future)
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any

# Check for tostools availability
try:
    from tostools.api.tos_client import TOSClient
    HAS_TOSTOOLS = True
except ImportError:
    HAS_TOSTOOLS = False
    TOSClient = None  # type: ignore

# Check for gps_parser availability
try:
    import gps_parser
    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False
    gps_parser = None


def get_stations_from_config() -> List[str]:
    """Get list of all station IDs from stations.cfg."""
    if not HAS_GPS_PARSER:
        return []

    try:
        config = gps_parser.ConfigParser()
        return config.getStationList()
    except Exception as e:
        logging.warning(f"Could not get station list from config: {e}")
        return []


def cmd_tos_coordinates(args) -> int:
    """Extract station coordinates from TOS and optionally update stations.cfg."""

    if not HAS_TOSTOOLS:
        print("❌ tostools not available. Install with: pip install tostools")
        return 1

    logger = logging.getLogger("receivers.cli.tos")

    # Get station list
    if args.station:
        stations = [s.upper() for s in args.station]
    else:
        stations = get_stations_from_config()
        if not stations:
            print("❌ No stations specified and could not load from stations.cfg")
            return 1

    print(f"📡 Extracting coordinates for {len(stations)} station(s) from TOS...")

    # Initialize TOS client
    try:
        client = TOSClient()
    except Exception as e:
        print(f"❌ Failed to initialize TOS client: {e}")
        return 1

    # Extract coordinates
    results: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []

    for sid in stations:
        try:
            # Get station metadata from TOS
            station_data = client.get_complete_station_metadata(sid)

            if station_data:
                lat = station_data.get("lat", 0.0)
                lon = station_data.get("lon", 0.0)
                altitude = station_data.get("altitude", 0.0)

                if lat and lon:
                    results[sid] = {
                        "latitude": float(lat),
                        "longitude": float(lon),
                        "height": float(altitude) if altitude else 0.0
                    }
                    if args.verbose:
                        print(f"  ✅ {sid}: lat={lat:.6f}, lon={lon:.6f}, height={altitude:.2f}m")
                else:
                    errors.append(f"{sid}: No coordinates in TOS")
                    if args.verbose:
                        print(f"  ⚠️  {sid}: No coordinates found in TOS")
            else:
                errors.append(f"{sid}: Not found in TOS")
                if args.verbose:
                    print(f"  ⚠️  {sid}: Station not found in TOS")

        except Exception as e:
            errors.append(f"{sid}: {e}")
            if args.verbose:
                print(f"  ❌ {sid}: Error - {e}")

    print(f"\n📊 Results: {len(results)} stations with coordinates, {len(errors)} errors")

    # Output results
    if args.output == "-" or args.output is None:
        # Print to stdout
        print("\n# Station Coordinates from TOS")
        print("# Format: SID latitude longitude height")
        for sid, coords in sorted(results.items()):
            print(f"{sid} {coords['latitude']:.8f} {coords['longitude']:.8f} {coords['height']:.2f}")

    elif args.output.endswith(".cfg"):
        # Update stations.cfg
        if args.dry_run:
            print(f"\n🔍 Dry run - would update {args.output}")
            for sid, coords in sorted(results.items()):
                print(f"  {sid}: latitude={coords['latitude']:.8f}, longitude={coords['longitude']:.8f}, height={coords['height']:.2f}")
        else:
            updated = _update_stations_cfg(args.output, results)
            if updated:
                print(f"✅ Updated {len(results)} stations in {args.output}")
            else:
                print(f"❌ Failed to update {args.output}")
                return 1

    else:
        # Write to file
        output_path = Path(args.output)
        if args.dry_run:
            print(f"\n🔍 Dry run - would write to {output_path}")
        else:
            with open(output_path, 'w') as f:
                f.write("# Station Coordinates from TOS\n")
                f.write("# Format: SID latitude longitude height\n")
                for sid, coords in sorted(results.items()):
                    f.write(f"{sid} {coords['latitude']:.8f} {coords['longitude']:.8f} {coords['height']:.2f}\n")
            print(f"✅ Wrote coordinates to {output_path}")

    # Print errors if any
    if errors and args.verbose:
        print("\n⚠️  Errors:")
        for err in errors:
            print(f"  {err}")

    return 0 if results else 1


def _update_stations_cfg(cfg_path: str, coordinates: Dict[str, Dict[str, Any]]) -> bool:
    """Update stations.cfg with new coordinates.

    Args:
        cfg_path: Path to stations.cfg file
        coordinates: Dict mapping station ID to coordinate dict

    Returns:
        True if successful, False otherwise
    """
    if not HAS_GPS_PARSER:
        print("❌ gps_parser not available for config update")
        return False

    try:
        # Load existing config
        config = gps_parser.ConfigParser()

        # Update each station's coordinates
        updated_count = 0
        for sid, coords in coordinates.items():
            try:
                station_config = config.getStationConfig(sid)
                if station_config:
                    # Add/update coordinate fields
                    station_config["latitude"] = coords["latitude"]
                    station_config["longitude"] = coords["longitude"]
                    station_config["height"] = coords["height"]
                    updated_count += 1
            except Exception as e:
                logging.warning(f"Could not update {sid}: {e}")

        # Save config
        config.save()
        return updated_count > 0

    except Exception as e:
        logging.error(f"Failed to update stations.cfg: {e}")
        return False


def cmd_tos_antennas(args) -> int:
    """Extract antenna information from TOS (placeholder for future implementation)."""
    print("🚧 Antenna extraction not yet implemented")
    print("   This will extract antenna type and serial from TOS")
    return 1


def cmd_tos_sync(args) -> int:
    """Sync all metadata from TOS (placeholder for future implementation)."""
    print("🚧 Full sync not yet implemented")
    print("   This will sync coordinates, antennas, and other metadata from TOS")
    return 1


def create_tos_parser(subparsers) -> None:
    """Create the TOS subcommand parser with its subcommands."""

    tos_parser = subparsers.add_parser(
        "tos",
        help="Extract metadata from TOS (GPS metadata system)",
        description="Commands to extract station metadata from TOS and update local configuration"
    )

    tos_subparsers = tos_parser.add_subparsers(
        dest="tos_command",
        help="TOS commands"
    )

    # Coordinates command
    coords_parser = tos_subparsers.add_parser(
        "coordinates",
        help="Extract station coordinates from TOS",
        description="Extract latitude, longitude, and height for stations from TOS"
    )
    coords_parser.add_argument(
        "--station", "-s",
        nargs="+",
        metavar="SID",
        help="Station ID(s) to extract (default: all from stations.cfg)"
    )
    coords_parser.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Output file (use '-' for stdout, '.cfg' to update stations.cfg)"
    )
    coords_parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be done without making changes"
    )
    coords_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    coords_parser.set_defaults(func=cmd_tos_coordinates)

    # Antennas command (placeholder)
    antennas_parser = tos_subparsers.add_parser(
        "antennas",
        help="Extract antenna information from TOS",
        description="Extract antenna type and serial number from TOS"
    )
    antennas_parser.add_argument(
        "--station", "-s",
        nargs="+",
        metavar="SID",
        help="Station ID(s) to extract (default: all from stations.cfg)"
    )
    antennas_parser.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Output file"
    )
    antennas_parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be done without making changes"
    )
    antennas_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    antennas_parser.set_defaults(func=cmd_tos_antennas)

    # Sync command (placeholder)
    sync_parser = tos_subparsers.add_parser(
        "sync",
        help="Sync all metadata from TOS",
        description="Sync coordinates, antennas, and other metadata from TOS"
    )
    sync_parser.add_argument(
        "--station", "-s",
        nargs="+",
        metavar="SID",
        help="Station ID(s) to sync (default: all from stations.cfg)"
    )
    sync_parser.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Output file or stations.cfg path"
    )
    sync_parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be done without making changes"
    )
    sync_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    sync_parser.set_defaults(func=cmd_tos_sync)


def handle_tos_command(args) -> int:
    """Handle TOS subcommands."""

    if not hasattr(args, 'tos_command') or not args.tos_command:
        print("❌ No TOS command specified")
        print("Available commands: coordinates, antennas, sync")
        print("\nUse 'receivers tos <command> --help' for more information")
        return 1

    return args.func(args)


if __name__ == "__main__":
    # Direct CLI testing
    parser = argparse.ArgumentParser(description="TOS Integration CLI")
    subparsers = parser.add_subparsers(dest="command")

    create_tos_parser(subparsers)

    args = parser.parse_args()

    if args.command == "tos":
        sys.exit(handle_tos_command(args))
    else:
        parser.print_help()
