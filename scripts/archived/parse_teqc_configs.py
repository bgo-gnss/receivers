#!/usr/bin/env python3
"""Parse teqc config files and extract RINEX header metadata.

This script parses the legacy teqc config files from ~/confiles/config-*
and extracts the RINEX header metadata that can be added to station.cfg.

Usage:
    python parse_teqc_configs.py [--config-dir DIR] [--output FILE] [--update-stations]
"""

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import configparser
import json


@dataclass
class TeqcConfig:
    """Parsed teqc config data."""

    station: str
    run_by: Optional[str] = None
    observer: Optional[str] = None
    agency: Optional[str] = None
    marker_name: Optional[str] = None
    marker_number: Optional[str] = None
    antenna_serial: Optional[str] = None
    antenna_type: Optional[str] = None
    antenna_radome: Optional[str] = None
    antenna_height: float = 0.0
    antenna_east: float = 0.0
    antenna_north: float = 0.0
    file_mtime: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON/config output."""
        return {
            'station': self.station,
            'rinex_run_by': self.run_by,
            'rinex_observer': self.observer,
            'rinex_agency': self.agency,
            'rinex_marker_name': self.marker_name,
            'rinex_marker_number': self.marker_number,
            'antenna_serial': self.antenna_serial,
            'antenna_type': self.antenna_type,
            'antenna_radome': self.antenna_radome,
            'antenna_height': self.antenna_height,
            'antenna_east': self.antenna_east,
            'antenna_north': self.antenna_north,
            'config_modified': self.file_mtime,
        }

    def to_station_cfg_lines(self) -> list[str]:
        """Generate lines for station.cfg format."""
        lines = []
        if self.run_by:
            lines.append(f"rinex_run_by = {self.run_by}")
        if self.observer:
            lines.append(f"rinex_observer = {self.observer}")
        if self.agency:
            lines.append(f"rinex_agency = {self.agency}")
        if self.marker_name:
            lines.append(f"rinex_marker_name = {self.marker_name}")
        if self.marker_number:
            lines.append(f"rinex_marker_number = {self.marker_number}")
        if self.antenna_serial:
            lines.append(f"antenna_serial = {self.antenna_serial}")
        if self.antenna_type:
            lines.append(f"antenna_type = {self.antenna_type}")
        if self.antenna_radome:
            lines.append(f"antenna_radome = {self.antenna_radome}")
        if self.antenna_height != 0.0:
            lines.append(f"antenna_height = {self.antenna_height}")
        if self.antenna_east != 0.0:
            lines.append(f"antenna_east = {self.antenna_east}")
        if self.antenna_north != 0.0:
            lines.append(f"antenna_north = {self.antenna_north}")
        return lines


def parse_teqc_config(config_path: Path) -> Optional[TeqcConfig]:
    """Parse a teqc config file.

    Teqc config format example:
        -O.r[un_by] "BGO"
        -O.o[perator] "HMF/BGO"
        -O.ag[ency] "JH/IMO"
        -O.mo[nument] "THOB"
        -O.mn "THOB"
        -O.an "0000"
        -O.at "SEPCHOKE_B3E6   SPKE"
        -O.pe[hEN,m] 0.0000 0.0000 0.0000
    """
    # Extract station name from filename (config-stationname)
    station = config_path.name.replace('config-', '').upper()

    config = TeqcConfig(station=station)

    try:
        content = config_path.read_text()

        # Get file modification time
        stat = config_path.stat()
        from datetime import datetime
        config.file_mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d')

        # Parse each line
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            # Run by: -O.r[un_by] "value"
            if '-O.r' in line:
                match = re.search(r'"([^"]*)"', line)
                if match:
                    config.run_by = match.group(1)

            # Observer/Operator: -O.o[perator] "value"
            elif '-O.o' in line:
                match = re.search(r'"([^"]*)"', line)
                if match:
                    config.observer = match.group(1)

            # Agency: -O.ag[ency] "value"
            elif '-O.ag' in line:
                match = re.search(r'"([^"]*)"', line)
                if match:
                    config.agency = match.group(1)

            # Marker name (monument): -O.mo[nument] "value"
            elif '-O.mo' in line:
                match = re.search(r'"([^"]*)"', line)
                if match:
                    config.marker_name = match.group(1)

            # Marker number: -O.mn "value" or -O.mn[nument] "value"
            elif '-O.mn' in line:
                match = re.search(r'"([^"]*)"', line)
                if match:
                    config.marker_number = match.group(1)

            # Antenna serial: -O.an "value"
            elif '-O.an' in line:
                match = re.search(r'"([^"]*)"', line)
                if match:
                    config.antenna_serial = match.group(1)

            # Antenna type: -O.at "MODEL   RADOME" (20 chars model, 4 chars radome)
            elif '-O.at' in line:
                match = re.search(r'"([^"]*)"', line)
                if match:
                    ant_str = match.group(1)
                    # IGS format: 20 chars antenna model, then radome
                    # But often just separated by whitespace
                    parts = ant_str.split()
                    if parts:
                        config.antenna_type = parts[0]
                        if len(parts) > 1:
                            config.antenna_radome = parts[1]
                        else:
                            config.antenna_radome = 'NONE'

            # Antenna offsets: -O.pe[hEN,m] h e n
            elif '-O.pe' in line:
                # Extract the three float values
                numbers = re.findall(r'[-+]?\d*\.?\d+', line)
                if len(numbers) >= 3:
                    config.antenna_height = float(numbers[0])
                    config.antenna_east = float(numbers[1])
                    config.antenna_north = float(numbers[2])

        return config

    except Exception as e:
        print(f"Error parsing {config_path}: {e}")
        return None


def parse_all_configs(config_dir: Path) -> list[TeqcConfig]:
    """Parse all teqc config files in directory."""
    configs = []

    # Find all config files (exclude backups with ~)
    config_files = sorted(config_dir.glob('config-*'))
    config_files = [f for f in config_files if not f.name.endswith('~')]

    for config_path in config_files:
        config = parse_teqc_config(config_path)
        if config:
            configs.append(config)

    return configs


def generate_report(configs: list[TeqcConfig]) -> str:
    """Generate a summary report of the parsed configs."""
    lines = [
        "# RINEX Metadata from Teqc Configs",
        f"Total stations: {len(configs)}",
        "",
        "## Antenna Types Summary",
    ]

    # Count antenna types
    antenna_counts: dict[str, int] = {}
    for c in configs:
        ant = f"{c.antenna_type} {c.antenna_radome}" if c.antenna_type else "Unknown"
        antenna_counts[ant] = antenna_counts.get(ant, 0) + 1

    for ant, count in sorted(antenna_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {ant}: {count}")

    lines.extend([
        "",
        "## Stations with non-zero antenna height",
    ])

    for c in configs:
        if c.antenna_height != 0.0:
            lines.append(f"  {c.station}: h={c.antenna_height}m")

    lines.extend([
        "",
        "## Agency Summary",
    ])

    agency_counts: dict[str, int] = {}
    for c in configs:
        agency = c.agency or "Unknown"
        agency_counts[agency] = agency_counts.get(agency, 0) + 1

    for agency, count in sorted(agency_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {agency}: {count}")

    return "\n".join(lines)


def update_stations_cfg(
    configs: list[TeqcConfig],
    stations_cfg_path: Path,
    output_path: Optional[Path] = None,
) -> None:
    """Update station.cfg with RINEX metadata from teqc configs.

    Args:
        configs: List of parsed teqc configs
        stations_cfg_path: Path to existing station.cfg
        output_path: Path to write updated config (if None, prints to stdout)
    """
    # Create lookup by station
    config_by_station = {c.station: c for c in configs}

    # Read existing station.cfg
    parser = configparser.ConfigParser()
    parser.read(stations_cfg_path)

    # Track updates
    updated = 0
    not_found = []

    for section in parser.sections():
        station = section.upper()
        if station in config_by_station:
            teqc = config_by_station[station]

            # Add RINEX metadata fields
            if teqc.run_by:
                parser.set(section, 'rinex_run_by', teqc.run_by)
            if teqc.observer:
                parser.set(section, 'rinex_observer', teqc.observer)
            if teqc.agency:
                parser.set(section, 'rinex_agency', teqc.agency)
            if teqc.marker_name:
                parser.set(section, 'rinex_marker_name', teqc.marker_name)
            if teqc.marker_number:
                parser.set(section, 'rinex_marker_number', teqc.marker_number)
            if teqc.antenna_serial:
                parser.set(section, 'antenna_serial', teqc.antenna_serial)
            if teqc.antenna_type:
                parser.set(section, 'antenna_type', teqc.antenna_type)
            if teqc.antenna_radome:
                parser.set(section, 'antenna_radome', teqc.antenna_radome)
            if teqc.antenna_height != 0.0:
                parser.set(section, 'antenna_height', str(teqc.antenna_height))
            if teqc.antenna_east != 0.0:
                parser.set(section, 'antenna_east', str(teqc.antenna_east))
            if teqc.antenna_north != 0.0:
                parser.set(section, 'antenna_north', str(teqc.antenna_north))
            if teqc.file_mtime:
                parser.set(section, 'rinex_config_valid_from', teqc.file_mtime)

            updated += 1
        else:
            not_found.append(station)

    # Write output
    if output_path:
        with open(output_path, 'w') as f:
            parser.write(f)
        print(f"Updated {updated} stations, wrote to {output_path}")
    else:
        import io
        output = io.StringIO()
        parser.write(output)
        print(output.getvalue())

    if not_found:
        print(f"\nStations in station.cfg without teqc config: {len(not_found)}")
        print(f"  {', '.join(not_found[:20])}{'...' if len(not_found) > 20 else ''}")


def main():
    parser = argparse.ArgumentParser(description='Parse teqc config files')
    parser.add_argument(
        '--config-dir',
        type=Path,
        default=Path(__file__).parent.parent / 'docs/legacy_scripts/teqc_configs',
        help='Directory containing teqc config files',
    )
    parser.add_argument(
        '--output',
        type=Path,
        help='Output file for JSON report',
    )
    parser.add_argument(
        '--report',
        action='store_true',
        help='Print summary report',
    )
    parser.add_argument(
        '--update-stations',
        type=Path,
        help='Path to station.cfg to update',
    )
    parser.add_argument(
        '--output-stations',
        type=Path,
        help='Output path for updated station.cfg',
    )

    args = parser.parse_args()

    # Parse all configs
    configs = parse_all_configs(args.config_dir)
    print(f"Parsed {len(configs)} teqc config files")

    # Generate report
    if args.report:
        print(generate_report(configs))

    # Output JSON
    if args.output:
        data = [c.to_dict() for c in configs]
        with open(args.output, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"Wrote JSON to {args.output}")

    # Update station.cfg
    if args.update_stations:
        update_stations_cfg(configs, args.update_stations, args.output_stations)


if __name__ == '__main__':
    main()
