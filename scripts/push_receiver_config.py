#!/usr/bin/env python3
"""Push configuration commands to Septentrio PolaRx5 receivers via TCP.

This script sends command files to multiple receivers simultaneously,
avoiding the need to manually upload through the web interface.

Usage:
    # Single station
    python push_receiver_config.py ISFS config_file.txt

    # Multiple stations
    python push_receiver_config.py ISFS,THOB,OLKE config_file.txt

    # All stations from config
    python push_receiver_config.py --all config_file.txt

    # Dry run (show what would be sent)
    python push_receiver_config.py ISFS config_file.txt --dry-run
"""

import argparse
import socket
import sys
import time
from pathlib import Path
from typing import List, Tuple, Optional

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from gps_parser import ConfigParser
    HAS_GPS_PARSER = True
except ImportError:
    HAS_GPS_PARSER = False
    print("Warning: gps_parser not available, --all option won't work")


# Default command port for Septentrio receivers
DEFAULT_CMD_PORT = 28784
DEFAULT_TIMEOUT = 10


def send_commands_to_receiver(
    ip: str,
    port: int,
    commands: List[str],
    timeout: float = DEFAULT_TIMEOUT,
    verbose: bool = False
) -> Tuple[bool, List[str]]:
    """Send commands to a receiver via TCP.

    Args:
        ip: Receiver IP address
        port: Command port (usually 28784)
        commands: List of command strings to send
        timeout: Socket timeout in seconds
        verbose: Print detailed output

    Returns:
        Tuple of (success, responses)
    """
    responses = []

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, port))

        # Read initial prompt
        initial = sock.recv(1024).decode('utf-8', errors='ignore')
        if verbose:
            print(f"  Connected: {initial.strip()}")

        # Send each command
        for cmd in commands:
            cmd = cmd.strip()
            if not cmd or cmd.startswith('#'):
                continue  # Skip empty lines and comments

            if verbose:
                print(f"  Sending: {cmd}")

            # Send command with newline
            sock.send((cmd + '\n').encode('utf-8'))

            # Wait a bit for response
            time.sleep(0.1)

            # Read response
            try:
                response = sock.recv(4096).decode('utf-8', errors='ignore')
                responses.append(response)

                if verbose:
                    # Print response (clean up prompts)
                    clean_response = response.replace('IP10>', '').strip()
                    if clean_response:
                        print(f"  Response: {clean_response[:200]}")

                # Check for errors
                if '$E:' in response:
                    print(f"  ⚠️  Error in response: {response}")

            except socket.timeout:
                responses.append("(no response - timeout)")

        sock.close()
        return True, responses

    except socket.timeout:
        return False, [f"Connection timeout to {ip}:{port}"]
    except ConnectionRefusedError:
        return False, [f"Connection refused to {ip}:{port}"]
    except Exception as e:
        return False, [f"Error: {e}"]


def get_station_ip(station_id: str) -> Optional[str]:
    """Get station IP from gps_parser config."""
    if not HAS_GPS_PARSER:
        return None

    try:
        config = ConfigParser()
        station_info = config.get_station_info(station_id)
        return station_info.get('router_ip')
    except Exception as e:
        print(f"Warning: Could not get IP for {station_id}: {e}")
        return None


def get_all_polarx5_stations() -> List[str]:
    """Get list of all PolaRx5 stations from config."""
    if not HAS_GPS_PARSER:
        return []

    try:
        config = ConfigParser()
        stations = []
        for station_id in config.get_all_stations():
            info = config.get_station_info(station_id)
            if info.get('receiver_type', '').lower() == 'polarx5':
                stations.append(station_id)
        return stations
    except Exception as e:
        print(f"Warning: Could not get station list: {e}")
        return []


def main():
    parser = argparse.ArgumentParser(
        description='Push configuration to Septentrio PolaRx5 receivers',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        'stations',
        nargs='?',
        help='Station ID(s), comma-separated (e.g., ISFS,THOB) or --all'
    )
    parser.add_argument(
        'config_file',
        help='Configuration file with receiver commands'
    )
    parser.add_argument(
        '--all',
        action='store_true',
        help='Apply to all PolaRx5 stations'
    )
    parser.add_argument(
        '--ip',
        help='Direct IP address (overrides station lookup)'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=DEFAULT_CMD_PORT,
        help=f'Command port (default: {DEFAULT_CMD_PORT})'
    )
    parser.add_argument(
        '--timeout',
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f'Connection timeout in seconds (default: {DEFAULT_TIMEOUT})'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be sent without actually sending'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Verbose output'
    )
    parser.add_argument(
        '--no-save',
        action='store_true',
        help='Do not save config to boot (default: saves to both Current and Boot)'
    )

    args = parser.parse_args()

    # Read config file
    config_path = Path(args.config_file)
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)

    commands = config_path.read_text().strip().split('\n')

    # Add save-to-boot command unless --no-save is specified
    if not args.no_save:
        commands.append('eccf, Current, Boot')

    print(f"Loaded {len(commands)} commands from {config_path.name}")
    if not args.no_save:
        print("Will save to Boot config after applying")

    if args.dry_run:
        print("\n--- DRY RUN - Commands to send ---")
        for cmd in commands:
            if cmd.strip() and not cmd.strip().startswith('#'):
                print(f"  {cmd}")
        print("--- End of commands ---\n")

    # Determine target stations
    if args.ip:
        # Direct IP mode
        targets = [('DIRECT', args.ip)]
    elif args.all:
        stations = get_all_polarx5_stations()
        if not stations:
            print("Error: No PolaRx5 stations found or gps_parser not available")
            sys.exit(1)
        targets = [(s, get_station_ip(s)) for s in stations]
    elif args.stations:
        station_list = [s.strip() for s in args.stations.split(',')]
        targets = [(s, get_station_ip(s)) for s in station_list]
    else:
        parser.print_help()
        sys.exit(1)

    # Filter out stations without IPs
    targets = [(s, ip) for s, ip in targets if ip]

    if not targets:
        print("Error: No valid targets found")
        sys.exit(1)

    print(f"\nTargets: {', '.join(f'{s} ({ip})' for s, ip in targets)}")

    if args.dry_run:
        print("\nDry run complete. Use without --dry-run to execute.")
        sys.exit(0)

    # Send to each target
    results = []
    for station_id, ip in targets:
        print(f"\n{'='*50}")
        print(f"📡 {station_id} ({ip}:{args.port})")
        print('='*50)

        success, responses = send_commands_to_receiver(
            ip, args.port, commands,
            timeout=args.timeout,
            verbose=args.verbose
        )

        if success:
            print(f"✅ {station_id}: Configuration sent successfully")
        else:
            print(f"❌ {station_id}: Failed - {responses[0] if responses else 'Unknown error'}")

        results.append((station_id, success))

    # Summary
    print(f"\n{'='*50}")
    print("SUMMARY")
    print('='*50)
    succeeded = sum(1 for _, s in results if s)
    failed = len(results) - succeeded
    print(f"✅ Succeeded: {succeeded}")
    print(f"❌ Failed: {failed}")

    if failed > 0:
        print("\nFailed stations:")
        for station_id, success in results:
            if not success:
                print(f"  - {station_id}")
        sys.exit(1)


if __name__ == '__main__':
    main()
