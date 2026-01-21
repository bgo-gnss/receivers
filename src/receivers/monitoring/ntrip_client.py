"""NTRIP client for checking RTK stream status.

Connects to NTRIP casters to verify RTK correction streams are active
and measure data latency.
"""

import base64
import logging
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class NTRIPConfig:
    """NTRIP connection configuration."""

    host: str
    port: int
    username: str
    password: str
    mountpoints: List[str]  # List of mountpoint names to check
    connect_timeout: float = 10.0
    data_timeout: float = 5.0
    latency_warning: float = 10.0
    latency_critical: float = 30.0

    @classmethod
    def from_config(
        cls,
        station_id: str,
        receivers_config: Any,
        station_config: Optional[Dict[str, Any]] = None,
    ) -> Optional["NTRIPConfig"]:
        """Create NTRIPConfig from receivers config and station config.

        Args:
            station_id: Station identifier (e.g., 'THOB')
            receivers_config: ReceiversConfig instance with ntrip_defaults
            station_config: Optional station-specific config dict

        Returns:
            NTRIPConfig if configuration is available, None otherwise
        """
        station_config = station_config or {}

        # Get defaults from receivers.cfg [ntrip_defaults] section
        try:
            defaults = {
                "host": receivers_config.config.get(
                    "ntrip_defaults", "host", fallback="ntrcaster.vedur.is"
                ),
                "port": receivers_config.config.getint(
                    "ntrip_defaults", "port", fallback=2101
                ),
                "username": receivers_config.config.get(
                    "ntrip_defaults", "username", fallback=""
                ),
                "password": receivers_config.config.get(
                    "ntrip_defaults", "password", fallback=""
                ),
                "mountpoint_suffix": receivers_config.config.get(
                    "ntrip_defaults", "mountpoint_suffix", fallback="0"
                ),
                "connect_timeout": receivers_config.config.getfloat(
                    "ntrip_defaults", "connect_timeout", fallback=10.0
                ),
                "data_timeout": receivers_config.config.getfloat(
                    "ntrip_defaults", "data_timeout", fallback=5.0
                ),
                "latency_warning": receivers_config.config.getfloat(
                    "ntrip_defaults", "latency_warning", fallback=10.0
                ),
                "latency_critical": receivers_config.config.getfloat(
                    "ntrip_defaults", "latency_critical", fallback=30.0
                ),
            }
        except Exception as e:
            logger.debug(f"Could not load ntrip_defaults: {e}")
            defaults = {
                "host": "ntrcaster.vedur.is",
                "port": 2101,
                "username": "gpsops",
                "password": "<your_password>",
                "mountpoint_suffix": "0",
                "connect_timeout": 10.0,
                "data_timeout": 5.0,
                "latency_warning": 10.0,
                "latency_critical": 30.0,
            }

        # Apply per-station overrides (ntrip_ prefix)
        host = station_config.get("ntrip_host", defaults["host"])
        port = int(station_config.get("ntrip_port", defaults["port"]))
        username = station_config.get("ntrip_username", defaults["username"])
        password = station_config.get("ntrip_password", defaults["password"])
        connect_timeout = float(
            station_config.get("ntrip_connect_timeout", defaults["connect_timeout"])
        )
        data_timeout = float(
            station_config.get("ntrip_data_timeout", defaults["data_timeout"])
        )
        latency_warning = float(
            station_config.get("ntrip_latency_warning", defaults["latency_warning"])
        )
        latency_critical = float(
            station_config.get("ntrip_latency_critical", defaults["latency_critical"])
        )

        # Determine mountpoints
        mountpoints = []

        # Check for explicit mountpoint name(s)
        explicit_mountpoint = station_config.get("ntrip_mountpoint")
        if explicit_mountpoint:
            # Can be comma-separated
            mountpoints = [mp.strip() for mp in explicit_mountpoint.split(",")]
        else:
            # Build from station ID + suffix
            suffix_str = station_config.get(
                "ntrip_mountpoint_suffix", defaults["mountpoint_suffix"]
            )
            suffixes = [s.strip() for s in suffix_str.split(",")]
            mountpoints = [f"{station_id}{suffix}" for suffix in suffixes]

        if not mountpoints:
            logger.debug(f"No NTRIP mountpoints configured for {station_id}")
            return None

        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            mountpoints=mountpoints,
            connect_timeout=connect_timeout,
            data_timeout=data_timeout,
            latency_warning=latency_warning,
            latency_critical=latency_critical,
        )


@dataclass
class MountpointStatus:
    """Status of a single NTRIP mountpoint."""

    mountpoint: str
    is_active: bool
    bytes_received: int = 0
    latency_seconds: Optional[float] = None
    error_message: Optional[str] = None
    data_rate_bps: Optional[float] = None  # bytes per second


