"""Push configuration commands to Septentrio PolaRx5 receivers via TCP.

This module provides functionality to send command files to receivers
via their TCP command interface (default port 28784).
"""

import logging
import socket
import time
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Default command port for Septentrio receivers
DEFAULT_CONTROL_PORT = 28784
DEFAULT_TIMEOUT = 10


def send_commands_to_receiver(
    ip: str,
    port: int,
    commands: List[str],
    timeout: float = DEFAULT_TIMEOUT,
    verbose: bool = False,
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
        initial = sock.recv(1024).decode("utf-8", errors="ignore")
        if verbose:
            logger.debug(f"Connected: {initial.strip()}")

        # Send each command
        for cmd in commands:
            cmd = cmd.strip()
            if not cmd or cmd.startswith("#"):
                continue  # Skip empty lines and comments

            if verbose:
                logger.debug(f"Sending: {cmd}")

            # Send command with newline
            sock.send((cmd + "\n").encode("utf-8"))

            # Wait a bit for response
            time.sleep(0.1)

            # Read response
            try:
                response = sock.recv(4096).decode("utf-8", errors="ignore")
                responses.append(response)

                if verbose:
                    # Print response (clean up prompts)
                    clean_response = response.replace("IP10>", "").strip()
                    if clean_response:
                        logger.debug(f"Response: {clean_response[:200]}")

                # Check for errors
                if "$E:" in response:
                    logger.warning(f"Error in response: {response}")

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


def push_config_to_station(
    station_id: str,
    ip: str,
    port: int,
    commands: List[str],
    save_to_boot: bool = True,
    verbose: bool = False,
) -> Tuple[bool, str]:
    """Push configuration to a single station.

    Args:
        station_id: Station identifier
        ip: Receiver IP address
        port: TCP command port
        commands: List of commands to send
        save_to_boot: Whether to save config to boot (eccf command)
        verbose: Enable verbose output

    Returns:
        Tuple of (success, message)
    """
    # Add save-to-boot command if requested
    if save_to_boot:
        commands = commands + ["eccf, Current, Boot"]

    logger.info(f"Pushing config to {station_id} ({ip}:{port})")

    success, responses = send_commands_to_receiver(ip, port, commands, verbose=verbose)

    if success:
        return True, "Configuration sent successfully"
    else:
        error_msg = responses[0] if responses else "Unknown error"
        return False, f"Failed - {error_msg}"


def load_config_file(config_path: Path) -> List[str]:
    """Load commands from a configuration file.

    Args:
        config_path: Path to configuration file

    Returns:
        List of command strings

    Raises:
        FileNotFoundError: If config file doesn't exist
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    return config_path.read_text().strip().split("\n")
