"""Unified writer for ping and port connectivity status.

Consolidates duplicate connectivity writing code from:
- cli/main.py: _write_connectivity_status()
- scheduling/bulk_scheduler.py: _write_connectivity_status()
- scheduling/tasks/status_task.py: _write_ping_status(), _write_port_status()

All callers should use ConnectivityWriter instead of inline SQL.

Key fix: Uses explicit timestamps from health data instead of NOW(),
which caused the "Last Checked" 20.6-hour desynchronization bug in
Grafana dashboards.

Usage:
    from receivers.health.connectivity_writer import ConnectivityWriter

    writer = ConnectivityWriter(logger)
    writer.write_connectivity_status(station_id, health_data)
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .database_factory import DatabaseConnectionFactory

logger = logging.getLogger(__name__)


class ConnectivityWriter:
    """Unified writer for ping and port status tables.

    Writes to:
    - block_ping_status: Online/offline status with ICMP ping results
    - block_port_status: Download and health port status

    Uses health data timestamp for consistent time alignment across
    all block tables (power, receiver, ping, port).
    """

    def __init__(self, log: Optional[logging.Logger] = None):
        self.logger = log or logger

    def write_connectivity_status(
        self,
        station_id: str,
        health_data: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> bool:
        """Write ping and port status to database.

        Args:
            station_id: Station identifier (e.g., 'ELDC')
            health_data: Health data dictionary with connection info
            timestamp: Explicit timestamp. If None, extracted from health_data
                       or falls back to current UTC time.

        Returns:
            True if write successful, False otherwise.
        """
        ts = timestamp or self._extract_timestamp(health_data)

        try:
            with DatabaseConnectionFactory.connection() as conn:
                self._write_ping_status(conn, station_id, health_data, ts)
                self._write_port_status(conn, station_id, health_data, ts)

            self.logger.debug(f"Wrote connectivity status for {station_id}")
            return True

        except ImportError:
            self.logger.debug("psycopg2 not available for connectivity status")
            return False
        except Exception as e:
            self.logger.warning(f"Failed to write connectivity status for {station_id}: {e}")
            return False

    def write_ping_only(
        self,
        station_id: str,
        health_data: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> bool:
        """Write only ping status (for error/offline recording).

        Used when a health check fails and we only want to record
        the station as offline without port data.

        Args:
            station_id: Station identifier
            health_data: Health data (may be minimal for error cases)
            timestamp: Explicit timestamp

        Returns:
            True if write successful.
        """
        ts = timestamp or self._extract_timestamp(health_data)

        try:
            with DatabaseConnectionFactory.connection() as conn:
                self._write_ping_status(conn, station_id, health_data, ts)

            return True

        except ImportError:
            self.logger.debug("psycopg2 not available for ping status")
            return False
        except Exception as e:
            self.logger.warning(f"Failed to write ping status for {station_id}: {e}")
            return False

    def _extract_timestamp(self, health_data: Dict[str, Any]) -> datetime:
        """Extract timestamp from health data.

        Tries health_data['timestamp'] first, falls back to UTC now.
        This ensures ping/port timestamps align with power/receiver
        timestamps written by HealthDatabaseWriter.

        Args:
            health_data: Health data dictionary

        Returns:
            Timezone-aware datetime in UTC.
        """
        ts = health_data.get("timestamp")
        if ts is None:
            return datetime.now(timezone.utc)

        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                return ts.replace(tzinfo=timezone.utc)
            return ts

        if isinstance(ts, str):
            ts_str = ts.rstrip("Z")
            if "+" not in ts_str and "T" in ts_str:
                ts_str += "+00:00"
            try:
                return datetime.fromisoformat(ts_str)
            except ValueError:
                self.logger.warning(f"Could not parse timestamp '{ts}', using now")
                return datetime.now(timezone.utc)

        return datetime.now(timezone.utc)

    def _write_ping_status(
        self,
        conn: Any,
        station_id: str,
        health_data: Dict[str, Any],
        ts: datetime,
    ) -> None:
        """Write to block_ping_status table.

        Determines online status from ICMP ping result + port accessibility.
        A station is online only if the router responds to ping AND at least
        one service port is open.

        Args:
            conn: Database connection
            station_id: Station identifier
            health_data: Health data dictionary
            ts: Timestamp to use for the record
        """
        connection = health_data.get("connection", {})
        metrics = health_data.get("metrics", {})
        ports = metrics.get("ports", {})

        # ICMP ping result
        router_ping = connection.get("router_ping", {})
        ping_accessible = router_ping.get("accessible", False)
        response_time_ms = router_ping.get("response_time_ms")
        packet_loss = router_ping.get("packet_loss")

        # Check if any service port is open
        all_ports_closed = True
        if ports:
            for port_name in ("ftp", "http", "control"):
                port_info = ports.get(port_name, {})
                if isinstance(port_info, dict) and port_info.get("open", False):
                    all_ports_closed = False
                    break

        # Online = ping works AND at least one port open
        if ping_accessible:
            is_online = not all_ports_closed
        else:
            is_online = False

        # Determine error message
        error_message = None
        if not is_online:
            if not ping_accessible:
                error_message = router_ping.get("error", "ping failed - host unreachable")
            elif all_ports_closed:
                error_message = "all ports closed - port forwarding missing"

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO block_ping_status (
                    sid, ts, is_online, response_time_ms, packet_loss, error_message
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (sid, ts) DO UPDATE SET
                    is_online = EXCLUDED.is_online,
                    response_time_ms = EXCLUDED.response_time_ms,
                    packet_loss = EXCLUDED.packet_loss,
                    error_message = EXCLUDED.error_message
                """,
                (station_id, ts, is_online, response_time_ms, packet_loss, error_message),
            )

    def _write_port_status(
        self,
        conn: Any,
        station_id: str,
        health_data: Dict[str, Any],
        ts: datetime,
    ) -> None:
        """Write to block_port_status table.

        Extracts download port (FTP for Septentrio, HTTP for Trimble)
        and health port (always HTTP) from health data.

        Args:
            conn: Database connection
            station_id: Station identifier
            health_data: Health data dictionary
            ts: Timestamp to use for the record
        """
        connection = health_data.get("connection", {})
        metrics = health_data.get("metrics", {})
        ports = metrics.get("ports", {})

        # Determine download protocol (FTP for Septentrio, HTTP for Trimble)
        protocol_info = connection.get("protocol", {})
        protocol_type = protocol_info.get("type", "ftp")

        ftp_port = ports.get("ftp", {})
        http_port = ports.get("http", {})

        # Download port depends on receiver type
        if protocol_type == "http":
            download_port_info = http_port
        else:
            download_port_info = ftp_port

        if download_port_info and isinstance(download_port_info, dict):
            download_port = download_port_info.get("port")
            download_status = download_port_info.get("status", "unknown")
            download_response_ms = download_port_info.get("response_time_ms")
        else:
            download_port = None
            download_status = "unknown"
            download_response_ms = None

        # Health port is always HTTP (web interface)
        if http_port and isinstance(http_port, dict):
            health_port = http_port.get("port")
            health_status = http_port.get("status", "unknown")
            health_response_ms = http_port.get("response_time_ms")
        else:
            health_port = None
            health_status = "unknown"
            health_response_ms = None

        # Also check connection.protocol and connection.http_port as fallback
        # (status_task.py format)
        if download_port is None and protocol_info:
            download_port = protocol_info.get("port")
            if protocol_info.get("accessible"):
                download_status = "open"
            elif protocol_info.get("error_type"):
                download_status = protocol_info["error_type"]
            download_response_ms = protocol_info.get("response_time_ms")

        http_port_data = connection.get("http_port", {})
        if health_port is None and http_port_data:
            health_port = http_port_data.get("port")
            if http_port_data.get("accessible"):
                health_status = "open"
            elif http_port_data.get("error_type"):
                health_status = http_port_data["error_type"]
            health_response_ms = http_port_data.get("response_time_ms")

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO block_port_status (
                    sid, ts, download_port, download_status, download_response_ms,
                    health_port, health_status, health_response_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sid, ts) DO UPDATE SET
                    download_port = EXCLUDED.download_port,
                    download_status = EXCLUDED.download_status,
                    download_response_ms = EXCLUDED.download_response_ms,
                    health_port = EXCLUDED.health_port,
                    health_status = EXCLUDED.health_status,
                    health_response_ms = EXCLUDED.health_response_ms
                """,
                (
                    station_id,
                    ts,
                    download_port,
                    download_status,
                    download_response_ms,
                    health_port,
                    health_status,
                    health_response_ms,
                ),
            )
