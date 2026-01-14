#!/usr/bin/env python3
"""
Plot receiver voltage timeseries from extracted health JSON data.

Usage:
    python plot_voltage.py ISFS
    python plot_voltage.py ISFS --days 7
    python plot_voltage.py ISFS --date 20260113
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd


def find_health_json(station: str, date: datetime, base_path: str = '/tmp/gpsdata') -> Path:
    """Find health JSON file for a station and date."""
    year = date.strftime('%Y')
    month = date.strftime('%b').lower()
    filename = f"{station}_{date.strftime('%Y%m%d')}_health.json"

    json_path = Path(base_path) / year / month / station / 'status_1hr' / 'json' / filename
    return json_path


def load_voltage_timeseries(json_path: Path) -> pd.DataFrame:
    """Load voltage timeseries from health JSON."""
    with open(json_path, 'r') as f:
        data = json.load(f)

    timeseries = data.get('timeseries', [])

    # Extract voltage samples
    voltage_data = []
    for sample in timeseries:
        if 'voltage' in sample and 'time' in sample:
            voltage_data.append({
                'datetime': pd.to_datetime(sample['time']),
                'voltage': sample['voltage']['value']
            })

    if not voltage_data:
        return pd.DataFrame()

    df = pd.DataFrame(voltage_data)
    df.set_index('datetime', inplace=True)
    return df


def plot_voltage(station: str, dfs: list, dates: list, output_file: str = None):
    """Create voltage plot."""
    # Combine all dataframes
    all_data = pd.concat(dfs) if dfs else pd.DataFrame()

    if all_data.empty:
        print(f"No voltage data found for {station}")
        return

    all_data = all_data.sort_index()

    # Create figure
    fig, ax = plt.subplots(figsize=(12, 5))

    # Plot voltage
    ax.plot(all_data.index, all_data['voltage'], 'b-', linewidth=0.8, alpha=0.8)
    ax.scatter(all_data.index, all_data['voltage'], s=3, c='blue', alpha=0.5)

    # Formatting
    ax.set_xlabel('Date/Time (UTC)')
    ax.set_ylabel('Voltage (V)')
    ax.set_title(f'{station} Receiver Input Voltage')

    # Date formatting
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()

    # Add grid
    ax.grid(True, alpha=0.3)

    # Add statistics annotation
    stats_text = f"Mean: {all_data['voltage'].mean():.2f}V\n"
    stats_text += f"Min: {all_data['voltage'].min():.2f}V\n"
    stats_text += f"Max: {all_data['voltage'].max():.2f}V\n"
    stats_text += f"Samples: {len(all_data)}"

    ax.text(0.02, 0.98, stats_text, transform=ax.transAxes,
            fontsize=9, verticalalignment='top',
            bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

    # Set y-axis limits with some padding
    ymin, ymax = all_data['voltage'].min(), all_data['voltage'].max()
    padding = (ymax - ymin) * 0.1 or 0.5
    ax.set_ylim(ymin - padding, ymax + padding)

    plt.tight_layout()

    if output_file:
        plt.savefig(output_file, dpi=150, bbox_inches='tight')
        print(f"Saved plot to {output_file}")
    else:
        plt.show()


def main():
    parser = argparse.ArgumentParser(description='Plot receiver voltage timeseries')
    parser.add_argument('station', help='Station ID (e.g., ISFS)')
    parser.add_argument('--days', type=int, default=1, help='Number of days to plot (default: 1)')
    parser.add_argument('--date', help='Specific date (YYYYMMDD), defaults to today')
    parser.add_argument('--base-path', default='/tmp/gpsdata', help='Base data path')
    parser.add_argument('-o', '--output', help='Output file (e.g., voltage.png)')

    args = parser.parse_args()

    station = args.station.upper()

    # Parse date
    if args.date:
        end_date = datetime.strptime(args.date, '%Y%m%d')
    else:
        end_date = datetime.now()

    start_date = end_date - timedelta(days=args.days - 1)

    print(f"Loading voltage data for {station}")
    print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

    # Load data for each day
    dfs = []
    dates = []
    current = start_date

    while current <= end_date:
        json_path = find_health_json(station, current, args.base_path)

        if json_path.exists():
            print(f"  Loading {json_path.name}...")
            df = load_voltage_timeseries(json_path)
            if not df.empty:
                dfs.append(df)
                dates.append(current)
        else:
            print(f"  No data for {current.strftime('%Y-%m-%d')}")

        current += timedelta(days=1)

    if not dfs:
        print(f"No health JSON files found for {station}")
        print(f"Run: receivers health {station} --extract-day YYYYMMDD")
        return

    total_samples = sum(len(df) for df in dfs)
    print(f"Loaded {total_samples} voltage samples from {len(dfs)} days")

    # Plot
    output_file = args.output
    if not output_file:
        output_file = f"{station}_voltage.png"

    plot_voltage(station, dfs, dates, output_file)


if __name__ == '__main__':
    main()
