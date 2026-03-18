#!/usr/bin/env python3
"""Test health data extraction from SBF files using updated RxToolsExtractor."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from receivers.health.rxtools_extractor import RxToolsExtractor, RxToolsNotFoundError


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_health_extraction.py <sbf_file>")
        print("\nExample:")
        print("  python test_health_extraction.py ~/.gpsdata/septentrio/ISFS/status_1hr/ISFS251112.sbf.gz")
        sys.exit(1)

    sbf_file = Path(sys.argv[1])

    if not sbf_file.exists():
        print(f"Error: File not found: {sbf_file}")
        sys.exit(1)

    print(f"Testing health extraction from: {sbf_file}\n")

    # Initialize extractor
    extractor = RxToolsExtractor(station_id="TEST")

    # Check RxTools availability
    if not extractor.check_rxtools_available():
        print("❌ RxTools bin2asc not found")
        print("Please install RxTools: https://www.septentrio.com/")
        sys.exit(1)

    print("✅ RxTools bin2asc found\n")

    try:
        # Extract health data
        print("Extracting health data...")
        health_data = extractor.extract_health_from_sbf(sbf_file)

        # Display results
        print("\n" + "="*60)
        print("HEALTH DATA EXTRACTION RESULTS")
        print("="*60)

        print(f"\nExtraction time: {health_data.get('extraction_time')}")

        # Metrics
        if health_data.get('metrics'):
            print("\n📊 METRICS:")
            metrics = health_data['metrics']

            if 'power' in metrics:
                power = metrics['power']
                status_emoji = '✅' if power['status'] == 'ok' else '⚠️' if power['status'] == 'warning' else '❌'
                print(f"  {status_emoji} Voltage: {power['voltage']} {power['unit']} [{power['status']}]")
                if 'timestamp' in power:
                    print(f"      Timestamp: {power['timestamp']}")

            if 'cpu_load' in metrics:
                cpu = metrics['cpu_load']
                status_emoji = '✅' if cpu['status'] == 'ok' else '⚠️' if cpu['status'] == 'warning' else '❌'
                print(f"  {status_emoji} CPU Load: {cpu['percent']}% [{cpu['status']}]")

            if 'temperature' in metrics:
                temp = metrics['temperature']
                status_emoji = '✅' if temp['status'] == 'ok' else '⚠️' if temp['status'] == 'warning' else '❌'
                print(f"  {status_emoji} Temperature: {temp['value']} {temp['unit']} [{temp['status']}]")

        # Data Quality
        if health_data.get('data_quality'):
            print("\n📈 DATA QUALITY:")
            dq = health_data['data_quality']

            if 'disk_usage' in dq:
                disk = dq['disk_usage']
                status_emoji = '✅' if disk['status'] == 'ok' else '⚠️' if disk['status'] == 'warning' else '❌'
                print(f"  {status_emoji} Disk Usage: {disk['usage_percent']}% [{disk['status']}]")

            if 'tracked_satellites' in dq:
                print(f"  📡 Tracked Satellites: {dq['tracked_satellites']}")

        # Receiver Specific
        if health_data.get('receiver_specific'):
            print("\n🔧 RECEIVER SPECIFIC:")
            rs = health_data['receiver_specific']

            if 'uptime_seconds' in rs:
                uptime_hours = rs['uptime_seconds'] / 3600
                uptime_days = uptime_hours / 24
                print(f"  ⏱️  Uptime: {rs['uptime_seconds']}s ({uptime_hours:.1f}h / {uptime_days:.1f}d)")

        print("\n" + "="*60)
        print("✅ Health extraction completed successfully")
        print("="*60)

    except RxToolsNotFoundError as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\n❌ Error: {e}")
        print("\nTraceback:")
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