@dataclass
class NTRIPStatus:
    """Overall NTRIP status for a station."""

    station_id: str
    host: str
    port: int
    mountpoints: List[MountpointStatus]
    overall_status: str  # 'ok', 'warning', 'critical', 'unknown'
    message: str
    check_time: datetime


class NTRIPClient:
    """Client for checking NTRIP stream status.

    Connects to NTRIP casters and verifies that RTK correction
    streams are active and have acceptable latency.
    """

    USER_AGENT = "receivers/1.0"

    def __init__(self, config: NTRIPConfig):
        """Initialize NTRIP client.

        Args:
            config: NTRIP connection configuration
        """
        self.config = config
        self.logger = logging.getLogger(__name__)

    def _build_auth_header(self) -> str:
        """Build HTTP Basic Auth header."""
        credentials = f"{self.config.username}:{self.config.password}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    def _connect_and_receive(
        self, mountpoint: str, duration: float = 3.0
    ) -> Tuple[bool, int, Optional[str]]:
        """Connect to mountpoint and receive data for duration.

        Args:
            mountpoint: Mountpoint name (e.g., 'THOB0')
            duration: How long to receive data (seconds)

        Returns:
            Tuple of (success, bytes_received, error_message)
        """
        sock = None
        try:
            # Create socket and connect
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.config.connect_timeout)
            sock.connect((self.config.host, self.config.port))

            # Build HTTP request (NTRIP v2.0)
            request = (
                f"GET /{mountpoint} HTTP/1.1\r\n"
                f"Host: {self.config.host}:{self.config.port}\r\n"
                f"Ntrip-Version: Ntrip/2.0\r\n"
                f"User-Agent: NTRIP {self.USER_AGENT}\r\n"
                f"Authorization: {self._build_auth_header()}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            )

            sock.send(request.encode())

            # Read response header
            sock.settimeout(self.config.data_timeout)
            response = b""
            header_end = False

            while not header_end:
                chunk = sock.recv(1024)
                if not chunk:
                    return False, 0, "Connection closed by server"
                response += chunk
                if b"\r\n\r\n" in response:
                    header_end = True

            # Check response status
            header_part = response.split(b"\r\n\r\n")[0].decode("utf-8", errors="replace")
            first_line = header_part.split("\r\n")[0]

            if "200 OK" not in first_line:
                if "SOURCETABLE" in header_part:
                    return False, 0, f"Mountpoint not found (got sourcetable)"
                elif "401" in first_line:
                    return False, 0, "Authentication failed"
                elif "404" in first_line:
                    return False, 0, "Mountpoint not found"
                else:
                    return False, 0, f"HTTP error: {first_line}"

            # Receive data for duration
            bytes_received = len(response) - len(response.split(b"\r\n\r\n")[0]) - 4
            start_time = time.time()

            while time.time() - start_time < duration:
                remaining = duration - (time.time() - start_time)
                if remaining <= 0:
                    break
                sock.settimeout(min(remaining, 1.0))
                try:
                    chunk = sock.recv(4096)
                    if chunk:
                        bytes_received += len(chunk)
                    else:
                        break
                except socket.timeout:
                    continue

            return True, bytes_received, None

        except socket.timeout:
            return False, 0, "Connection timeout"
        except ConnectionRefusedError:
            return False, 0, "Connection refused"
        except socket.gaierror as e:
            return False, 0, f"DNS error: {e}"
        except Exception as e:
            return False, 0, f"Connection error: {e}"
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def check_mountpoint(
        self, mountpoint: str, available_mountpoints: Optional[List[str]] = None
    ) -> MountpointStatus:
        """Check status of a single mountpoint.

        Args:
            mountpoint: Mountpoint name to check
            available_mountpoints: Optional list of known mountpoints from sourcetable

        Returns:
            MountpointStatus with results
        """
        # If we have sourcetable, check if mountpoint exists
        if available_mountpoints is not None and mountpoint not in available_mountpoints:
            return MountpointStatus(
                mountpoint=mountpoint,
                is_active=False,
                error_message=f"Mountpoint not found on caster",
            )

        start_time = time.time()
        duration = 3.0  # Receive data for 3 seconds

        success, bytes_received, error = self._connect_and_receive(mountpoint, duration)

        elapsed = time.time() - start_time

        if success and bytes_received > 0:
            # Calculate data rate
            data_rate = bytes_received / elapsed if elapsed > 0 else 0

            # For RTCM data, we can't easily determine actual latency
            # without parsing the messages. Use data rate as proxy.
            # If data is flowing, latency is likely low.
            # Estimate latency based on data rate (rough heuristic)
            if data_rate > 100:  # >100 bytes/sec = good stream
                estimated_latency = 1.0
            elif data_rate > 10:
                estimated_latency = 5.0
            else:
                estimated_latency = 15.0

            return MountpointStatus(
                mountpoint=mountpoint,
                is_active=True,
                bytes_received=bytes_received,
                latency_seconds=estimated_latency,
                data_rate_bps=data_rate,
            )
        else:
            return MountpointStatus(
                mountpoint=mountpoint,
                is_active=False,
                bytes_received=bytes_received,
                error_message=error,
            )

    def check_status(self, station_id: str) -> NTRIPStatus:
        """Check NTRIP status for all configured mountpoints.

        Args:
            station_id: Station ID for status message

        Returns:
            NTRIPStatus with overall results
        """
        # Get sourcetable first to check if mountpoints exist
        streams = self.get_sourcetable()
        available_mountpoints = (
            [s["mountpoint"] for s in streams] if streams else None
        )

        mountpoint_statuses = []

        for mp in self.config.mountpoints:
            self.logger.debug(f"Checking NTRIP mountpoint: {mp}")
            status = self.check_mountpoint(mp, available_mountpoints)
            mountpoint_statuses.append(status)

        # Determine overall status
        active_count = sum(1 for s in mountpoint_statuses if s.is_active)
        total_count = len(mountpoint_statuses)

        if active_count == 0:
            overall_status = "critical"
            if total_count == 1:
                error = mountpoint_statuses[0].error_message or "no data"
                message = f"RTK stream DOWN - {mountpoint_statuses[0].mountpoint}: {error}"
            else:
                message = f"RTK streams DOWN - 0/{total_count} active"
        elif active_count < total_count:
            overall_status = "warning"
            active_mps = [s.mountpoint for s in mountpoint_statuses if s.is_active]
            message = f"RTK partial - {active_count}/{total_count} active: {', '.join(active_mps)}"
        else:
            # All active - check latency
            max_latency = max(
                (s.latency_seconds or 0 for s in mountpoint_statuses), default=0
            )
            if max_latency > self.config.latency_critical:
                overall_status = "critical"
                message = f"RTK high latency - {max_latency:.1f}s"
            elif max_latency > self.config.latency_warning:
                overall_status = "warning"
                message = f"RTK elevated latency - {max_latency:.1f}s"
            else:
                overall_status = "ok"
                if total_count == 1:
                    mp = mountpoint_statuses[0]
                    rate = mp.data_rate_bps or 0
                    message = f"RTK OK - {mp.mountpoint}: {rate:.0f} B/s"
                else:
                    message = f"RTK OK - {active_count}/{total_count} streams active"

        return NTRIPStatus(
            station_id=station_id,
            host=self.config.host,
            port=self.config.port,
            mountpoints=mountpoint_statuses,
            overall_status=overall_status,
            message=message,
            check_time=datetime.now(),
        )

    def get_sourcetable(self) -> Optional[List[Dict[str, str]]]:
        """Get sourcetable from NTRIP caster.

        Returns:
            List of stream entries, or None on error
        """
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.config.connect_timeout)
            sock.connect((self.config.host, self.config.port))

            # Request sourcetable (empty mountpoint)
            request = (
                f"GET / HTTP/1.0\r\n"
                f"Host: {self.config.host}:{self.config.port}\r\n"
                f"User-Agent: {self.USER_AGENT}\r\n"
                f"Accept: */*\r\n"
                f"\r\n"
            )

            sock.send(request.encode())

            # Read response
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk

            # Parse sourcetable
            text = response.decode("utf-8", errors="replace")
            streams = []

            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("STR;"):
                    # STR;mountpoint;identifier;format;...
                    parts = line.split(";")
                    if len(parts) >= 4:
                        streams.append(
                            {
                                "mountpoint": parts[1],
                                "identifier": parts[2],
                                "format": parts[3],
                                "raw": line,
                            }
                        )

            return streams

        except Exception as e:
            self.logger.debug(f"Error getting sourcetable: {e}")
            return None
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass


def check_ntrip_status(
    station_id: str,
    receivers_config: Any,
    station_config: Optional[Dict[str, Any]] = None,
) -> Optional[NTRIPStatus]:
    """Check NTRIP status for a station.

    Convenience function that creates client and checks status.

    Args:
        station_id: Station identifier
        receivers_config: ReceiversConfig instance
        station_config: Optional station-specific config

    Returns:
        NTRIPStatus or None if NTRIP not configured
    """
    config = NTRIPConfig.from_config(station_id, receivers_config, station_config)
    if not config:
        return None

    client = NTRIPClient(config)
    return client.check_status(station_id)
