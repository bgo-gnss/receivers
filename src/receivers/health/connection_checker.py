"""Multi-level connection health checker for GPS receivers.

Provides layered connection testing:
1. Router/Network: Ping test to verify network reachability
2. HTTP Port: Test if HTTP port responds
3. Protocol: Protocol-specific connection test (FTP, HTTP, TCP)
"""

import logging
import socket
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class HealthStatus(Enum):
    """Health status levels."""

    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class ConnectionStatus:
    """Connection status result."""

    status: HealthStatus
    response_time_ms: Optional[float] = None
    accessible: bool = False
    error_message: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary format."""
        result = {
            "status": self.status.value,
            "accessible": self.accessible,
        }
        if self.response_time_ms is not None:
            result["response_time_ms"] = round(self.response_time_ms, 2)
        if self.error_message:
            result["error"] = self.error_message
        if self.details:
            result.update(self.details)
        return result


class ConnectionChecker:
    """Multi-level connection health checker for GPS receivers."""

    def __init__(self, host: str, station_id: str = "UNKNOWN"):
        """Initialize connection checker.

        Args:
            host: Receiver hostname or IP address
            station_id: Station identifier for logging
        """
        self.host = host
        self.station_id = station_id
        self.logger = logging.getLogger(f"receivers.health.{station_id}")

    def check_all_levels(
        self,
        http_port: int = 80,
        protocol_type: str = "ftp",
        protocol_port: Optional[int] = None,
        fail_fast: bool = True,
    ) -> Dict[str, ConnectionStatus]:
        """Check all connection levels.

        Args:
            http_port: HTTP port to test (default: 80)
            protocol_type: Protocol type (ftp, http, tcp)
            protocol_port: Protocol-specific port (if different from http_port)
            fail_fast: If True, skip remaining checks when ping fails (saves time
                      on offline stations). Default True.

        Returns:
            Dictionary with connection status for each level
        """
        results = {}

        # Level 1: Router/Network ping (fast check first)
        # Use count=5 to handle lossy links (DYNC/GFUM/ELDC have ~33% loss).
        # Retry once on failure to catch bursty packet loss automatically.
        # False-offline rate: single try 0.4%, with retry ~0.002%.
        # Cost for truly offline: ~12s (two tries) — still saves 20s+ vs
        # futile port checks and extraction attempts.
        results["router_ping"] = self.check_ping(count=5, timeout=2)

        if not results["router_ping"].accessible:
            self.logger.debug(f"Ping failed for {self.station_id}, retrying once...")
            results["router_ping"] = self.check_ping(count=5, timeout=2)

        # If ping failed, don't immediately give up: some routers/firewalls
        # block ICMP while the data ports stay open and data keeps flowing
        # (e.g. ISAF — ping 100% loss but HTTP/FTP open, daily files arriving).
        # Probe the HTTP port as a fallback before declaring the host down,
        # mirroring the download path's ICMP-blocked fallback
        # (see trimble/netrs.py, netr9.py). Only when BOTH ping and the data
        # port fail do we honour fail_fast and skip the rest.
        if fail_fast and not results["router_ping"].accessible:
            fallback = self.check_http_port(http_port)
            if not fallback.accessible:
                self.logger.debug(
                    f"Ping retry failed for {self.station_id} and HTTP port "
                    f"{http_port} unreachable, skipping port checks (fail_fast)"
                )
                results["http_port"] = fallback
                results["protocol"] = ConnectionStatus(
                    status=HealthStatus.CRITICAL,
                    accessible=False,
                    error_message="Skipped: host unreachable (ping + HTTP failed)",
                    details={"type": protocol_type, "skipped": True},
                )
                return results
            # Reachable on the data port despite ping failure — the router
            # blocks ICMP. Continue with the normal checks; reuse this probe.
            self.logger.info(
                f"{self.station_id}: ICMP ping failed but HTTP port {http_port} "
                "is open — router blocks ping, continuing with port checks"
            )
            results["http_port"] = fallback

        # Level 2: HTTP port test (skip if already probed during ping fallback)
        if "http_port" not in results:
            results["http_port"] = self.check_http_port(http_port)

        # Level 3: Protocol-specific test
        if protocol_port is None:
            protocol_port = self._get_default_port(protocol_type)

        # For HTTP-only receivers (NetR9, NetRS, G10), the protocol check
        # would be a redundant socket connect to the same port.  On lossy
        # 3G/4G links this second connect can time out while the first
        # succeeded, causing false CRITICAL.  Reuse the http_port result.
        if protocol_type == "http" and protocol_port == http_port:
            results["protocol"] = results["http_port"]
        elif protocol_type == "ftp":
            results["protocol"] = self.check_ftp(protocol_port)
        elif protocol_type == "http":
            results["protocol"] = self.check_http(protocol_port)
        elif protocol_type == "tcp":
            results["protocol"] = self.check_tcp(protocol_port)
        else:
            results["protocol"] = ConnectionStatus(
                status=HealthStatus.UNKNOWN,
                error_message=f"Unknown protocol type: {protocol_type}",
            )

        return results

    def check_ping(self, count: int = 3, timeout: int = 5) -> ConnectionStatus:
        """Check router/network connectivity with ping.

        Args:
            count: Number of ping packets to send
            timeout: Timeout in seconds

        Returns:
            ConnectionStatus with ping results
        """
        self.logger.debug(f"Pinging {self.host} ({count} packets, {timeout}s timeout)")

        try:
            start_time = time.time()

            # Run ping command
            result = subprocess.run(
                ["ping", "-c", str(count), "-W", str(timeout), self.host],
                capture_output=True,
                text=True,
                timeout=count + timeout + 2,
            )

            elapsed_ms = (time.time() - start_time) * 1000

            if result.returncode == 0:
                # Parse average latency from ping output
                # Example line: "rtt min/avg/max/mdev = 1.234/2.345/3.456/0.567 ms"
                latency_ms = None
                packet_loss = 0

                for line in result.stdout.split("\n"):
                    if "rtt min/avg/max" in line or "round-trip" in line:
                        # Extract avg latency
                        parts = line.split("=")
                        if len(parts) > 1:
                            values = parts[1].strip().split("/")
                            if len(values) >= 2:
                                try:
                                    latency_ms = float(values[1])
                                except (ValueError, IndexError):
                                    pass

                    if "packet loss" in line:
                        # Extract packet loss percentage
                        try:
                            loss_str = line.split("%")[0].split()[-1]
                            packet_loss = float(loss_str)
                        except (ValueError, IndexError):
                            pass

                # Determine status based on latency and packet loss
                # Thresholds loaded from database.cfg via ThresholdConfig
                try:
                    from .metrics import load_thresholds

                    tc = load_thresholds()
                    loss_warn = tc.packet_loss_warning  # default 20.0
                    loss_crit = tc.packet_loss_critical  # default 70.0
                except Exception:
                    loss_warn, loss_crit = 20.0, 70.0

                if packet_loss >= loss_crit:
                    status = HealthStatus.CRITICAL
                elif packet_loss >= loss_warn or (latency_ms and latency_ms > 500):
                    status = HealthStatus.WARNING
                else:
                    status = HealthStatus.OK

                return ConnectionStatus(
                    status=status,
                    response_time_ms=latency_ms if latency_ms else elapsed_ms,
                    accessible=True,
                    details={
                        "latency_ms": latency_ms,
                        "packet_loss": packet_loss,
                        "packets_sent": count,
                    },
                )
            else:
                return ConnectionStatus(
                    status=HealthStatus.CRITICAL,
                    accessible=False,
                    error_message=f"Ping failed: {result.stderr.strip()}",
                )

        except subprocess.TimeoutExpired:
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"Ping timeout after {timeout}s",
            )
        except Exception as e:
            self.logger.error(f"Ping error: {e}")
            return ConnectionStatus(
                status=HealthStatus.ERROR,
                accessible=False,
                error_message=f"Ping error: {str(e)}",
            )

    def check_http_port(self, port: int = 80, timeout: int = 5) -> ConnectionStatus:
        """Check if HTTP port is open via TCP socket connect.

        Uses a raw socket connect instead of a full HTTP GET request.
        Embedded receiver web servers (PolaRX5, Trimble) can occasionally
        be slow to serve HTTP responses even though the port is open.

        Args:
            port: HTTP port number
            timeout: Connection timeout in seconds

        Returns:
            ConnectionStatus with HTTP port test results
        """
        self.logger.debug(f"Testing HTTP port {port} on {self.host}")

        sock = None
        try:
            start_time = time.time()

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((self.host, port))

            elapsed_ms = (time.time() - start_time) * 1000

            return ConnectionStatus(
                status=HealthStatus.OK,
                response_time_ms=elapsed_ms,
                accessible=True,
                details={"port": port},
            )

        except TimeoutError:
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"HTTP port {port} timeout after {timeout}s",
                details={"port": port, "error_type": "timeout"},
            )
        except ConnectionRefusedError:
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"HTTP port {port} connection refused",
                details={"port": port, "error_type": "refused"},
            )
        except OSError as e:
            import errno

            if e.errno == errno.EHOSTUNREACH:
                error_type = "unreachable"
                msg = "Host unreachable"
            elif e.errno == errno.ENETUNREACH:
                error_type = "unreachable"
                msg = "Network unreachable"
            else:
                error_type = "error"
                msg = str(e)
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"HTTP port {port}: {msg}",
                details={"port": port, "error_type": error_type},
            )
        except Exception as e:
            self.logger.error(f"HTTP port test error: {e}")
            return ConnectionStatus(
                status=HealthStatus.ERROR,
                accessible=False,
                error_message=f"HTTP port test error: {str(e)}",
                details={"port": port, "error_type": "error"},
            )
        finally:
            if sock is not None:
                sock.close()

    def check_ftp(self, port: int = 21, timeout: int = 10) -> ConnectionStatus:
        """Check FTP connection.

        Args:
            port: FTP port number
            timeout: Connection timeout in seconds

        Returns:
            ConnectionStatus with FTP connection results
        """
        self.logger.debug(f"Testing FTP connection on {self.host}:{port}")

        sock = None
        try:
            start_time = time.time()

            # Try to connect to FTP port and read welcome message
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((self.host, port))

            # Read FTP welcome banner
            banner_text = None
            has_banner = False
            try:
                raw_banner = sock.recv(1024).decode("utf-8", errors="ignore")
                has_banner = len(raw_banner) > 0 and "220" in raw_banner
                if has_banner:
                    banner_text = raw_banner.strip()
            except Exception:
                has_banner = False

            elapsed_ms = (time.time() - start_time) * 1000

            return ConnectionStatus(
                status=HealthStatus.OK,
                response_time_ms=elapsed_ms,
                accessible=True,
                details={
                    "type": "ftp",
                    "port": port,
                    "connected": True,
                    "ftp_banner": has_banner,
                    "banner_text": banner_text,
                },
            )

        except TimeoutError:
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"FTP connection timeout after {timeout}s",
                details={"type": "ftp", "port": port, "error_type": "timeout"},
            )
        except ConnectionRefusedError:
            # Port refused = host is up but service not running (instant response)
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"FTP connection refused on port {port}",
                details={"type": "ftp", "port": port, "error_type": "refused"},
            )
        except OSError as e:
            import errno

            if e.errno == errno.EHOSTUNREACH:
                error_type = "unreachable"
                msg = "Host unreachable"
            elif e.errno == errno.ENETUNREACH:
                error_type = "unreachable"
                msg = "Network unreachable"
            else:
                error_type = "error"
                msg = str(e)
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"FTP connection error: {msg}",
                details={"type": "ftp", "port": port, "error_type": error_type},
            )
        except Exception as e:
            self.logger.error(f"FTP connection error: {e}")
            return ConnectionStatus(
                status=HealthStatus.ERROR,
                accessible=False,
                error_message=f"FTP connection error: {str(e)}",
                details={"type": "ftp", "port": port, "error_type": "error"},
            )
        finally:
            if sock is not None:
                sock.close()

    def check_http(self, port: int = 8060, timeout: int = 5) -> ConnectionStatus:
        """Check HTTP protocol connectivity via TCP socket connect.

        Uses a raw socket connect like check_http_port() and check_ftp().
        Embedded receiver web servers can be slow to serve full HTTP responses
        even when the port is open. Actual HTTP protocol validation happens
        during data extraction, not here.

        Args:
            port: HTTP port number
            timeout: Connection timeout in seconds

        Returns:
            ConnectionStatus with HTTP connection results
        """
        self.logger.debug(f"Testing HTTP connection on {self.host}:{port}")

        sock = None
        try:
            start_time = time.time()

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((self.host, port))

            elapsed_ms = (time.time() - start_time) * 1000

            return ConnectionStatus(
                status=HealthStatus.OK,
                response_time_ms=elapsed_ms,
                accessible=True,
                details={
                    "type": "http",
                    "port": port,
                    "connected": True,
                },
            )

        except TimeoutError:
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"HTTP connection timeout after {timeout}s",
                details={"type": "http", "port": port},
            )
        except ConnectionRefusedError:
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"HTTP connection refused on port {port}",
                details={"type": "http", "port": port, "error_type": "refused"},
            )
        except OSError as e:
            import errno

            if e.errno == errno.EHOSTUNREACH:
                msg = "Host unreachable"
            elif e.errno == errno.ENETUNREACH:
                msg = "Network unreachable"
            else:
                msg = str(e)
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"HTTP connection failed: {msg}",
                details={"type": "http", "port": port},
            )
        except Exception as e:
            self.logger.error(f"HTTP connection error: {e}")
            return ConnectionStatus(
                status=HealthStatus.ERROR,
                accessible=False,
                error_message=f"HTTP connection error: {str(e)}",
                details={"type": "http", "port": port},
            )
        finally:
            if sock is not None:
                sock.close()

    def check_tcp(self, port: int, timeout: int = 5) -> ConnectionStatus:
        """Check generic TCP connection.

        Args:
            port: TCP port number
            timeout: Connection timeout in seconds

        Returns:
            ConnectionStatus with TCP connection results
        """
        self.logger.debug(f"Testing TCP connection on {self.host}:{port}")

        sock = None
        try:
            start_time = time.time()

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((self.host, port))

            elapsed_ms = (time.time() - start_time) * 1000

            return ConnectionStatus(
                status=HealthStatus.OK,
                response_time_ms=elapsed_ms,
                accessible=True,
                details={
                    "type": "tcp",
                    "port": port,
                    "connected": True,
                },
            )

        except TimeoutError:
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"TCP connection timeout after {timeout}s",
                details={"type": "tcp", "port": port},
            )
        except ConnectionRefusedError:
            return ConnectionStatus(
                status=HealthStatus.CRITICAL,
                accessible=False,
                error_message=f"TCP connection refused on port {port}",
                details={"type": "tcp", "port": port},
            )
        except Exception as e:
            self.logger.error(f"TCP connection error: {e}")
            return ConnectionStatus(
                status=HealthStatus.ERROR,
                accessible=False,
                error_message=f"TCP connection error: {str(e)}",
                details={"type": "tcp", "port": port},
            )
        finally:
            if sock is not None:
                sock.close()

    @staticmethod
    def _get_default_port(protocol_type: str) -> int:
        """Get default port for protocol type.

        Args:
            protocol_type: Protocol type (ftp, http, tcp)

        Returns:
            Default port number
        """
        defaults = {
            "ftp": 21,
            "http": 80,
            "tcp": 80,
        }
        return defaults.get(protocol_type.lower(), 80)

    def get_overall_status(
        self, results: Dict[str, ConnectionStatus]
    ) -> Tuple[HealthStatus, str]:
        """Determine overall connection status from all levels.

        Args:
            results: Dictionary of connection test results

        Returns:
            Tuple of (overall_status, summary_message)
        """
        statuses = [result.status for result in results.values()]

        # Determine overall status (worst status wins)
        if HealthStatus.CRITICAL in statuses:
            overall = HealthStatus.CRITICAL
            message = "Connection critical: "
        elif HealthStatus.ERROR in statuses:
            overall = HealthStatus.ERROR
            message = "Connection error: "
        elif HealthStatus.WARNING in statuses:
            overall = HealthStatus.WARNING
            message = "Connection degraded: "
        elif HealthStatus.OK in statuses and all(
            s == HealthStatus.OK for s in statuses
        ):
            overall = HealthStatus.OK
            message = "All connection levels OK"
        else:
            overall = HealthStatus.UNKNOWN
            message = "Connection status unknown"

        # Add details about failed levels
        if overall != HealthStatus.OK:
            failed = [
                level
                for level, result in results.items()
                if result.status != HealthStatus.OK
            ]
            if failed:
                message += f"{', '.join(failed)} failed"

        return overall, message
