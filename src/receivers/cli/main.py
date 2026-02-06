#!/usr/bin/env python3
"""
receivers CLI - GPS Receiver Data Management Tool

Enhanced command-line interface for downloading and managing GPS receiver data.
Migrated from getSeptentrio3 with modern subcommand architecture.

Usage:
    receivers download STATION [STATION...] [OPTIONS]
    receivers status STATION
    receivers health STATION
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import gtimes.timefunc as gt
from gtimes.timefunc import currDatetime

from ..base.exceptions import ConfigurationError, ConnectionError
from ..base.type_validator import ReceiverTypeValidator
from ..base.receiver_factory import get_receiver_factory, create_receiver
from ..utils.time_utils import calculate_download_time_range, generate_period_ranges

# Import gps_parser for centralized config
try:
    import sys

    sys.path.append("../gps_parser/src")
    import gps_parser

    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False
    gps_parser = None

# Import station config from utility to avoid circular imports
from ..config_utils import get_station_config


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Set up logging for CLI commands."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("receivers")


def parse_datetime(date_str: str) -> datetime:
    """Parse datetime string in format YYYYMMDD-HHMM or YYYYMMDD."""
    if "-" in date_str:
        return datetime.strptime(date_str, "%Y%m%d-%H%M")
    else:
        return datetime.strptime(date_str, "%Y%m%d")


# get_station_config function moved to config_utils.py to avoid circular imports


def _validate_station_for_download(
    station_id: str, logger: logging.Logger, session: str = None
) -> Optional[Any]:
    """Validate station config and return (receiver, station_config) or None on failure.

    Args:
        station_id: Station identifier
        logger: Logger instance
        session: Session type to validate (e.g., '15s_24hr', 'status_1hr')

    Returns:
        Receiver instance if valid, None if station should be skipped
    """
    station_config = get_station_config(station_id)
    if station_config is None:
        logger.warning(
            f"⚠️  Station {station_id} not found in configuration - SKIPPING"
        )
        return None

    try:
        ip = station_config["router"]["ip"]
        # Accept either FTP or HTTP port — Trimble NetR9/NetR5 use HTTP downloads
        port = (
            station_config["receiver"].get("ftpport")
            or station_config["receiver"].get("httpport")
        )
        if not ip or not port:
            logger.warning(
                f"⚠️  Station {station_id} missing IP ({ip}) or port ({port}) - SKIPPING"
            )
            return None
    except KeyError as e:
        logger.warning(
            f"⚠️  Station {station_id} configuration missing required key {e} - SKIPPING"
        )
        return None

    # Check if session is supported by this receiver type
    if session:
        from ..config.receivers_config import get_receivers_config
        receivers_config = get_receivers_config()
        receiver_type = station_config.get("receiver_type", "").lower()
        if not receivers_config.is_session_supported_by_receiver(receiver_type, session):
            supported = receivers_config.get_supported_sessions(receiver_type)
            logger.info(
                f"⏭️  Skipping {station_id}: {session} not supported for {receiver_type} "
                f"(supported: {', '.join(supported) or 'none'})"
            )
            return None

    receiver = create_receiver(station_id, station_config)
    return receiver


def _download_station_period(
    receiver,
    station_id: str,
    start: datetime,
    end: datetime,
    args,
    logger: logging.Logger,
    audit_logger=None,
    ffrequency: str = "",
    afrequency: str = "",
    reverse_chronological: bool = False,
) -> tuple:
    """Download a single period for one station.

    Returns:
        Tuple of (files_downloaded, errors).
    """
    files_downloaded = 0
    errors = 0

    try:
        # Test connection if requested
        if args.test_connection:
            status = receiver.get_connection_status()
            if not status.get("receiver"):
                logger.error(
                    f"Connection test failed for {station_id}: {status.get('error')}"
                )
                return 0, 1
            logger.info(f"Connection test successful for {station_id}")

        # Download data
        result = receiver.download_data(
            start=start,
            end=end,
            session=args.session,
            ffrequency=ffrequency,
            afrequency=afrequency,
            compression=args.compression,
            sync=args.sync,
            clean_tmp=args.clean_tmp,
            archive=args.archive,
            reverse_chronological=reverse_chronological,
            loglevel=args.loglevel,
        )

        # Report results
        files_downloaded = result.get("files_downloaded", 0)

        # Log to audit trail if production logging enabled
        if audit_logger:
            audit_logger.log_download_session(
                station_id,
                {
                    "session": args.session,
                    "status": result.get("status", "unknown"),
                    "duration": result.get("duration", 0),
                    "files_downloaded": files_downloaded,
                    "bytes_downloaded": result.get("total_bytes", 0),
                    "errors": result.get("errors", 0),
                    "start_time": start.isoformat() if start else None,
                    "end_time": end.isoformat() if end else None,
                    "connection_time": getattr(
                        receiver, "_last_connection_time", None
                    ),
                },
            )

        logger.info(f"Station {station_id}: {files_downloaded} files downloaded")
        logger.info(
            f"Status: {result.get('status')}, Duration: {result.get('duration', 0):.2f}s"
        )

        if files_downloaded > 0:
            logger.info("Downloaded files:")
            for file_path in result.get("downloaded_files", []):
                logger.info(f"  - {file_path}")

    except (ConfigurationError, ConnectionError) as e:
        logger.error(f"Error processing {station_id}: {e}")
        errors += 1
    except Exception as e:
        import traceback

        logger.error(f"Unexpected error processing {station_id}: {e}")
        logger.debug(f"Traceback:\n{traceback.format_exc()}")
        errors += 1

    return files_downloaded, errors


def cmd_download(args) -> int:
    """Download command - main data download functionality."""

    # Set up production logging if requested
    if getattr(args, "production", False) or getattr(args, "json_log", False):
        from ..base.production_logging import setup_production_logging

        production_config = setup_production_logging(
            json_output=getattr(args, "json_log", False),
            verbose=(args.loglevel == logging.DEBUG),
        )
        logger = production_config.create_station_logger("receivers")
        audit_logger = production_config.get_audit_logger()
    else:
        logger = setup_logging(args.loglevel)
        audit_logger = None

    logger.info(f"Starting download for stations: {args.stations}")

    # Process time arguments (from getSeptentrio3 logic)
    start_time = None
    end_time = None
    reverse_chronological = False  # Default for explicit --start/--end

    if args.start:
        start_time = parse_datetime(args.start)

    if args.end:
        end_time = parse_datetime(args.end)

    # Default to time periods back if no start/end specified (use shared time_utils)
    if not start_time and args.days:
        # -D flag used: prioritize latest data (reverse chronological)
        reverse_chronological = True

        # Use shared time utility - single source of truth for time calculation
        # This implements correct "previous complete period" logic
        start_time, end_time = calculate_download_time_range(
            session_type=args.session, lookback_periods=args.days
        )

    # If explicit --start or --end provided, honor them
    if args.start and not end_time:
        # User provided start but no end - calculate reasonable end
        if args.session and "1hr" in args.session:
            end_time = start_time + timedelta(hours=1)
        else:
            end_time = start_time + timedelta(days=1)

    if args.end and not start_time:
        # User provided end but no start - calculate reasonable start
        if args.session and "1hr" in args.session:
            start_time = end_time - timedelta(hours=1)
        else:
            start_time = end_time - timedelta(days=1)

    # Process session frequency arguments (from getSeptentrio3)
    afrequency = args.afrequency or args.session.split("_")[0]
    ffrequency = args.ffrequency or args.session.split("_")[1]

    # Convert frequency to gtimes format
    frequency_mapping = {
        "24hr": "1D",  # Daily
        "1hr": "1H",  # Hourly
    }
    ffrequency = frequency_mapping.get(ffrequency, ffrequency)

    logger.info(f"Time range: {start_time} to {end_time}")
    logger.info(
        f"Session: {args.session}, File frequency: {ffrequency}, Acquisition frequency: {afrequency}"
    )

    # Download for each station
    total_downloaded = 0
    total_errors = 0

    # Network-first mode: when using -d (reverse_chronological) with multiple stations,
    # iterate day→station so all stations get the latest data first.
    network_first = reverse_chronological and len(args.stations) > 1

    if network_first:
        # Pre-validate all stations upfront
        receivers_map: Dict[str, Any] = {}
        for sid in args.stations:
            sid = sid.upper()
            receiver = _validate_station_for_download(sid, logger, session=args.session)
            if receiver is None:
                total_errors += 1
                continue
            receivers_map[sid] = receiver

        if not receivers_map:
            logger.error("No valid stations to download")
            return 1

        # Outer: periods (newest first), Inner: stations
        periods = generate_period_ranges(
            start_time, end_time, args.session, reverse=True
        )
        for period_start, period_end in periods:
            if args.session == '15s_24hr':
                logger.info(f"--- {period_start.strftime('%Y-%m-%d')} ---")
            else:
                logger.info(f"--- {period_start.strftime('%Y-%m-%d %H:%M')} ---")
            for sid, receiver in receivers_map.items():
                logger.info(f"Processing station: {sid}")
                dl, err = _download_station_period(
                    receiver, sid, period_start, period_end,
                    args, logger, audit_logger,
                    ffrequency=ffrequency, afrequency=afrequency,
                    reverse_chronological=False,  # single period, no need to reverse
                )
                total_downloaded += dl
                total_errors += err
    else:
        # Station-first: current behavior (single station or explicit -s/-e range)
        for station_id in args.stations:
            station_id = station_id.upper()
            logger.info(f"Processing station: {station_id}")

            receiver = _validate_station_for_download(station_id, logger, session=args.session)
            if receiver is None:
                total_errors += 1
                continue

            dl, err = _download_station_period(
                receiver, station_id, start_time, end_time,
                args, logger, audit_logger,
                ffrequency=ffrequency, afrequency=afrequency,
                reverse_chronological=reverse_chronological,
            )
            total_downloaded += dl
            total_errors += err

    # Final summary
    logger.info(
        f"Download complete. Total files: {total_downloaded}, Errors: {total_errors}"
    )
    return 0 if total_errors == 0 else 1


def cmd_status(args) -> int:
    """Status command - thin wrapper around ``health --compact``.

    Provides the same compact output as before by delegating to cmd_health
    with appropriate defaults set.
    """
    # Set health-command defaults that status doesn't expose
    args.compact = True
    args.no_files = False
    args.no_ntrip = False
    # Date flags — status is live-only
    args.start = None
    args.end = None
    args.days = None
    args.extract_all = False
    args.import_json = False
    args.export_json = False
    args.save_json = False
    args.skip_blocks = True
    args.force = False
    return cmd_health(args)


def _send_status_to_icinga(
    results: list,
    logger: logging.Logger,
    save_to_db: bool = True
) -> int:
    """Send status check results to Icinga monitoring system.

    Also saves health data to database for Grafana dashboards (same data,
    single extraction).

    Args:
        results: List of (health_data, station_config) tuples
        logger: Logger instance
        save_to_db: Also save health data to database (default: True)

    Returns:
        0 if all checks sent successfully, 1 otherwise
    """
    try:
        from ..monitoring.icinga_client import IcingaClient
    except ImportError:
        logger.error("❌ Icinga client not available. Install requests: pip install requests")
        return 1

    # Initialize database writer if saving to DB
    db_writer = None
    if save_to_db:
        try:
            from ..health.db_writer import HealthDatabaseWriter
            db_writer = HealthDatabaseWriter()
            if not db_writer.connect():
                logger.warning("Could not connect to database, skipping DB storage")
                db_writer = None
        except Exception as e:
            logger.warning(f"Database not available: {e}")
            db_writer = None

    client = IcingaClient()
    all_success = True
    has_critical = False

    for health, station_config in results:
        station_id = health.get("station_id", "UNKNOWN")

        # Track critical status
        if health.get("overall_status") == "critical":
            has_critical = True

        # Save to database (same data used for Icinga and Grafana)
        if db_writer:
            try:
                db_writer.write_health_data(health)
                logger.debug(f"Saved health data to database for {station_id}")
            except Exception as e:
                logger.warning(f"Failed to save to database for {station_id}: {e}")

        # Send all health-based checks to Icinga
        try:
            responses = client.send_health_from_json(health)

            # Print results
            print(f"\n=== Icinga results for {station_id} ===")
            for check_name, response in responses.items():
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} {check_name}: sent (HTTP {code})")
                else:
                    print(f"{status} {check_name}: FAILED - {response.get('message', 'Unknown error')}")
                    all_success = False

        except Exception as e:
            logger.error(f"Failed to send checks for {station_id}: {e}")
            all_success = False

        # Send file status checks (check archive file system)
        try:
            from ..health.file_tracker import ArchiveFileChecker
            checker = ArchiveFileChecker()

            # Check daily files (15s_24hr)
            stats = checker.check_file_status(station_id, "15s_24hr", days_back=7)
            if stats:
                response = client.send_download_check(
                    station=station_id,
                    session_type="15s_24hr",
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 7),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(0, stats.get("files_expected", 0) - stats.get("files_found", 0)),
                    error_count=0,
                    warn_hours=26.0,  # Warning after 26 hours for daily files
                    crit_hours=50.0,  # Critical after 50 hours
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 15s_24hr file status: sent (HTTP {code})")
                else:
                    print(f"{status} 15s_24hr file status: FAILED - {response.get('message', 'Unknown error')}")

            # Check hourly files (1Hz_1hr)
            stats = checker.check_file_status(station_id, "1Hz_1hr", days_back=1)
            if stats and stats.get("files_found", 0) > 0:
                response = client.send_download_check(
                    station=station_id,
                    session_type="1Hz_1hr",
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 24),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(0, stats.get("files_expected", 0) - stats.get("files_found", 0)),
                    error_count=0,
                    warn_hours=2.0,   # Warning after 2 hours for hourly files
                    crit_hours=4.0,   # Critical after 4 hours
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 1Hz_1hr file status: sent (HTTP {code})")
                else:
                    print(f"{status} 1Hz_1hr file status: FAILED - {response.get('message', 'Unknown error')}")

            # Check daily RINEX files (15s_24hr_rinex) - only if directory exists
            stats = checker.check_file_status(station_id, "15s_24hr_rinex", days_back=7)
            if stats and stats.get("dir_exists", False):
                response = client.send_download_check(
                    station=station_id,
                    session_type="15s_24hr rinex",  # Icinga service name with space
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 7),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(0, stats.get("files_expected", 0) - stats.get("files_found", 0)),
                    error_count=0,
                    warn_hours=26.0,
                    crit_hours=50.0,
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 15s_24hr rinex file status: sent (HTTP {code})")
                else:
                    print(f"{status} 15s_24hr rinex file status: FAILED - {response.get('message', 'Unknown error')}")

            # Check hourly RINEX files (1Hz_1hr_rinex) - only if directory exists
            stats = checker.check_file_status(station_id, "1Hz_1hr_rinex", days_back=1)
            if stats and stats.get("dir_exists", False) and stats.get("files_found", 0) > 0:
                response = client.send_download_check(
                    station=station_id,
                    session_type="1Hz_1hr rinex",  # Icinga service name with space
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 24),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(0, stats.get("files_expected", 0) - stats.get("files_found", 0)),
                    error_count=0,
                    warn_hours=2.0,
                    crit_hours=4.0,
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 1Hz_1hr rinex file status: sent (HTTP {code})")
                else:
                    print(f"{status} 1Hz_1hr rinex file status: FAILED - {response.get('message', 'Unknown error')}")

            # Check 20Hz hourly files - only if directory exists
            stats = checker.check_file_status(station_id, "20Hz_1hr", days_back=1)
            if stats and stats.get("dir_exists", False) and stats.get("files_found", 0) > 0:
                response = client.send_download_check(
                    station=station_id,
                    session_type="20Hz_1hr",
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 24),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(0, stats.get("files_expected", 0) - stats.get("files_found", 0)),
                    error_count=0,
                    warn_hours=2.0,
                    crit_hours=4.0,
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 20Hz_1hr file status: sent (HTTP {code})")
                else:
                    print(f"{status} 20Hz_1hr file status: FAILED - {response.get('message', 'Unknown error')}")

            # Check 50Hz hourly files - only if directory exists
            stats = checker.check_file_status(station_id, "50Hz_1hr", days_back=1)
            if stats and stats.get("dir_exists", False) and stats.get("files_found", 0) > 0:
                response = client.send_download_check(
                    station=station_id,
                    session_type="50Hz_1hr",
                    latest_download=stats.get("latest_mtime"),
                    hours_since_download=stats.get("hours_since_file"),
                    downloads_expected=stats.get("files_expected", 24),
                    downloads_successful=stats.get("files_found", 0),
                    downloads_missing=max(0, stats.get("files_expected", 0) - stats.get("files_found", 0)),
                    error_count=0,
                    warn_hours=2.0,
                    crit_hours=4.0,
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 50Hz_1hr file status: sent (HTTP {code})")
                else:
                    print(f"{status} 50Hz_1hr file status: FAILED - {response.get('message', 'Unknown error')}")

        except Exception as e:
            logger.debug(f"Could not send file status checks for {station_id}: {e}")

        # Send 24hr processing status check
        try:
            from ..health.file_tracker import ProcessingStatusChecker
            proc_checker = ProcessingStatusChecker()
            result = proc_checker.check_24hr_processing(station_id)

            if result.get("file_exists", False):
                response = client.send_processing_check(
                    station=station_id,
                    check_name="24hr processing status",
                    status=result.get("status", "unknown"),
                    message=result.get("message", "Unknown status"),
                    days_behind=result.get("days_behind"),
                    latest_date=result.get("latest_date"),
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} 24hr processing status: sent (HTTP {code})")
                else:
                    print(f"{status} 24hr processing status: FAILED - {response.get('message', 'Unknown error')}")
        except Exception as e:
            logger.debug(f"Could not send processing status for {station_id}: {e}")

        # Send RTK status check (NTRIP stream health)
        try:
            from ..monitoring.ntrip_client import check_ntrip_status, NTRIPConfig
            from ..config.receivers_config import ReceiversConfig

            receivers_cfg = ReceiversConfig()
            ntrip_status = check_ntrip_status(
                station_id=station_id,
                receivers_config=receivers_cfg,
                station_config=station_config,
            )

            if ntrip_status:
                response = client.send_rtk_check(
                    station=station_id,
                    ntrip_status=ntrip_status,
                )
                status = "✅" if response.get("success") else "❌"
                code = response.get("code", "N/A")
                if response.get("success"):
                    print(f"{status} rtk status: sent (HTTP {code})")
                else:
                    print(f"{status} rtk status: FAILED - {response.get('message', 'Unknown error')}")
        except Exception as e:
            logger.debug(f"Could not send RTK status for {station_id}: {e}")

    # Close database connection
    if db_writer:
        db_writer.close()

    return 0 if all_success and not has_critical else 1


def _print_quick_status(health: Dict[str, Any], station_config: Dict[str, Any]) -> None:
    """Print compact status output for quick checks.

    Args:
        health: Health status dictionary from get_health_status()
        station_config: Station configuration dictionary
    """
    station_id = health.get("station_id", "UNKN")
    receiver_type = health.get("receiver_type", "Unknown")
    overall = health.get("overall_status", "unknown")

    # Get IP from station config (try common locations)
    ip = (
        station_config.get("ip")
        or station_config.get("router", {}).get("ip")
        or station_config.get("host")
        or "N/A"
    )

    # Overall status with emoji
    status_emoji = {
        "healthy": "✅",
        "warning": "⚠️",
        "critical": "❌",
        "unknown": "❓",
    }

    # Header line: station, type, IP, overall status
    print(f"{station_id} ({receiver_type}) @ {ip}  {status_emoji.get(overall, '❓')} {overall.upper()}")

    # Port status from metrics.ports (aligns with Icinga Receiver status check)
    metrics = health.get("metrics", {})
    ports = metrics.get("ports", {})
    if ports:
        port_parts = []
        for port_name in ["ftp", "http", "control"]:
            port_data = ports.get(port_name, {})
            if isinstance(port_data, dict) and "open" in port_data:
                is_open = port_data.get("open", False)
                port_num = port_data.get("port", "?")
                if is_open:
                    port_parts.append(f"{port_name}:{port_num} ✅")
                else:
                    port_parts.append(f"{port_name}:{port_num} closed")
            else:
                port_parts.append(f"{port_name}: N/A")
        if port_parts:
            print(f"  Ports: {' | '.join(port_parts)}")
    else:
        # Fallback to connection status if no port data
        connection = health.get("connection", {})
        conn_parts = []
        for level, data in connection.items():
            status = data.get("status", "unknown")
            emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
            conn_parts.append(f"{level}:{emoji}")
        if conn_parts:
            print(f"  Connection: {' '.join(conn_parts)}")

    # Key metrics on one line each
    metrics = health.get("metrics", {})
    if metrics:
        metric_lines = []

        # Power/Voltage
        power = metrics.get("power", {})
        if power:
            voltage = power.get("voltage")
            if voltage is not None:
                status = power.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                metric_lines.append(f"Voltage: {emoji} {voltage:.2f} V")

        # Temperature
        temp = metrics.get("temperature", {})
        if temp:
            value = temp.get("value")
            if value is not None:
                status = temp.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                metric_lines.append(f"Temp: {emoji} {value}°C")

        # CPU load
        cpu = metrics.get("cpu_load", {})
        if cpu:
            value = cpu.get("value", cpu.get("percent"))
            if value is not None:
                status = cpu.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                metric_lines.append(f"CPU: {emoji} {value}%")

        # Disk usage
        disk = metrics.get("disk", {})
        if disk:
            value = disk.get("usage_percent")
            if value is not None:
                status = disk.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                metric_lines.append(f"Disk: {emoji} {value:.0f}%")

        if metric_lines:
            print(f"  Metrics: {' | '.join(metric_lines)}")

        # Satellites (sent to Icinga as "Satellite status")
        sats = metrics.get("satellites", {})
        if sats:
            total = sats.get("total")
            if total is not None:
                status = sats.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                by_const = sats.get("by_constellation", {})
                const_str = ", ".join(f"{k}:{v}" for k, v in by_const.items()) if by_const else ""
                if const_str:
                    print(f"  Satellites: {emoji} {total} ({const_str})")
                else:
                    print(f"  Satellites: {emoji} {total}")

        # Position (sent to Icinga as "Station position")
        pos = metrics.get("position", {})
        if pos:
            fix_mode = pos.get("fix_mode") or pos.get("fix_type")
            if fix_mode:
                status = pos.get("status", "ok")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                sats_used = pos.get("satellites_used", "")
                lat = pos.get("latitude")
                lon = pos.get("longitude")
                height = pos.get("height")
                pos_str = f"{fix_mode}"
                if sats_used:
                    pos_str += f", {sats_used} sats"
                if lat is not None and lon is not None:
                    pos_str += f" @ {lat:.5f}, {lon:.5f}"
                    if height is not None:
                        pos_str += f", {height:.1f}m"
                print(f"  Position: {emoji} {pos_str}")

            # DOP values (Trimble provides PDOP/HDOP/VDOP/TDOP)
            pdop = pos.get("pdop")
            if pdop is not None:
                hdop = pos.get("hdop")
                vdop = pos.get("vdop")
                dop_parts = [f"PDOP:{pdop:.1f}"]
                if hdop is not None:
                    dop_parts.append(f"HDOP:{hdop:.1f}")
                if vdop is not None:
                    dop_parts.append(f"VDOP:{vdop:.1f}")
                print(f"  DOP: {' '.join(dop_parts)}")

        # Uptime
        uptime = metrics.get("uptime", {})
        if uptime and uptime.get("available") is not False:
            formatted = uptime.get("formatted")
            if formatted:
                print(f"  Uptime: {formatted}")

    # Logging status (sent to Icinga as "Logging status")
    data_quality = health.get("data_quality", {})
    disk_status = data_quality.get("disk", {})
    if disk_status:
        status = disk_status.get("status", "unknown")
        if status != "unknown":
            emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
            print(f"  Logging: {emoji} {status}")

    # RTK status (NTRIP stream health)
    rtk_status = health.get("rtk", {})
    if rtk_status:
        status = rtk_status.get("status", "unknown")
        message = rtk_status.get("message", "")
        if status != "unknown":
            emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
            print(f"  RTK: {emoji} {message}")

    # File status checks
    file_status = health.get("file_status", {})
    if file_status:
        file_parts = []
        # Order: raw files first, then RINEX, then high-rate
        session_order = ["15s_24hr", "1Hz_1hr", "15s_24hr_rinex", "1Hz_1hr_rinex", "20Hz_1hr", "50Hz_1hr"]
        session_labels = {
            "15s_24hr": "15s",
            "1Hz_1hr": "1Hz",
            "15s_24hr_rinex": "15s-rnx",
            "1Hz_1hr_rinex": "1Hz-rnx",
            "20Hz_1hr": "20Hz",
            "50Hz_1hr": "50Hz",
        }
        # Get thresholds from icinga config
        try:
            from ..config.icinga_config import get_icinga_config
            icinga_thresholds = get_icinga_config().get_thresholds()
            daily_warn = icinga_thresholds.file_daily_warning_hours
            daily_crit = icinga_thresholds.file_daily_critical_hours
            hourly_warn = icinga_thresholds.file_hourly_warning_hours
            hourly_crit = icinga_thresholds.file_hourly_critical_hours
        except Exception:
            daily_warn, daily_crit = 26.0, 50.0
            hourly_warn, hourly_crit = 2.0, 4.0

        thresholds = {
            "15s_24hr": (daily_warn, daily_crit),
            "1Hz_1hr": (hourly_warn, hourly_crit),
            "15s_24hr_rinex": (daily_warn, daily_crit),
            "1Hz_1hr_rinex": (hourly_warn, hourly_crit),
            "20Hz_1hr": (hourly_warn, hourly_crit),
            "50Hz_1hr": (hourly_warn, hourly_crit),
        }
        for session in session_order:
            stats = file_status.get(session)
            if stats:
                hours = stats.get("hours_since_file")
                warn_h, crit_h = thresholds.get(session, (26, 50))
                if hours is None:
                    status = "critical"
                elif hours >= crit_h:
                    status = "critical"
                elif hours >= warn_h:
                    status = "warning"
                else:
                    status = "ok"
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                label = session_labels.get(session, session)
                file_parts.append(f"{label}:{emoji}")
        if file_parts:
            print(f"  Files: {' '.join(file_parts)}")

    # 24hr processing status
    proc_status = health.get("processing_24hr", {})
    if proc_status:
        status = proc_status.get("status", "unknown")
        message = proc_status.get("message", "")
        if status != "unknown":
            emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
            # Shorten the message for display
            short_msg = message.replace("24hr processing ", "").replace("OK - ", "").replace("CRITICAL - ", "")
            print(f"  Processing: {emoji} {short_msg}")


def cmd_health_timeseries_extract(
    args, station_id: str, station_config: Dict[str, Any], logger: logging.Logger
) -> int:
    """Extract time-series health data from SBF files and save to JSON.

    Args:
        args: Command-line arguments
        station_id: Station identifier
        station_config: Station configuration dictionary
        logger: Logger instance

    Returns:
        0 on success, 1 on failure
    """
    from ..health.timeseries_extractor import TimeSeriesHealthExtractor
    from ..health.json_writer import HealthJSONWriter

    try:
        # Parse dates to extract using unified flags: -s/--start, -e/--end, -d/--days
        # For status_1hr (hourly session), -d counts hours not days (consistent with download)
        from ..utils.time_utils import calculate_download_time_range

        dates_to_extract = []
        start_date = None
        end_date = None
        session_type = "status_1hr"  # Health extraction uses status_1hr session
        is_hourly = True  # status_1hr is hourly data

        # Handle -d/--days: N periods back (hours for hourly, days for daily)
        if getattr(args, "days", None):
            # Use same time range calculation as download command for consistency
            start_time, end_time = calculate_download_time_range(
                session_type=session_type,
                lookback_periods=args.days
            )
            # Convert to dates for directory-based extraction
            # For hourly data, we need unique dates that span the hour range
            start_date = start_time.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            end_date = end_time.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            # Make sure we include end_date's day if end_time has hours
            if end_time.hour > 0 or end_time == start_time:
                pass  # end_date is correct
            logger.info(f"Period range: {args.days} {'hours' if is_hourly else 'days'} "
                       f"({start_time.strftime('%Y-%m-%d %H:%M')} to {end_time.strftime('%Y-%m-%d %H:%M')})")

        # Handle -s/--start
        if getattr(args, "start", None):
            try:
                start_date = datetime.strptime(args.start, "%Y%m%d")
            except ValueError:
                logger.error(
                    f"Invalid start date format: {args.start} (expected YYYYMMDD)"
                )
                return 1

        # Handle -e/--end
        if getattr(args, "end", None):
            try:
                end_date = datetime.strptime(args.end, "%Y%m%d")
            except ValueError:
                logger.error(f"Invalid end date format: {args.end} (expected YYYYMMDD)")
                return 1

        # If only start provided, end = start (single day)
        if start_date and not end_date:
            end_date = start_date

        # If only end provided, start = end (single day)
        if end_date and not start_date:
            start_date = end_date

        # Handle --extract-all
        if getattr(args, "extract_all", False):
            logger.error(
                "--extract-all not yet implemented (requires scanning archive directory)"
            )
            return 1

        # Validate we have dates
        if not start_date or not end_date:
            logger.error(
                "No dates to extract. Use -s DATE, -s START -e END, or -d DAYS"
            )
            return 1

        if start_date > end_date:
            logger.error("Start date must be before or equal to end date")
            return 1

        # Generate list of dates
        current_date = start_date
        while current_date <= end_date:
            dates_to_extract.append(current_date)
            current_date += timedelta(days=1)

        logger.info(
            f"Extracting data for {len(dates_to_extract)} days: "
            f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        )

        # Get receiver type
        receiver_type = station_config.get("receiver", {}).get("type", "PolaRX5")

        # Get data paths from receivers_config
        from ..config.receivers_config import get_receivers_config

        receivers_config = get_receivers_config()
        data_prepath = receivers_config.get_prepath()

        logger.debug(f"Using data_prepath: {data_prepath}")
        logger.debug(f"Receiver type: {receiver_type}")

        # Initialize extractors
        extractor = TimeSeriesHealthExtractor(station_id, receiver_type)

        # Process each date
        success_count = 0
        skip_count = 0
        error_count = 0

        # Initialize file tracker for import tracking
        from ..health import FileTracker, compute_checksum
        file_tracker = FileTracker()
        tracker_connected = file_tracker.connect()

        today = datetime.now().date()

        for date in dates_to_extract:
            try:
                # Check if already imported (skip if --save-db and already in DB, unless --force)
                # For hourly data, never skip today since new hours keep arriving
                if getattr(args, "save_db", False) and tracker_connected and not getattr(args, "force", False):
                    # Convert datetime to date for tracking check
                    check_date = date.date() if hasattr(date, 'date') else date
                    # Don't skip today for hourly sessions - new data may have arrived
                    is_today = check_date == today
                    if not is_today and file_tracker.is_health_imported(station_id, check_date):
                        logger.info(f"⏭️  Skipping {date.strftime('%Y-%m-%d')} (already imported to database)")
                        skip_count += 1
                        continue
                    elif is_today:
                        logger.debug(f"Re-processing today ({date.strftime('%Y-%m-%d')}) for latest hourly data")

                # Build path to status_1hr directory for this date
                year = date.strftime("%Y")
                month = date.strftime("%b").lower()
                base_path = Path(data_prepath) / year / month

                # Check both status_1hr/ and status_1hr/raw/ subdirectories
                status_dir = base_path / station_id / "status_1hr"
                status_raw_dir = status_dir / "raw"

                if not status_dir.exists():
                    logger.warning(
                        f"Status directory not found for {date.strftime('%Y-%m-%d')}: {status_dir}"
                    )
                    error_count += 1
                    continue

                # Find all SBF files for this date
                date_str = date.strftime("%Y%m%d")
                sbf_files = []

                # Look for hourly status files: STATION{YYYYMMDD}{HH}00c.sbf.gz
                # Try both status_1hr/ and status_1hr/raw/ subdirectories
                search_dirs = [status_dir]
                if status_raw_dir.exists():
                    search_dirs.append(status_raw_dir)

                for hour in range(24):
                    filename = f"{station_id}{date_str}{hour:02d}00c.sbf.gz"

                    # Check each potential directory
                    for search_dir in search_dirs:
                        filepath = search_dir / filename
                        if filepath.exists() and filepath not in sbf_files:
                            sbf_files.append(filepath)
                            break  # Found in this dir, don't check others

                if not sbf_files:
                    logger.warning(
                        f"No SBF files found for {date.strftime('%Y-%m-%d')} in {status_dir}"
                    )
                    error_count += 1
                    continue

                logger.info(
                    f"Found {len(sbf_files)} SBF files for {date.strftime('%Y-%m-%d')}"
                )

                # Extract daily health data
                health_data = extractor.extract_daily_health(sbf_files, date)

                # Write to JSON
                json_writer = HealthJSONWriter(str(base_path), station_id)
                force = getattr(args, "force", False)

                json_path = json_writer.write_daily_health_data(
                    health_data, date, force=force
                )

                if json_path:
                    logger.info(f"✅ Wrote {json_path.name}")

                    # Update latest symlink
                    json_writer.write_daily_latest_symlink(json_path)

                    # Extract per-block JSONs for exploration (unless disabled)
                    if not getattr(args, "skip_blocks", False):
                        logger.info(f"Extracting per-block JSONs for exploration...")
                        from ..health.block_json_writer import BlockJsonWriter

                        block_writer = BlockJsonWriter(station_id, status_dir / "json")
                        try:
                            block_stats = block_writer.extract_all_blocks(
                                sbf_files, date.date()
                            )
                            if block_stats:
                                logger.info(
                                    f"✅ Extracted {len(block_stats)} block types:"
                                )
                                for block_name, count in block_stats.items():
                                    logger.info(f"   - {block_name}: {count} samples")
                            else:
                                logger.debug("No additional blocks found")
                        except Exception as e:
                            logger.warning(f"Per-block extraction failed: {e}")
                            if args.loglevel == logging.DEBUG:
                                import traceback

                                traceback.print_exc()

                    success_count += 1
                else:
                    # JSON was skipped (no source changes)
                    # But if --save-db is requested, we need to check if data is in DB
                    check_date = date.date() if hasattr(date, 'date') else date
                    db_has_data = tracker_connected and file_tracker.is_health_imported(station_id, check_date)

                    if getattr(args, "save_db", False) and not db_has_data:
                        # JSON exists but DB is missing data - import from existing JSON
                        logger.info(f"📥 JSON exists but DB missing - importing from existing JSON for {date.strftime('%Y-%m-%d')}")

                        # Find and load the existing JSON file
                        date_str = date.strftime("%Y%m%d")
                        json_dir = base_path / station_id / "status_1hr" / "json"
                        existing_json = json_dir / f"{station_id}_{date_str}_health.json"

                        if existing_json.exists():
                            try:
                                import json
                                with open(existing_json) as f:
                                    health_data = json.load(f)
                                logger.debug(f"Loaded existing JSON: {existing_json}")
                            except Exception as e:
                                logger.warning(f"Failed to load existing JSON: {e}")
                                health_data = None
                        else:
                            logger.warning(f"Expected JSON not found: {existing_json}")
                            health_data = None

                        # Will be imported in the save_db block below
                        success_count += 1
                    else:
                        logger.info(
                            f"⏭️  Skipped {date.strftime('%Y-%m-%d')} (no source changes, data in DB: {db_has_data})"
                        )
                        skip_count += 1
                        health_data = None  # Don't re-import to DB

                # Save to database if requested
                if getattr(args, "save_db", False) and health_data:
                    try:
                        from ..health.json_importer import HealthJsonImporter

                        with HealthJsonImporter() as importer:
                            if importer.connect(database="gps_health"):
                                rows_imported = importer.import_health_data(
                                    health_data, station_id, receiver_type
                                )
                                logger.info(f"💾 Saved {rows_imported} rows to database for {date.strftime('%Y-%m-%d')}")

                                # Mark as imported in file tracker
                                if tracker_connected and rows_imported > 0:
                                    checksum = compute_checksum(health_data)
                                    # Convert datetime to date for tracking
                                    track_date = date.date() if hasattr(date, 'date') else date
                                    file_tracker.mark_health_imported(
                                        station_id, track_date, rows_imported, checksum, str(json_path) if json_path else None
                                    )
                            else:
                                logger.warning("Failed to connect to database")
                    except Exception as e:
                        logger.warning(f"Database save failed: {e}")

            except Exception as e:
                logger.error(f"Failed to extract {date.strftime('%Y-%m-%d')}: {e}")
                if args.loglevel == logging.DEBUG:
                    import traceback

                    traceback.print_exc()
                error_count += 1

        # Cleanup file tracker
        file_tracker.close()

        # Summary
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Extraction complete:")
        logger.info(f"  ✅ Extracted: {success_count}")
        logger.info(f"  ⏭️  Skipped: {skip_count}")
        logger.info(f"  ❌ Errors: {error_count}")
        logger.info(f"{'=' * 60}")

        return 0 if error_count == 0 else 1

    except Exception as e:
        logger.error(f"Time-series extraction failed: {e}")
        if args.loglevel == logging.DEBUG:
            import traceback

            traceback.print_exc()
        return 1


def cmd_health_json_import(args, station_id: str, logger: logging.Logger) -> int:
    """Import JSON health files to database.

    Args:
        args: Command-line arguments
        station_id: Station identifier
        logger: Logger instance

    Returns:
        0 on success, 1 on failure
    """
    from pathlib import Path
    from ..health.json_importer import HealthJsonImporter

    # Determine JSON directory
    if getattr(args, "json_dir", None):
        json_dir = Path(args.json_dir)
    else:
        # Auto-detect from data path
        from ..config.receivers_config import get_receivers_config
        from datetime import datetime

        config = get_receivers_config()
        data_prepath = config.get_data_prepath()

        now = datetime.now()
        month_abbr = now.strftime("%b").lower()
        year = now.strftime("%Y")

        json_dir = Path(data_prepath) / year / month_abbr / station_id / "status_1hr" / "json"

    if not json_dir.exists():
        logger.error(f"JSON directory not found: {json_dir}")
        return 1

    logger.info(f"Importing JSON health data for {station_id} from {json_dir}")

    # Parse date filters
    start_date = None
    end_date = None

    if getattr(args, "start", None):
        try:
            start_date = datetime.strptime(args.start, "%Y%m%d")
        except ValueError:
            logger.error(f"Invalid start date format: {args.start}")
            return 1

    if getattr(args, "end", None):
        try:
            end_date = datetime.strptime(args.end, "%Y%m%d")
        except ValueError:
            logger.error(f"Invalid end date format: {args.end}")
            return 1

    # Import
    try:
        with HealthJsonImporter() as importer:
            if not importer.connect(database="gps_health"):
                logger.error("Failed to connect to database")
                return 1

            files, rows, skipped = importer.import_directory(
                json_dir,
                station_id=station_id,
                start_date=start_date,
                end_date=end_date,
            )

            logger.info(f"Import complete: {files} files, {rows} rows imported, {skipped} skipped")
            return 0

    except Exception as e:
        logger.error(f"Import failed: {e}")
        return 1


def cmd_health_json_export(args, station_id: str, logger: logging.Logger) -> int:
    """Export health data from database to JSON files.

    Args:
        args: Command-line arguments
        station_id: Station identifier
        logger: Logger instance

    Returns:
        0 on success, 1 on failure
    """
    from pathlib import Path
    from datetime import datetime
    from ..health.json_importer import HealthJsonImporter

    # Determine output directory
    if getattr(args, "json_dir", None):
        output_dir = Path(args.json_dir)
    else:
        # Auto-detect from data path
        from ..config.receivers_config import get_receivers_config

        config = get_receivers_config()
        data_prepath = config.get_data_prepath()

        now = datetime.now()
        month_abbr = now.strftime("%b").lower()
        year = now.strftime("%Y")

        output_dir = Path(data_prepath) / year / month_abbr / station_id / "status_1hr" / "json"

    # Parse date filters
    start_date = None
    end_date = None

    if getattr(args, "start", None):
        try:
            start_date = datetime.strptime(args.start, "%Y%m%d")
        except ValueError:
            logger.error(f"Invalid start date format: {args.start}")
            return 1

    if getattr(args, "end", None):
        try:
            end_date = datetime.strptime(args.end, "%Y%m%d")
        except ValueError:
            logger.error(f"Invalid end date format: {args.end}")
            return 1

    logger.info(f"Exporting health data for {station_id} to {output_dir}")

    # Export
    try:
        with HealthJsonImporter() as exporter:
            if not exporter.connect(database="gps_health"):
                logger.error("Failed to connect to database")
                return 1

            files, rows = exporter.export_to_json(
                output_dir,
                station_id=station_id,
                start_date=start_date,
                end_date=end_date,
            )

            logger.info(f"Export complete: {files} files, {rows} rows exported")
            return 0

    except Exception as e:
        logger.error(f"Export failed: {e}")
        return 1


def cmd_health_single(args, station_id: str, logger: logging.Logger) -> int:
    """Process health command for a single station.

    Args:
        args: Command-line arguments
        station_id: Station identifier
        logger: Logger instance

    Returns:
        0 on success, 1 on failure
    """
    try:
        # Check if JSON export requested (database -> JSON)
        if getattr(args, "export_json", False):
            return cmd_health_json_export(args, station_id, logger)

        # Check if JSON import requested (JSON -> database)
        if getattr(args, "import_json", False):
            return cmd_health_json_import(args, station_id, logger)

        station_config = get_station_config(station_id)
        if station_config is None:
            logger.warning(f"⚠️  Station {station_id} not found in configuration")
            return 1

        # Check if time-series extraction requested (any date flag triggers extraction)
        if any(
            [
                getattr(args, "start", None),
                getattr(args, "end", None),
                getattr(args, "days", None),
                getattr(args, "extract_all", False),
            ]
        ):
            return cmd_health_timeseries_extract(
                args, station_id, station_config, logger
            )

        # --- Live mode (no date flags) ---
        # Create receiver instance using factory pattern
        receiver = create_receiver(station_id, station_config)

        # Get comprehensive health status (including files + NTRIP)
        from ..health.live_health import gather_comprehensive_health

        include_files = not getattr(args, "no_files", False)
        include_ntrip = not getattr(args, "no_ntrip", False)

        health = gather_comprehensive_health(
            station_id, station_config, receiver,
            include_files=include_files,
            include_ntrip=include_ntrip,
        )

        # Save to JSON if requested
        if getattr(args, "save_json", False):
            json_path = receiver.save_health_to_json(health)
            if json_path:
                logger.info(f"Saved health data to {json_path}")

        # Save to database if requested
        if getattr(args, "save_db", False):
            success = receiver.save_health_to_database(health)
            if success:
                logger.info("Saved health data to database")
            else:
                logger.warning("Failed to save health data to database")

        # Return health + config for Icinga/compact handling in cmd_health
        # Store on args for the caller to collect
        if not hasattr(args, "_health_results"):
            args._health_results = []
        args._health_results.append((health, station_config))

        # Compact output mode (used by 'status' command wrapper)
        if getattr(args, "compact", False):
            # Output handled by cmd_health after collecting all stations
            return 0

        # Icinga output mode
        if getattr(args, "icinga", False):
            # Output handled by cmd_health after collecting all stations
            return 0

        # Output format
        if getattr(args, "json", False):
            # JSON output
            import json

            print(json.dumps(health, indent=2, default=str))
        else:
            # Human-readable output (detailed)
            print(f"Station: {health['station_id']}")
            print(f"Receiver Type: {health['receiver_type']}")
            print(f"Timestamp: {health.get('timestamp', 'N/A')}")
            print(f"Overall Status: {health.get('overall_status', 'unknown').upper()}")

            # Connection summary (TCP + ports)
            connection = health.get("connection", {})
            metrics = health.get("metrics", {})
            ports = metrics.get("ports", {})

            print(f"\nConnection Health:")
            # Show TCP status
            if "tcp" in connection:
                tcp = connection["tcp"]
                status = tcp.get("status", "unknown")
                host = tcp.get("host", "")
                emoji = "✅" if status == "ok" else "⚠️" if status == "warning" else "❌"
                print(f"  tcp: {emoji} {status} ({host})")

            # Show port status (always show all ports for consistency, N/A if not configured)
            for port_name in ["http", "ftp", "control"]:
                if port_name in ports:
                    port_data = ports[port_name]
                    port_num = port_data.get("port", "?")
                    is_open = port_data.get("open", False)
                    if is_open:
                        print(f"  {port_name}: ✅ port {port_num}")
                    else:
                        print(f"  {port_name}: ❌ port {port_num} closed")
                else:
                    print(f"  {port_name}: N/A")

            # Metrics summary
            if metrics:
                print(f"\nMetrics:")
                # Power
                if "power" in metrics:
                    power = metrics["power"]
                    voltage = power.get("voltage", "N/A")
                    status = power.get("status", "unknown")
                    print(f"  power: {voltage} V [{status}]")

                # CPU
                if "cpu_load" in metrics:
                    cpu = metrics["cpu_load"]
                    percent = cpu.get("percent", "N/A")
                    status = cpu.get("status", "unknown")
                    print(f"  cpu_load: {percent}% [{status}]")

                # Temperature
                if "temperature" in metrics:
                    temp = metrics["temperature"]
                    value = temp.get("value", "N/A")
                    status = temp.get("status", "unknown")
                    print(f"  temperature: {value} C [{status}]")

                # Disk usage
                disk = metrics.get("disk", {})
                if disk and disk.get("available") is not False:
                    usage = disk.get("usage_percent")
                    if usage is not None:
                        total = disk.get("total_mb", 0)
                        free = disk.get("free_mb", 0)
                        status = disk.get("status", "unknown")
                        print(f"  disk: {usage:.1f}% used ({free:.0f} MB free / {total:.0f} MB total) [{status}]")

                # Position
                if "position" in metrics:
                    pos = metrics["position"]
                    lat = pos.get("latitude")
                    lon = pos.get("longitude")
                    height = pos.get("height")
                    # Handle both PolaRX5 (fix_mode) and Trimble (fix_type) formats
                    fix = pos.get("fix_mode") or pos.get("fix_type", "unknown")
                    status = pos.get("status", "unknown")
                    if lat is not None and lon is not None:
                        if height is not None:
                            print(f"  position: {lat:.6f}, {lon:.6f}, {height:.1f}m ({fix}) [{status}]")
                        else:
                            print(f"  position: {lat:.6f}, {lon:.6f} ({fix}) [{status}]")
                    else:
                        print(f"  position: N/A [{status}]")

                    # DOP values (Trimble provides PDOP/HDOP/VDOP/TDOP)
                    pdop = pos.get("pdop")
                    if pdop is not None:
                        dop_parts = [f"PDOP={pdop:.1f}"]
                        for k in ["hdop", "vdop", "tdop"]:
                            v = pos.get(k)
                            if v is not None:
                                dop_parts.append(f"{k.upper()}={v:.1f}")
                        print(f"  dop: {', '.join(dop_parts)}")

                # Uptime
                uptime = metrics.get("uptime", {})
                if uptime and uptime.get("available") is not False:
                    formatted = uptime.get("formatted")
                    seconds = uptime.get("seconds")
                    if formatted:
                        print(f"  uptime: {formatted}")
                    elif seconds is not None:
                        days = seconds // 86400
                        hours = (seconds % 86400) // 3600
                        print(f"  uptime: {days}d {hours}h")

                # System info (serial number, antenna)
                system = metrics.get("system", {})
                if system:
                    parts = []
                    if system.get("serial_number"):
                        parts.append(f"SN:{system['serial_number']}")
                    if system.get("antenna_type"):
                        parts.append(f"Ant:{system['antenna_type']}")
                    if parts:
                        print(f"  system: {', '.join(parts)}")

                # Satellites
                if "satellites" in metrics:
                    sats = metrics["satellites"]
                    total = sats.get("total", 0)
                    by_const = sats.get("by_constellation", {})
                    status = sats.get("status", "unknown")
                    # Build constellation summary
                    const_parts = []
                    for const in ["GPS", "GLONASS", "Galileo", "BeiDou", "SBAS"]:
                        count = by_const.get(const)
                        if count:
                            const_parts.append(f"{const}:{count}")
                    const_str = ", ".join(const_parts) if const_parts else ""
                    if const_str:
                        print(f"  satellites: {total} ({const_str}) [{status}]")
                    else:
                        print(f"  satellites: {total} [{status}]")

            # Status summary
            summary = health.get("status_summary", {})
            if summary:
                print(f"\nStatus Summary:")
                print(f"  Healthy: {summary.get('healthy', 0)}")
                print(f"  Warning: {summary.get('warning', 0)}")
                print(f"  Critical: {summary.get('critical', 0)}")

        return 0

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        return 1


def cmd_health(args) -> int:
    """Health command - get receiver health information for one or more stations."""
    logger = setup_logging(args.loglevel)

    # Initialize results collector for compact/icinga modes
    args._health_results = []

    # Get list of stations (now supports multiple)
    stations = [s.upper() for s in args.stations]

    if len(stations) > 1:
        logger.info(f"Processing {len(stations)} stations: {', '.join(stations)}")

    # Determine if this is a timeseries extraction with -d flag (network-first candidate)
    has_date_flags = any([
        getattr(args, "start", None),
        getattr(args, "end", None),
        getattr(args, "days", None),
        getattr(args, "extract_all", False),
    ])
    # Network-first: -d with multiple stations → iterate day→station
    use_days = getattr(args, "days", None)
    network_first = (
        has_date_flags
        and use_days is not None
        and not getattr(args, "start", None)
        and len(stations) > 1
    )

    # Track results
    success_count = 0
    error_count = 0

    if network_first:
        # Network-first health extraction: day→station ordering
        # Calculate the date range, then iterate per-day across all stations
        from ..utils.time_utils import calculate_download_time_range as _calc_range
        session_type = "status_1hr"
        start_time, end_time = _calc_range(
            session_type=session_type, lookback_periods=use_days
        )
        # Convert to dates
        start_date = start_time.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        end_date = end_time.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

        # Generate day list (newest first)
        dates = []
        current = start_date
        while current <= end_date:
            dates.append(current)
            current += timedelta(days=1)
        dates.reverse()  # newest first

        logger.info(f"Network-first mode: {len(dates)} days x {len(stations)} stations")

        for date in dates:
            logger.info(f"\n--- {date.strftime('%Y-%m-%d')} ---")
            # Create single-day args for each station
            day_str = date.strftime("%Y%m%d")
            for station_id in stations:
                logger.info(f"Processing station: {station_id}")
                # Create a copy of args scoped to this single day
                import copy
                day_args = copy.copy(args)
                day_args.start = day_str
                day_args.end = day_str
                day_args.days = None  # Use start/end instead
                day_args._health_results = args._health_results  # Share collector

                result = cmd_health_single(day_args, station_id, logger)
                if result == 0:
                    success_count += 1
                else:
                    error_count += 1
    else:
        # Station-first: current behavior
        for station_id in stations:
            if len(stations) > 1:
                logger.info(f"\n{'='*60}")
                logger.info(f"Processing station: {station_id}")
                logger.info(f"{'='*60}")

            result = cmd_health_single(args, station_id, logger)

            if result == 0:
                success_count += 1
            else:
                error_count += 1

    # --- Post-collection output for compact/icinga modes ---
    results = args._health_results

    if results and getattr(args, "icinga", False):
        return _send_status_to_icinga(results, logger)

    if results and getattr(args, "compact", False):
        has_critical = False
        # JSON output if requested alongside compact
        if getattr(args, "json", False):
            import json
            if len(results) == 1:
                print(json.dumps(results[0][0], indent=2, default=str))
            else:
                print(json.dumps([r[0] for r in results], indent=2, default=str))
        else:
            for health, station_config in results:
                _print_quick_status(health, station_config)
                if health.get("overall_status") == "critical":
                    has_critical = True
        return 1 if has_critical else 0

    # Summary for multiple stations
    if len(stations) > 1:
        logger.info(f"\n{'='*60}")
        logger.info(f"Summary: {success_count} succeeded, {error_count} failed")
        logger.info(f"{'='*60}")

    return 0 if error_count == 0 else 1


def cmd_validate_web_accuracy(args) -> int:
    """Validate configuration accuracy using web interface scraping."""
    import sys
    from pathlib import Path

    # Add the receivers root directory to path
    receivers_root = Path(__file__).parent.parent.parent.parent
    sys.path.insert(0, str(receivers_root))

    try:
        from config_accuracy_validator import ConfigAccuracyValidator

        validator = ConfigAccuracyValidator()

        # Get stations to validate
        if args.stations:
            station_ids = [s.upper() for s in args.stations]
        else:
            # Get all configured stations
            station_configs = get_all_station_configs()
            station_ids = list(station_configs.keys())

        if not station_ids:
            print("❌ No stations found to validate")
            return 1

        # Run web accuracy validation
        results, summary = validator.validate_multiple_stations(station_ids)

        if args.summary:
            # Show summary only
            print(f"\n📊 CONFIGURATION ACCURACY SUMMARY")
            print(f"{'=' * 50}")
            print(f"Total stations: {summary['total_stations']}")
            print(f"✅ Correct configs: {summary['correct_configs']}")
            print(f"❌ Mismatched configs: {summary['mismatched_configs']}")
            print(f"⚠️  Unverifiable: {summary['unverifiable_configs']}")

            if summary["type_mismatches"]:
                print(f"\n🔧 Type mismatches: {', '.join(summary['type_mismatches'])}")

            if summary["name_mismatches"]:
                print(f"🏷️  Name mismatches: {', '.join(summary['name_mismatches'])}")

            accuracy_rate = summary["correct_configs"] / summary["total_stations"] * 100
            print(f"\n📈 Configuration accuracy: {accuracy_rate:.1f}%")
        else:
            # Show detailed results
            print(f"\n📋 DETAILED CONFIGURATION VALIDATION")
            print(f"{'=' * 60}")

            for station_id, result in results.items():
                print(f"\n🏢 {station_id}:")
                print(f"   IP: {result['ip']}")

                if result["status"] == "error":
                    print(f"   ❌ ERROR: {result['error']}")
                    continue

                if result.get("actual_type"):
                    type_status = "✅" if result.get("type_match", True) else "❌"
                    print(
                        f"   {type_status} Type: {result['configured_type']} vs {result['actual_type']}"
                    )

                if result.get("actual_station_name"):
                    name_status = "✅" if result.get("name_match", True) else "❌"
                    print(
                        f"   {name_status} Name: {station_id} vs {result['actual_station_name']}"
                    )

                if result.get("type_mismatch"):
                    print(f"      🔧 Fix: {result['type_mismatch']['suggested_fix']}")

                if result.get("name_mismatch"):
                    print(f"      🔧 Fix: {result['name_mismatch']['suggested_fix']}")

        return 0 if summary["mismatched_configs"] == 0 else 1

    except ImportError as e:
        print(f"❌ Web accuracy validation requires additional dependencies: {e}")
        print("   Install with: pip install beautifulsoup4")
        return 1
    except Exception as e:
        print(f"❌ Web accuracy validation failed: {e}")
        return 1


def cmd_validate(args) -> int:
    """Validate command - check receiver type configuration accuracy."""
    logger = setup_logging(args.loglevel)

    # Check if web accuracy validation was requested
    if args.web_accuracy:
        return cmd_validate_web_accuracy(args)

    try:
        # Initialize validator
        validator = ReceiverTypeValidator(logger)

        # Get receiver factory for available types
        factory = get_receiver_factory()
        available_types = list(factory.get_available_types().keys())
        logger.info(f"Available receiver types: {', '.join(available_types)}")

        # Get stations to validate
        if args.stations:
            # Validate specific stations
            station_ids = [s.upper() for s in args.stations]
            station_configs = {}
            for station_id in station_ids:
                config = get_station_config(station_id)
                if config:
                    station_configs[station_id] = config
                else:
                    logger.warning(f"Station {station_id} not found in configuration")
        else:
            # Validate all stations
            logger.info("Validating all stations in configuration...")
            station_configs = get_all_station_configs()

        if not station_configs:
            logger.error("No stations found to validate")
            return 1

        # Run validation
        logger.info(f"Validating receiver types for {len(station_configs)} stations...")
        results = validator.batch_validate_stations(station_configs)

        # Analyze results
        matches = sum(
            1 for r in results.values() if r.get("validation_status") == "match"
        )
        mismatches = sum(
            1 for r in results.values() if r.get("validation_status") == "mismatch"
        )
        unreachable = sum(
            1 for r in results.values() if r.get("validation_status") == "unreachable"
        )
        errors = sum(
            1 for r in results.values() if r.get("validation_status") == "error"
        )

        # Print summary
        print(f"\n=== RECEIVER TYPE VALIDATION RESULTS ===")
        print(f"Total stations validated: {len(results)}")
        print(f"✅ Correct receiver types: {matches}")
        print(f"❌ Mismatched receiver types: {mismatches}")
        print(f"🔌 Unreachable stations: {unreachable}")
        print(f"⚠️  Errors: {errors}")

        # Show mismatches in detail
        if mismatches > 0:
            print(f"\n=== RECEIVER TYPE MISMATCHES ===")
            for station_id, result in results.items():
                if result.get("validation_status") == "mismatch":
                    configured = result.get("configured_type", "Unknown")
                    detected = ", ".join(result.get("detected_types", []))
                    suggestion = result.get("suggestion", {})
                    recommended = suggestion.get("recommended_type", "Unknown")
                    confidence = suggestion.get("confidence", 0)

                    print(f"\n📡 {station_id} ({result.get('ip', 'Unknown IP')})")
                    print(f"   Configured: {configured}")
                    print(f"   Detected:   {detected}")
                    print(
                        f"   Recommended: {recommended} (confidence: {confidence:.1%})"
                    )

        # Generate correction report if requested
        if args.report:
            report = validator.generate_correction_report(results)
            print(f"\n{report}")

            # Save report to file
            report_file = f"receiver_validation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            with open(report_file, "w") as f:
                f.write(report)
            print(f"📄 Detailed report saved to: {report_file}")

        # Auto-fix if requested (EXPERIMENTAL)
        if args.fix and mismatches > 0:
            logger.warning(
                "⚠️  AUTO-FIX is EXPERIMENTAL - backup your stations.cfg first!"
            )
            response = input("Do you want to proceed with auto-corrections? [y/N]: ")

            if response.lower() in ["y", "yes"]:
                fixed_count = apply_receiver_type_corrections(results)
                print(f"🔧 Applied corrections to {fixed_count} stations")
                if fixed_count > 0:
                    print(
                        "⚠️  Please restart receivers service and verify functionality"
                    )
            else:
                print("Auto-fix cancelled")

        # Return appropriate exit code
        return 0 if (mismatches == 0 and errors == 0) else 1

    except Exception as e:
        logger.error(f"Validation failed: {e}")
        import traceback

        traceback.print_exc()
        return 1


def cmd_rec_config(args) -> int:
    """Receiver configuration command - extract or push config for Septentrio receivers."""
    logger = setup_logging(args.loglevel)

    from pathlib import Path
    from ..septentrio.tcp_client import (
        PolaRX5TCPClient,
        save_config_to_file,
        load_config_from_file,
        DEFAULT_CONTROL_PORT,
    )
    from ..config.receivers_config import get_receivers_config

    # Parse station list
    station_list = [s.strip().upper() for s in args.stations.split(',')]

    # Get receiver config for default port
    receivers_config = get_receivers_config()
    polarx5_config = receivers_config.get_receiver_config('polarx5')
    default_port = args.port or polarx5_config.get('control_port', DEFAULT_CONTROL_PORT)
    if isinstance(default_port, str):
        default_port = int(default_port)

    # Build targets list with IPs and ports
    targets = []
    for station_id in station_list:
        station_config = get_station_config(station_id)
        if station_config is None:
            logger.warning(f"Station {station_id} not found in configuration - SKIPPING")
            continue

        # Get IP - try multiple sources
        ip = (
            station_config.get('ip') or
            station_config.get('router', {}).get('ip') or
            station_config.get('host')
        )
        if not ip:
            logger.warning(f"Station {station_id} has no IP configured - SKIPPING")
            continue

        # Get port (station override > cli arg > config default)
        port = station_config.get('receiver', {}).get('controlport')
        if port:
            port = int(port)
        else:
            port = default_port

        targets.append((station_id, ip, port))

    if not targets:
        logger.error("No valid targets found")
        return 1

    logger.info(f"Targets: {', '.join(f'{s} ({ip}:{p})' for s, ip, p in targets)}")

    # Handle extract mode
    if args.extract:
        return _extract_configs(args, targets, logger)

    # Handle push mode
    if args.push:
        return _push_configs(args, targets, logger)

    return 0


def _extract_configs(args, targets, logger) -> int:
    """Extract configurations from receivers."""
    from pathlib import Path
    from ..septentrio.tcp_client import PolaRX5TCPClient, save_config_to_file
    from ..config.receivers_config import get_receivers_config
    import difflib
    import os

    config_type = args.config_type
    diff_file = Path(args.diff_with) if args.diff_with else None
    save_to_file = getattr(args, 'save', False)

    # Determine output directory if saving
    output_dir = None
    if save_to_file:
        if args.output_dir:
            output_dir = Path(args.output_dir).expanduser()
        else:
            # Get from config or use default
            try:
                receivers_config = get_receivers_config()
                polarx5_config = receivers_config.get_receiver_config('polarx5')
                config_dir = polarx5_config.get('rec_config_dir')
                if config_dir:
                    output_dir = Path(os.path.expanduser(config_dir))
                else:
                    output_dir = Path('/tmp/polarconfig')
            except Exception:
                output_dir = Path('/tmp/polarconfig')

        # Create directory if it doesn't exist
        output_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Output directory: {output_dir}")

    success_count = 0
    for station_id, ip, port in targets:
        if save_to_file:
            print(f"\n{'='*50}", file=sys.stderr)
            print(f"Extracting config from {station_id} ({ip}:{port})", file=sys.stderr)
            print(f"{'='*50}", file=sys.stderr)

        if args.dry_run:
            if save_to_file:
                print(f"  [DRY RUN] Would extract {config_type} config", file=sys.stderr)
                print(f"  [DRY RUN] Would save to: {output_dir}/PolaRx5_{station_id}_{config_type}_*.txt", file=sys.stderr)
            else:
                print(f"# [DRY RUN] Would extract {config_type} config from {station_id}")
            success_count += 1
            continue

        try:
            client = PolaRX5TCPClient(ip, station_id, port, timeout=args.timeout)
            config = client.extract_config(config_type)
            client.disconnect()

            if not config:
                logger.error(f"Failed to extract config from {station_id}")
                continue

            if save_to_file:
                # Save to file
                filepath = save_config_to_file(
                    config,
                    station_id,
                    config_type,
                    receiver_type="PolaRx5",
                    output_dir=output_dir
                )
                print(f"  ✓ Saved to: {filepath}", file=sys.stderr)

                # Show diff if requested
                if diff_file and diff_file.exists():
                    old_config = diff_file.read_text().strip().split('\n')
                    new_config = config.strip().split('\n')
                    diff = list(difflib.unified_diff(
                        old_config, new_config,
                        fromfile=str(diff_file),
                        tofile=str(filepath),
                        lineterm=''
                    ))
                    if diff:
                        print(f"\n  Differences from {diff_file.name}:", file=sys.stderr)
                        for line in diff[:50]:  # Limit output
                            print(f"    {line}", file=sys.stderr)
                        if len(diff) > 50:
                            print(f"    ... ({len(diff) - 50} more lines)", file=sys.stderr)
                    else:
                        print(f"  ✓ No differences from {diff_file.name}", file=sys.stderr)
            else:
                # Print to stdout (Unix convention)
                if len(targets) > 1:
                    # Add header for multiple stations
                    print(f"# Configuration for {station_id} ({config_type})")
                    print(f"# Extracted from {ip}:{port}")
                print(config)

            success_count += 1

        except Exception as e:
            logger.error(f"Error extracting from {station_id}: {e}")

    if save_to_file:
        print(f"\n{'='*50}", file=sys.stderr)
        print(f"Extracted {success_count}/{len(targets)} configurations", file=sys.stderr)
    return 0 if success_count == len(targets) else 1


def _push_configs(args, targets, logger) -> int:
    """Push configuration to receivers."""
    from pathlib import Path
    from ..septentrio.tcp_client import PolaRX5TCPClient, load_config_from_file

    config_path = Path(args.push)
    try:
        commands = load_config_from_file(config_path)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 1

    logger.info(f"Loaded {len(commands)} commands from {config_path.name}")
    if not args.no_save:
        logger.info("Will save to Boot config after applying")

    # Dry run - show commands and exit
    if args.dry_run:
        print("\n--- DRY RUN - Commands to send ---")
        for cmd in commands:
            if cmd.strip() and not cmd.strip().startswith('#'):
                print(f"  {cmd}")
        print("--- End of commands ---\n")
        print("Dry run complete. Use without --dry-run to execute.")
        return 0

    success_count = 0
    for station_id, ip, port in targets:
        print(f"\n{'='*50}")
        print(f"Pushing config to {station_id} ({ip}:{port})")
        print(f"{'='*50}")

        try:
            client = PolaRX5TCPClient(ip, station_id, port, timeout=args.timeout)
            success, errors = client.push_config(commands, save_to_boot=not args.no_save)
            client.disconnect()

            if success:
                print(f"  ✓ Configuration applied successfully")
                if not args.no_save:
                    print(f"  ✓ Saved to Boot config")
                success_count += 1
            else:
                print(f"  ✗ Errors occurred:")
                for err in errors:
                    print(f"    - {err}")

        except Exception as e:
            logger.error(f"  Error pushing to {station_id}: {e}")

    print(f"\n{'='*50}")
    print(f"Successfully configured {success_count}/{len(targets)} receivers")
    return 0 if success_count == len(targets) else 1


def get_all_station_configs() -> Dict[str, Dict[str, Any]]:
    """Get configurations for all stations.

    Returns:
        Dictionary mapping station_id to configuration
    """
    if not HAS_GPS_PARSER:
        logging.error("gps_parser not available - cannot load all stations")
        return {}

    try:
        import configparser

        parser = gps_parser.ConfigParser()
        config = configparser.ConfigParser()
        config.read(parser.get_stations_config_path())

        stations = {}
        for section in config.sections():
            try:
                station_config = get_station_config(section)
                if station_config:
                    stations[section] = station_config
            except Exception as e:
                logging.debug(f"Could not load config for {section}: {e}")

        return stations
    except Exception as e:
        logging.error(f"Could not load all station configurations: {e}")
        return {}


def apply_receiver_type_corrections(
    validation_results: Dict[str, Dict[str, Any]],
) -> int:
    """Apply receiver type corrections to stations.cfg (EXPERIMENTAL).

    Args:
        validation_results: Results from validation

    Returns:
        Number of corrections applied
    """
    # TODO: Implement auto-correction to stations.cfg
    # This would require:
    # 1. Reading stations.cfg
    # 2. Updating receiver_type fields for mismatched stations
    # 3. Writing back to stations.cfg
    # 4. Validating the changes

    logging.warning(
        "Auto-correction not yet implemented - use --report to get manual corrections"
    )
    return 0


def _create_rinex_converter(
    station_id: str, args, rinex_version, output_format, naming_convention,
    observation_types, logger: logging.Logger,
):
    """Create appropriate RINEX converter for a station.

    Returns:
        Tuple of (converter, raw_extension) or (None, None) on failure.
    """
    from ..rinex import SBFConverter, TrimbleConverter, LeicaConverter, TrimbleNativeConverter

    station_config = get_station_config(station_id)
    if station_config is None:
        logger.warning(f"Station {station_id} not found in configuration - SKIPPING")
        return None, None, None

    receiver_type = station_config.get("receiver", {}).get("type", "").lower()
    use_native_trimble = getattr(args, "native_trimble", False)

    if "polarx" in receiver_type or "septentrio" in receiver_type:
        converter = SBFConverter(
            station_id=station_id,
            rinex_version=rinex_version,
            output_format=output_format,
            naming_convention=naming_convention,
            apply_header_corrections=not getattr(args, "no_header_correction", False),
            observation_types=observation_types,
            loglevel=args.loglevel,
        )
        raw_extension = ".sbf.gz"
    elif "netr9" in receiver_type:
        if use_native_trimble:
            if not TrimbleNativeConverter.is_available():
                logger.error("Native Trimble converter not available")
                print("\n" + "=" * 60)
                print("NATIVE TRIMBLE CONVERTER - Setup Required")
                print("=" * 60)
                print("\nThe --native-trimble option requires Docker with the")
                print("trm2rinex image installed.")
                print("\nQuick setup:")
                print("  cd tools/trimble-native && ./setup.sh")
                print("\nManual setup:")
                print("  docker pull geodesyewsp/trm2rinex:cli-light")
                print("  docker tag geodesyewsp/trm2rinex:cli-light trm2rinex:cli-light")
                print("\nAlternative: Use standard conversion (without --native-trimble)")
                print("  receivers rinex STATION -d 1")
                print("=" * 60 + "\n")
                return None, None, None
            converter = TrimbleNativeConverter(
                station_id=station_id,
                rinex_version=rinex_version,
                output_format=output_format,
                naming_convention=naming_convention,
                apply_header_corrections=not getattr(args, "no_header_correction", False),
                loglevel=args.loglevel,
            )
        else:
            converter = TrimbleConverter(
                station_id=station_id,
                rinex_version=rinex_version,
                output_format=output_format,
                naming_convention=naming_convention,
                apply_header_corrections=not getattr(args, "no_header_correction", False),
                keep_intermediate=getattr(args, "keep_intermediate", False),
                loglevel=args.loglevel,
            )
        raw_extension = ".T02*"  # Match .T02 and .T02.gz
    elif "netrs" in receiver_type:
        if use_native_trimble:
            if not TrimbleNativeConverter.is_available():
                logger.error("Native Trimble converter not available")
                print("\n" + "=" * 60)
                print("NATIVE TRIMBLE CONVERTER - Setup Required")
                print("=" * 60)
                print("\nThe --native-trimble option requires Docker with the")
                print("trm2rinex image installed.")
                print("\nQuick setup:")
                print("  cd tools/trimble-native && ./setup.sh")
                print("\nManual setup:")
                print("  docker pull geodesyewsp/trm2rinex:cli-light")
                print("  docker tag geodesyewsp/trm2rinex:cli-light trm2rinex:cli-light")
                print("\nAlternative: Use standard conversion (without --native-trimble)")
                print("  receivers rinex STATION -d 1")
                print("=" * 60 + "\n")
                return None, None, None
            converter = TrimbleNativeConverter(
                station_id=station_id,
                rinex_version=rinex_version,
                output_format=output_format,
                naming_convention=naming_convention,
                apply_header_corrections=not getattr(args, "no_header_correction", False),
                loglevel=args.loglevel,
            )
        else:
            converter = TrimbleConverter(
                station_id=station_id,
                rinex_version=rinex_version,
                output_format=output_format,
                naming_convention=naming_convention,
                apply_header_corrections=not getattr(args, "no_header_correction", False),
                keep_intermediate=getattr(args, "keep_intermediate", False),
                loglevel=args.loglevel,
            )
        raw_extension = ".T00*"  # Match .T00 and .T00.gz
    elif "g10" in receiver_type or "leica" in receiver_type:
        converter = LeicaConverter(
            station_id=station_id,
            rinex_version=rinex_version,
            output_format=output_format,
            naming_convention=naming_convention,
            apply_header_corrections=not getattr(args, "no_header_correction", False),
            keep_intermediate=getattr(args, "keep_intermediate", False),
            loglevel=args.loglevel,
        )
        raw_extension = ".m00.gz"
    else:
        logger.warning(
            f"Unsupported receiver type '{receiver_type}' for {station_id} - SKIPPING"
        )
        return None, None, None

    # Validate tools
    if not getattr(args, "dry_run", False):
        tools = converter.validate_tools()
        missing = [t for t, avail in tools.items() if not avail]
        if missing:
            from ..tools import ToolManager
            manager = ToolManager()
            logger.error(f"Missing required tools for {station_id}: {', '.join(missing)}")
            # Print detailed installation guide
            print(manager.get_installation_guide(missing))
            return None, None, None

    return converter, raw_extension, station_config


def _rinex_convert_station_period(
    station_id: str, converter, raw_extension: str,
    start_time: datetime, end_time: datetime,
    args, logger: logging.Logger,
) -> tuple:
    """Convert raw files to RINEX for a single station and time period.

    Returns:
        Tuple of (converted, failed, skipped).
    """
    from ..config.receivers_config import get_receivers_config

    converted = 0
    failed = 0
    skipped = 0

    try:
        config = get_receivers_config()
        data_prepath = config.get_data_prepath()

        current_date = start_time
        raw_files = []

        while current_date < end_time:
            year = current_date.strftime("%Y")
            month = current_date.strftime("%b").lower()

            if "1hr" in args.session.lower():
                filename = f"{station_id}{current_date.strftime('%Y%m%d%H%M')}*.{raw_extension.lstrip('.')}"
                raw_dir = Path(data_prepath) / year / month / station_id / args.session / "raw"
                current_date += timedelta(hours=1)
            else:
                filename = f"{station_id}{current_date.strftime('%Y%m%d')}*{raw_extension}"
                raw_dir = Path(data_prepath) / year / month / station_id / args.session / "raw"
                current_date += timedelta(days=1)

            if raw_dir.exists():
                matches = list(raw_dir.glob(filename))
                raw_files.extend(matches)

        if not raw_files:
            print(f"  No raw files found for {station_id}")
            logger.warning(f"No raw files found for {station_id} in date range")
            return 0, 0, 1

        print(f"  Found {len(raw_files)} raw file(s) to convert")
        logger.info(f"Found {len(raw_files)} raw files to convert")

        if getattr(args, "dry_run", False):
            for raw_file in raw_files:
                print(f"  [DRY RUN] Would convert: {raw_file.name}")
            return 0, 0, 0

        for raw_file in raw_files:
            if getattr(args, "output_dir", None):
                output_dir = Path(args.output_dir)
            else:
                raw_parent = raw_file.parent
                if raw_parent.name == "raw":
                    output_dir = raw_parent.parent / "rinex"
                else:
                    output_dir = raw_parent / "rinex"

            output_dir.mkdir(parents=True, exist_ok=True)

            result = converter.convert_file(
                raw_file,
                output_dir=output_dir,
                force=getattr(args, "force", False),
            )

            if result.success:
                print(f"  ✅ {raw_file.name} -> {result.rinex_file.name}")
                logger.info(f"✅ {raw_file.name} -> {result.rinex_file.name}")
                if result.header_corrections_applied > 0:
                    logger.debug(
                        f"   Applied {result.header_corrections_applied} header corrections"
                    )
                converted += 1
            else:
                print(f"  ❌ {raw_file.name}: {result.message}")
                logger.error(f"❌ {raw_file.name}: {result.message}")
                failed += 1

    except Exception as e:
        logger.error(f"Error processing {station_id}: {e}")
        if args.loglevel == logging.DEBUG:
            import traceback
            traceback.print_exc()
        failed += 1

    return converted, failed, skipped


def cmd_rinex(args) -> int:
    """RINEX conversion command - convert raw GPS data to RINEX format."""
    logger = setup_logging(args.loglevel)
    stations = [s.upper() for s in args.stations]

    # Import rinex module
    try:
        from ..rinex import (
            SBFConverter,
            TrimbleConverter,
            RinexVersion,
            OutputFormat,
            NamingConvention,
        )
    except ImportError as e:
        logger.error(f"RINEX module not available: {e}")
        return 1

    # Load RINEX defaults from config
    from ..config.receivers_config import get_receivers_config
    try:
        rinex_config = get_receivers_config().get_rinex_config()
    except FileNotFoundError:
        rinex_config = {
            "default_version": 3,
            "default_naming": "short",
            "default_hatanaka": True,
            "default_compression": "gz",
            "apply_header_corrections": True,
        }

    # Parse RINEX version (CLI overrides config)
    version_map = {
        2: RinexVersion.RINEX_2,
        3: RinexVersion.RINEX_3,
        4: RinexVersion.RINEX_4,
    }
    # Use CLI arg if explicitly set, otherwise use config default
    cli_version = getattr(args, 'rinex_version', None)
    if cli_version is not None:
        rinex_version = version_map.get(cli_version, RinexVersion.RINEX_3)
    else:
        config_version = rinex_config.get("default_version", 3)
        rinex_version = version_map.get(config_version, RinexVersion.RINEX_3)

    # Parse output format (None means use config defaults)
    if args.output_format == "legacy":
        output_format = OutputFormat.LEGACY
    elif args.output_format == "modern":
        output_format = OutputFormat.MODERN
    else:
        output_format = None  # Let converter use config defaults

    # Parse naming convention (CLI overrides config)
    if args.naming is not None:
        naming_str = args.naming
    else:
        naming_str = rinex_config.get("default_naming", "short")
    naming_convention = (
        NamingConvention.SHORT
        if naming_str == "short"
        else NamingConvention.LONG
    )

    # Parse observation types
    observation_types = None
    if getattr(args, "observation_types", None):
        observation_types = [t.strip() for t in args.observation_types.split(",")]

    # Parse date range
    from datetime import timedelta
    from ..utils.time_utils import calculate_download_time_range

    start_time = None
    end_time = None
    reverse_chronological = False

    if getattr(args, "start", None):
        start_time = parse_datetime(args.start)

    if getattr(args, "end", None):
        end_time = parse_datetime(args.end)

    if not start_time and getattr(args, "days", None):
        reverse_chronological = True
        start_time, end_time = calculate_download_time_range(
            session_type=args.session, lookback_periods=args.days
        )

    if start_time and not end_time:
        if "1hr" in args.session.lower():
            end_time = start_time + timedelta(hours=1)
        else:
            end_time = start_time + timedelta(days=1)

    # Handle same start/end date (inclusive range)
    if start_time and end_time and start_time == end_time:
        if "1hr" in args.session.lower():
            end_time = start_time + timedelta(hours=1)
        else:
            end_time = start_time + timedelta(days=1)

    if not start_time:
        logger.error("No date range specified. Use -s/--start, -e/--end, or -d/--days")
        return 1

    # Print progress info (always visible, not dependent on log level)
    print(f"RINEX conversion for {len(stations)} station(s)")
    print(f"Date range: {start_time.strftime('%Y-%m-%d')} to {end_time.strftime('%Y-%m-%d')}")
    print(f"RINEX version: {rinex_version.value}, Naming: {naming_str}")
    logger.info(f"RINEX conversion for {len(stations)} stations")
    logger.info(f"Date range: {start_time} to {end_time}")
    logger.info(f"RINEX version: {rinex_version.value}, Naming: {naming_str}")

    # Track results
    total_converted = 0
    total_failed = 0
    total_skipped = 0

    # Network-first: -d with multiple stations → period→station ordering
    network_first = reverse_chronological and len(stations) > 1

    if network_first:
        # Pre-create converters for all stations
        converters: Dict[str, tuple] = {}
        for station_id in stations:
            conv, ext, _ = _create_rinex_converter(
                station_id, args, rinex_version, output_format,
                naming_convention, observation_types, logger,
            )
            if conv is not None:
                converters[station_id] = (conv, ext)
            else:
                total_skipped += 1

        if not converters:
            logger.error("No valid stations to convert")
            return 1

        # Outer: periods (newest first), Inner: stations
        periods = generate_period_ranges(
            start_time, end_time, args.session, reverse=True
        )
        for period_start, period_end in periods:
            if args.session == '15s_24hr':
                logger.info(f"\n--- {period_start.strftime('%Y-%m-%d')} ---")
            else:
                logger.info(f"\n--- {period_start.strftime('%Y-%m-%d %H:%M')} ---")
            for station_id, (conv, ext) in converters.items():
                logger.info(f"Processing station: {station_id}")
                c, f, s = _rinex_convert_station_period(
                    station_id, conv, ext, period_start, period_end,
                    args, logger,
                )
                total_converted += c
                total_failed += f
                total_skipped += s
    else:
        # Station-first: current behavior
        for station_id in stations:
            print(f"\nProcessing: {station_id}")
            logger.info(f"\n{'='*60}")
            logger.info(f"Processing station: {station_id}")
            logger.info(f"{'='*60}")

            conv, ext, _ = _create_rinex_converter(
                station_id, args, rinex_version, output_format,
                naming_convention, observation_types, logger,
            )
            if conv is None:
                total_skipped += 1
                continue

            c, f, s = _rinex_convert_station_period(
                station_id, conv, ext, start_time, end_time,
                args, logger,
            )
            total_converted += c
            total_failed += f
            total_skipped += s

    # Summary
    print(f"\nSummary: ✅ {total_converted} converted, ❌ {total_failed} failed, ⏭️  {total_skipped} skipped")
    logger.info(f"\n{'='*60}")
    logger.info(f"RINEX Conversion Summary:")
    logger.info(f"  ✅ Converted: {total_converted}")
    logger.info(f"  ❌ Failed: {total_failed}")
    logger.info(f"  ⏭️  Skipped: {total_skipped}")
    logger.info(f"{'='*60}")

    return 0 if total_failed == 0 else 1


def cmd_tools(args) -> int:
    """Tools management command - install and configure RINEX conversion tools."""
    from ..tools import ToolManager

    manager = ToolManager()

    if not hasattr(args, 'tools_command') or args.tools_command is None:
        # No subcommand - show help
        print("Usage: receivers tools <command>")
        print("\nCommands:")
        print("  list         List all tools and their installation status")
        print("  install      Install a specific tool")
        print("  install-all  Install all auto-installable tools")
        print("  check        Check tool availability")
        print("  configure    Update receivers.cfg with tool paths")
        print("\nRun 'receivers tools <command> --help' for more info")
        return 0

    if args.tools_command == "list":
        tools = manager.list_tools()
        print("\nRINEX Conversion Tools")
        print("=" * 70)

        for name, info in tools.items():
            status_icon = "✅" if info["status"] == "installed" else "❌"
            if info["status"] == "manual_required":
                status_icon = "📋"

            auto = " (auto)" if info["auto_install"] else " (manual)"

            print(f"\n{status_icon} {name}{auto}")
            print(f"   {info['description']}")
            if info["installed_path"]:
                print(f"   Path: {info['installed_path']}")
            if info["version"]:
                print(f"   Version: {info['version']}")
            print(f"   Required for: {', '.join(info['required_for'])}")

        print("\n" + "=" * 70)
        print("Run 'receivers tools install <name>' to install a tool")
        print("Run 'receivers tools install-all' to install all auto-installable tools")
        return 0

    elif args.tools_command == "install":
        tool_name = args.tool_name
        force = getattr(args, 'force', False)

        print(f"\nInstalling {tool_name}...")
        result = manager.install(tool_name, force=force)

        if result.success:
            print(f"\n✅ {result.message}")
            if result.path:
                print(f"   Path: {result.path}")
            return 0
        else:
            print(f"\n❌ {result.message}")
            return 1

    elif args.tools_command == "install-all":
        force = getattr(args, 'force', False)

        print("\nInstalling all auto-installable tools...")
        results = manager.install_all(force=force)

        success_count = sum(1 for r in results if r.success)
        fail_count = len(results) - success_count

        print(f"\nSummary: ✅ {success_count} installed, ❌ {fail_count} failed")

        for r in results:
            if not r.success and "Manual installation required" not in r.message:
                print(f"  ❌ {r.tool_name}: {r.message}")

        return 0 if fail_count == 0 else 1

    elif args.tools_command == "check":
        receiver_type = getattr(args, 'receiver_type', None)

        tools = manager.check_tools(receiver_type=receiver_type)

        if receiver_type:
            print(f"\nTools for {receiver_type}:")
        else:
            print("\nTool availability:")

        for name, available in tools.items():
            icon = "✅" if available else "❌"
            print(f"  {icon} {name}")

        all_available = all(tools.values())
        if all_available:
            print("\n✅ All required tools are available")
            return 0
        else:
            print("\n⚠️  Some tools are missing. Run 'receivers tools install-all'")
            return 1

    elif args.tools_command == "configure":
        from pathlib import Path

        config_path = None
        if hasattr(args, 'config') and args.config:
            config_path = Path(args.config)

        updated = manager.configure_receivers_cfg(config_path)

        if updated:
            print("✅ Updated receivers.cfg with tool paths")
            return 0
        else:
            print("ℹ️  No changes needed (tools already configured)")
            return 0

    return 0


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser using standardized arguments module."""
    from .arguments import create_argument_parser

    parser = create_argument_parser()

    # Get subparsers and set command functions
    # We need to access the subparsers to set the func defaults
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparsers_map = action.choices
            if "download" in subparsers_map:
                subparsers_map["download"].set_defaults(func=cmd_download)
            if "status" in subparsers_map:
                subparsers_map["status"].set_defaults(func=cmd_status)
            if "health" in subparsers_map:
                subparsers_map["health"].set_defaults(func=cmd_health)
            if "validate" in subparsers_map:
                subparsers_map["validate"].set_defaults(func=cmd_validate)
            if "rec-config" in subparsers_map:
                subparsers_map["rec-config"].set_defaults(func=cmd_rec_config)
            if "rinex" in subparsers_map:
                subparsers_map["rinex"].set_defaults(func=cmd_rinex)
            if "tools" in subparsers_map:
                subparsers_map["tools"].set_defaults(func=cmd_tools)
            break

    return parser


def main() -> int:
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Handle scheduler subcommands
    if args.command == "scheduler":
        try:
            from .scheduler import handle_scheduler_command

            return handle_scheduler_command(args)
        except ImportError:
            print(
                "❌ Scheduler requires APScheduler. Install with: pip install apscheduler"
            )
            return 1

    # Handle TOS subcommands
    if args.command == "tos":
        try:
            from .tos import handle_tos_command

            return handle_tos_command(args)
        except ImportError as e:
            print(f"❌ TOS command requires tostools. Install with: pip install tostools")
            print(f"   Error: {e}")
            return 1

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        return 130
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

