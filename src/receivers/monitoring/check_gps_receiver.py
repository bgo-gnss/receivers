#!/usr/bin/env python3
"""Icinga/Nagios plugin for GPS receiver health monitoring.

Nagios Plugin Exit Codes:
  0 - OK
  1 - WARNING
  2 - CRITICAL
  3 - UNKNOWN

Performance Data Format:
  'label'=value[UOM];[warn];[crit];[min];[max]

Usage:
  check_gps_receiver.py --station ELDC
  check_gps_receiver.py --station ELDC --save-db
"""

import argparse
import json
import sys
from pathlib import Path

# Add receivers to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from base.receiver_factory import create_receiver
from config_utils import get_station_config

# Nagios plugin exit codes
NAGIOS_OK = 0
NAGIOS_WARNING = 1
NAGIOS_CRITICAL = 2
NAGIOS_UNKNOWN = 3


def get_exit_code(overall_status: str) -> int:
    """Map health status to Nagios exit code.

    Args:
        overall_status: Health status (healthy, warning, critical, unknown)

    Returns:
        Nagios exit code
    """
    status_map = {
        "healthy": NAGIOS_OK,
        "ok": NAGIOS_OK,
        "warning": NAGIOS_WARNING,
        "critical": NAGIOS_CRITICAL,
        "error": NAGIOS_CRITICAL,
        "unknown": NAGIOS_UNKNOWN,
    }
    return status_map.get(overall_status.lower(), NAGIOS_UNKNOWN)


def format_performance_data(health: dict) -> str:
    """Format performance data for Nagios.

    Args:
        health: Health data dictionary

    Returns:
        Performance data string
    """
    perf_data = []

    # Extract metrics
    metrics = health.get("metrics", {})

    # Voltage
    if "power" in metrics:
        voltage = metrics["power"].get("voltage")
        if voltage is not None:
            # Format: voltage=12.3V;11.5;11.0;10;15
            perf_data.append(f"voltage={voltage}V;11.5;11.0;10;15")

    # Temperature
    if "temperature" in metrics:
        temp = metrics["temperature"].get("value")
        if temp is not None:
            # Format: temperature=45.2C;60;70;0;100
            perf_data.append(f"temperature={temp}C;60;70;0;100")

    # CPU load
    if "cpu_load" in metrics:
        cpu = metrics["cpu_load"].get("percent")
        if cpu is not None:
            # Format: cpu_load=25%;75;90;0;100
            perf_data.append(f"cpu_load={cpu}%;75;90;0;100")

    # Disk usage
    data_quality = health.get("data_quality", {})
    if "disk_usage" in data_quality:
        disk_pct = data_quality["disk_usage"].get("usage_percent")
        if disk_pct is not None:
            # Format: disk_usage=44.6%;80;90;0;100
            perf_data.append(f"disk_usage={disk_pct}%;90;97;0;100")

    # Response time from connection
    connection = health.get("connection", {})
    if "router_ping" in connection:
        latency = connection["router_ping"].get("latency_ms")
        if latency is not None:
            # Format: ping_latency=5.2ms;100;500;0;1000
            perf_data.append(f"ping_latency={latency}ms;100;500;0;1000")

    return " ".join(perf_data)


def format_status_message(health: dict) -> str:
    """Format status message for Nagios.

    Args:
        health: Health data dictionary

    Returns:
        Status message string
    """
    station_id = health.get("station_id", "UNKNOWN")
    overall_status = health.get("overall_status", "unknown").upper()

    # Build message with key issues
    issues = []
    connection = health.get("connection", {})

    # Check connection levels
    for level, data in connection.items():
        if data.get("status") != "ok":
            issues.append(f"{level}:{data.get('status', 'unknown')}")

    # Check critical metrics
    metrics = health.get("metrics", {})
    if "power" in metrics and metrics["power"].get("status") != "ok":
        voltage = metrics["power"].get("voltage", "N/A")
        issues.append(f"voltage:{voltage}V")

    if "temperature" in metrics and metrics["temperature"].get("status") != "ok":
        temp = metrics["temperature"].get("value", "N/A")
        issues.append(f"temp:{temp}C")

    # Build status message
    if issues:
        return f"{station_id} {overall_status} - {', '.join(issues)}"
    else:
        return f"{station_id} {overall_status} - All checks OK"


def main():
    """Main plugin execution."""
    parser = argparse.ArgumentParser(
        description="Icinga/Nagios plugin for GPS receiver health monitoring"
    )
    parser.add_argument("--station", required=True, help="Station ID to check")
    parser.add_argument(
        "--save-db", action="store_true", help="Save health data to database"
    )
    parser.add_argument(
        "--save-json", action="store_true", help="Save health data to JSON file"
    )
    parser.add_argument(
        "--timeout", type=int, default=30, help="Timeout in seconds (default: 30)"
    )

    args = parser.parse_args()

    try:
        station_id = args.station.upper()

        # Get station configuration
        station_config = get_station_config(station_id)
        if station_config is None:
            print(f"UNKNOWN - Station {station_id} not found in configuration")
            sys.exit(NAGIOS_UNKNOWN)

        # Create receiver and get health status
        receiver = create_receiver(station_id, station_config)
        health = receiver.get_health_status()

        # Save to database if requested
        if args.save_db:
            receiver.save_health_to_database(health)

        # Save to JSON if requested
        if args.save_json:
            receiver.save_health_to_json(health)

        # Format output for Nagios
        status_message = format_status_message(health)
        perf_data = format_performance_data(health)

        # Output format: STATUS_MESSAGE | PERFORMANCE_DATA
        if perf_data:
            print(f"{status_message} | {perf_data}")
        else:
            print(status_message)

        # Exit with appropriate code
        exit_code = get_exit_code(health.get("overall_status", "unknown"))
        sys.exit(exit_code)

    except Exception as e:
        print(f"UNKNOWN - Health check failed: {str(e)}")
        sys.exit(NAGIOS_UNKNOWN)


if __name__ == "__main__":
    main()
