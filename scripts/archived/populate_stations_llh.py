#!/usr/bin/env python3
"""
Populate stations.cfg with LLH coordinates from XYZ coordinate file.

One-time conversion script that:
1. Reads XYZ coordinates from gps-config-data/station_coord.xyz
2. Converts to latitude/longitude/height using geofunc (with WGS84 fallback)
3. Writes latitude, longitude, height fields into each station section in stations.cfg

After running, the database seeder can read coordinates directly from
stations.cfg — no geofunc dependency for routine DB operations.

Usage:
    python scripts/populate_stations_llh.py [--dry-run] [--xyz-file PATH]

Note: This modifies stations.cfg IN PLACE. Back up the file first if needed.
"""

import argparse
import configparser
import math
import os
import sys
from pathlib import Path

# Add sibling packages to path for development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "geofunc" / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "gps_parser" / "src"))

try:
    from geofunc.geo import xyzell
    HAS_GEOFUNC = True
except ImportError:
    HAS_GEOFUNC = False
    print("Warning: geofunc not available, using fallback WGS84 conversion")


def xyz_to_llh_fallback(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Fallback XYZ to LLH conversion using iterative WGS84 algorithm."""
    a = 6378137.0
    f = 1 / 298.257223563
    b = a * (1 - f)
    e2 = (a**2 - b**2) / a**2

    lon = math.atan2(y, x)
    p = math.sqrt(x**2 + y**2)
    lat = math.atan2(z, p * (1 - e2))

    for _ in range(10):
        N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - N
        lat = math.atan2(z, p * (1 - e2 * N / (N + h)))

    N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    h = p / math.cos(lat) - N

    return math.degrees(lat), math.degrees(lon), h


def parse_xyz_file(filepath: str) -> dict[str, tuple[float, float, float]]:
    """Parse station_coord.xyz file."""
    coordinates: dict[str, tuple[float, float, float]] = {}
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 4:
                try:
                    x = float(parts[0])
                    y = float(parts[1])
                    z = float(parts[2])
                    station = parts[3].upper()
                    coordinates[station] = (x, y, z)
                except (ValueError, IndexError):
                    continue
    return coordinates


def convert_xyz_to_llh(
    xyz_coords: dict[str, tuple[float, float, float]],
) -> dict[str, tuple[float, float, float]]:
    """Convert XYZ coordinates to lat/lon/height."""
    llh_coords: dict[str, tuple[float, float, float]] = {}
    for station, (x, y, z) in xyz_coords.items():
        if HAS_GEOFUNC:
            try:
                result = xyzell([x, y, z], radians=False)
                lat, lon, height = result
                llh_coords[station] = (float(lat), float(lon), float(height))
            except Exception as e:
                print(f"  geofunc failed for {station}, using fallback: {e}")
                llh_coords[station] = xyz_to_llh_fallback(x, y, z)
        else:
            llh_coords[station] = xyz_to_llh_fallback(x, y, z)
    return llh_coords


def find_stations_cfg() -> Path:
    """Find stations.cfg using GPS_CONFIG_PATH or default location."""
    config_path = os.environ.get("GPS_CONFIG_PATH")
    if config_path:
        p = Path(config_path) / "stations.cfg"
        if p.exists():
            return p

    # Default XDG location
    default = Path.home() / ".config" / "gpsconfig" / "stations.cfg"
    if default.exists():
        return default

    # Try gps_parser to find it
    try:
        import gps_parser
        parser = gps_parser.ConfigParser()
        # The config file path is stored in the parser
        cfg_path = Path(parser.config_file)
        if cfg_path.exists():
            return cfg_path
    except Exception:
        pass

    print("Error: Cannot find stations.cfg")
    print("Set GPS_CONFIG_PATH or ensure ~/.config/gpsconfig/stations.cfg exists")
    sys.exit(1)


def update_stations_cfg(
    stations_cfg_path: Path,
    llh_coords: dict[str, tuple[float, float, float]],
    dry_run: bool = False,
) -> tuple[int, int]:
    """Write LLH coordinates into stations.cfg.

    Returns (updated_count, skipped_count).
    """
    # Read the raw file to preserve formatting and comments
    config = configparser.ConfigParser(interpolation=None)
    config.read(str(stations_cfg_path))

    updated = 0
    skipped = 0

    for section in config.sections():
        # Only process station sections (4-letter uppercase)
        if not (len(section) == 4 and section.isupper()):
            continue

        if section not in llh_coords:
            skipped += 1
            continue

        lat, lon, height = llh_coords[section]

        if dry_run:
            existing_lat = config.get(section, "latitude", fallback=None)
            action = "overwrite" if existing_lat else "add"
            print(f"  {section}: {action} lat={lat:.6f}, lon={lon:.6f}, h={height:.2f}")
        else:
            config.set(section, "latitude", f"{lat:.6f}")
            config.set(section, "longitude", f"{lon:.6f}")
            config.set(section, "height", f"{height:.2f}")

        updated += 1

    if not dry_run:
        with open(stations_cfg_path, "w") as f:
            config.write(f)

    return updated, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Populate stations.cfg with LLH coordinates from XYZ file"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without modifying stations.cfg",
    )
    parser.add_argument(
        "--xyz-file",
        default=str(
            Path(__file__).parent.parent.parent
            / "gps-config-data"
            / "station_coord.xyz"
        ),
        help="Path to XYZ coordinate file",
    )
    args = parser.parse_args()

    # Step 1: Read XYZ coordinates
    print(f"Reading coordinates from: {args.xyz_file}")
    if not Path(args.xyz_file).exists():
        print(f"Error: XYZ file not found: {args.xyz_file}")
        return 1

    xyz_coords = parse_xyz_file(args.xyz_file)
    print(f"Found {len(xyz_coords)} stations in XYZ file")

    # Step 2: Convert to LLH
    print(f"Converting XYZ to LLH (using {'geofunc' if HAS_GEOFUNC else 'WGS84 fallback'})...")
    llh_coords = convert_xyz_to_llh(xyz_coords)

    # Show samples
    print("\nSample conversions:")
    for station, (lat, lon, height) in list(llh_coords.items())[:5]:
        print(f"  {station}: lat={lat:.6f}, lon={lon:.6f}, h={height:.2f}m")

    # Step 3: Find and update stations.cfg
    stations_cfg = find_stations_cfg()
    print(f"\nstations.cfg: {stations_cfg}")
    print(f"{'(dry run)' if args.dry_run else 'Writing coordinates...'}")

    updated, skipped = update_stations_cfg(stations_cfg, llh_coords, dry_run=args.dry_run)

    print(f"\nSummary: {updated} stations updated, {skipped} without coordinates")

    # Report stations in XYZ but not in config
    try:
        import gps_parser
        parser_obj = gps_parser.ConfigParser()
        config_sections = {
            s for s in parser_obj.config.sections()
            if len(s) == 4 and s.isupper()
        }
        xyz_only = set(xyz_coords.keys()) - config_sections
        if xyz_only:
            print(f"\nStations in XYZ file but not in stations.cfg ({len(xyz_only)}):")
            for sid in sorted(xyz_only):
                print(f"  {sid}")
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
