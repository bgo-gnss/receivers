"""Status task implementation for real-time receiver monitoring.

StatusTask performs live status checks on GPS receivers and sends results
to PostgreSQL database and Icinga monitoring system. This is the equivalent
of running: `receivers health STATION --icinga --save-db`

Key characteristics:
- REALTIME priority: Never blocked by backfill tasks
- Runs every 15 minutes for active monitoring
- Connects directly to receiver for current status
- Sends passive check results to Icinga
- Writes health metrics to PostgreSQL
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from ..task_interface import (
    ScheduledTask,
    TaskConfig,
    TaskPriority,
    TaskResult,
)


class StatusTask(ScheduledTask):
    """Live receiver status check task.

    Performs real-time health monitoring of GPS receivers:
    1. Connects to receiver and extracts health metrics
    2. Evaluates thresholds for alerts
    3. Sends results to PostgreSQL database
    4. Sends passive check results to Icinga

    This is the scheduled equivalent of:
        receivers health STATION --icinga --save-db
    """

    # Default to REALTIME priority for status checks
    default_priority = TaskPriority.REALTIME

    def __init__(
        self,
        station_id: str,
        config: TaskConfig,
        logger: Optional[logging.Logger] = None,
        send_to_database: bool = True,
        send_to_icinga: bool = True,
    ):
        """Initialize status task.

        Args:
            station_id: Station identifier
            config: Task configuration
            logger: Optional logger instance
            send_to_database: Write health data to PostgreSQL
            send_to_icinga: Send passive checks to Icinga
        """
        super().__init__(station_id, config, logger)
        self.send_to_database = send_to_database
        self.send_to_icinga = send_to_icinga
        self._station_config: Optional[Dict[str, Any]] = None
        self._receiver = None

    def get_time_parameters(self) -> Tuple[datetime, datetime]:
        """Get time parameters for status check.

        Status checks are point-in-time, not ranges. Returns current time
        as both start and end.

        Returns:
            Tuple of (now, now)
        """
        now = datetime.now(timezone.utc)
        return (now, now)

    def validate_prerequisites(self) -> Tuple[bool, Optional[str]]:
        """Validate that status check can be performed.

        Checks:
        - Station configuration exists
        - Receiver can be created
        - Required connection parameters present

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            from ...cli.main import get_station_config

            self._station_config = get_station_config(self.station_id)
            if not self._station_config:
                return False, f"No configuration found for station {self.station_id}"

            # Check required connection fields
            if not self._station_config.get('ip_number'):
                return False, f"No IP address configured for {self.station_id}"

            return True, None

        except Exception as e:
            return False, f"Validation failed: {str(e)}"

    def execute(self) -> TaskResult:
        """Execute the status check.

        Performs:
        1. Connects to receiver and gathers health data
        2. Writes to PostgreSQL if enabled
        3. Sends to Icinga if enabled

        Returns:
            TaskResult with status check details
        """
        start_time = time.time()

        try:
            self.logger.info(f"Starting status check: {self.station_id}")

            # Validate prerequisites
            valid, error = self.validate_prerequisites()
            if not valid:
                return self._create_failure_result(
                    start_time,
                    'validation_failed',
                    f"Validation failed: {error}",
                    f"ValidationError: {error}"
                )

            # Create receiver and gather health data
            from ...cli.main import create_receiver
            from ...health.live_health import gather_comprehensive_health

            # Station config validated in validate_prerequisites
            station_config = self._station_config
            if station_config is None:
                return self._create_failure_result(
                    start_time,
                    'error',
                    "Station config not loaded",
                    "InternalError: station config is None"
                )

            self._receiver = create_receiver(self.station_id, station_config)

            # Gather comprehensive health (receiver metrics + NTRIP checks)
            health_data = gather_comprehensive_health(
                station_id=self.station_id,
                station_config=station_config,
                receiver=self._receiver,
                include_files=False,  # File checks are separate
                include_ntrip=True,   # Include NTRIP/RTK status
            )

            # Write to database
            db_success = False
            if self.send_to_database:
                db_success = self._write_to_database(health_data)
                # Also write connectivity and port status
                self._write_ping_status(health_data)
                self._write_port_status(health_data)

            # Send to Icinga
            icinga_results = {}
            if self.send_to_icinga:
                icinga_results = self._send_to_icinga(health_data)

            # Calculate duration
            duration = time.time() - start_time

            # Build result
            overall_status = health_data.get('overall_status', 'unknown')
            success = overall_status in ('healthy', 'ok', 'warning')

            message = f"Status: {overall_status}"
            if db_success:
                message += ", saved to DB"
            if icinga_results:
                icinga_ok = sum(1 for r in icinga_results.values() if r.get('success'))
                message += f", {icinga_ok}/{len(icinga_results)} Icinga checks sent"

            self.logger.info(
                f"Status check complete: {self.station_id} - {overall_status} ({duration:.1f}s)"
            )

            return TaskResult(
                success=success,
                status=overall_status,
                duration=duration,
                message=message,
                data={
                    'station_id': self.station_id,
                    'overall_status': overall_status,
                    'db_write_success': db_success,
                    'icinga_checks_sent': len(icinga_results),
                    'metrics': health_data.get('metrics', {}),
                },
                metrics={
                    'check_duration': duration,
                    'db_enabled': self.send_to_database,
                    'icinga_enabled': self.send_to_icinga,
                }
            )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"{type(e).__name__}: {str(e)}"
            self.logger.error(f"Status check failed: {self.station_id} - {error_msg}")

            # Record station as offline when health check fails
            if self.send_to_database:
                self._write_ping_status({
                    'connection': {
                        'tcp': {'status': 'failed'},
                        'error': error_msg
                    }
                })

            return self._create_failure_result(
                start_time,
                'error',
                f"Status check failed: {str(e)}",
                error_msg
            )

    def _write_ping_status(self, health_data: Dict[str, Any]) -> bool:
        """Write connectivity status to block_ping_status table.

        Extracts connection info from health_data and stores it for
        online/offline tracking in Grafana dashboards.

        Args:
            health_data: Health data dictionary with connection info

        Returns:
            True if write successful
        """
        try:
            import os
            import psycopg2

            # Extract connection status from health data
            # Try router_ping first (from ConnectionChecker.check_all_levels)
            connection = health_data.get('connection', {})
            router_ping = connection.get('router_ping', {})
            metrics = health_data.get('metrics', {})
            ports = metrics.get('ports', {})

            # Determine if online based on ping result
            is_online = router_ping.get('accessible', False)

            # Get response time from ping if available
            response_time_ms = router_ping.get('response_time_ms')
            packet_loss = router_ping.get('packet_loss')
            error_message = router_ping.get('error') if not is_online else None

            # Fallback to tcp status if no router_ping data
            if not router_ping:
                tcp_status = connection.get('tcp', {}).get('status', 'unknown')
                is_online = tcp_status in ('ok', 'connected', 'success')
                response_time_ms = connection.get('tcp', {}).get('response_time_ms')
                error_message = connection.get('error') if not is_online else None
                packet_loss = None

            # Check if all service ports are closed - station is offline even if host pings
            if ports:
                ftp_open = ports.get('ftp', {}).get('open', False)
                http_open = ports.get('http', {}).get('open', False)
                control_open = ports.get('control', {}).get('open', False)

                if not ftp_open and not http_open and not control_open:
                    is_online = False
                    error_message = "all ports closed"

            db_host = os.getenv("POSTGRES_HOST", "localhost")
            db_port = os.getenv("POSTGRES_PORT", "5432")
            db_name = os.getenv("POSTGRES_DB", "gps_health")
            db_user = os.getenv("POSTGRES_USER", os.getenv("USER", "bgo"))
            db_pass = os.getenv("POSTGRES_PASSWORD", "")

            conn = psycopg2.connect(
                host=db_host,
                port=db_port,
                database=db_name,
                user=db_user,
                password=db_pass,
            )

            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO block_ping_status (
                            sid, ts, is_online, response_time_ms, packet_loss, error_message
                        ) VALUES (%s, NOW(), %s, %s, %s, %s)
                        ON CONFLICT (sid, ts) DO UPDATE SET
                            is_online = EXCLUDED.is_online,
                            response_time_ms = EXCLUDED.response_time_ms,
                            packet_loss = EXCLUDED.packet_loss,
                            error_message = EXCLUDED.error_message
                    """, (
                        self.station_id,
                        is_online,
                        response_time_ms,
                        packet_loss,
                        error_message
                    ))
                conn.commit()
                return True

            finally:
                conn.close()

        except ImportError:
            self.logger.debug("psycopg2 not available for ping status")
            return False
        except Exception as e:
            self.logger.debug(f"Ping status write failed: {e}")
            return False

    def _write_port_status(self, health_data: Dict[str, Any]) -> bool:
        """Write port status to block_port_status table.

        Extracts port check results from health_data and stores them for
        dashboard visualization.

        Args:
            health_data: Health data dictionary with connection info

        Returns:
            True if write successful
        """
        try:
            import os
            import psycopg2

            # Extract connection status from health data
            connection = health_data.get('connection', {})

            # Get protocol port status (FTP for Septentrio, HTTP for Trimble)
            # The protocol field contains the download protocol info
            protocol = connection.get('protocol', {})
            download_port = protocol.get('port')
            download_status = 'open' if protocol.get('accessible') else protocol.get('error_type', 'error')
            download_response_ms = protocol.get('response_time_ms')

            # Get HTTP port status (health/web interface)
            http_port_data = connection.get('http_port', {})
            health_port = http_port_data.get('port')
            health_status = 'open' if http_port_data.get('accessible') else http_port_data.get('error_type', 'error')
            health_response_ms = http_port_data.get('response_time_ms')

            db_host = os.getenv("POSTGRES_HOST", "localhost")
            db_port = os.getenv("POSTGRES_PORT", "5432")
            db_name = os.getenv("POSTGRES_DB", "gps_health")
            db_user = os.getenv("POSTGRES_USER", os.getenv("USER", "bgo"))
            db_pass = os.getenv("POSTGRES_PASSWORD", "")

            conn = psycopg2.connect(
                host=db_host,
                port=db_port,
                database=db_name,
                user=db_user,
                password=db_pass,
            )

            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO block_port_status (
                            sid, ts, download_port, download_status, download_response_ms,
                            health_port, health_status, health_response_ms
                        ) VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (sid, ts) DO UPDATE SET
                            download_port = EXCLUDED.download_port,
                            download_status = EXCLUDED.download_status,
                            download_response_ms = EXCLUDED.download_response_ms,
                            health_port = EXCLUDED.health_port,
                            health_status = EXCLUDED.health_status,
                            health_response_ms = EXCLUDED.health_response_ms
                    """, (
                        self.station_id,
                        download_port,
                        download_status,
                        download_response_ms,
                        health_port,
                        health_status,
                        health_response_ms
                    ))
                conn.commit()
                return True

            finally:
                conn.close()

        except ImportError:
            self.logger.debug("psycopg2 not available for port status")
            return False
        except Exception as e:
            self.logger.debug(f"Port status write failed: {e}")
            return False

    def _write_to_database(self, health_data: Dict[str, Any]) -> bool:
        """Write health data to PostgreSQL.

        Args:
            health_data: Health data dictionary

        Returns:
            True if write successful
        """
        try:
            from ...health.db_writer import HealthDatabaseWriter

            with HealthDatabaseWriter() as db:
                return db.write_health_data(health_data)

        except ImportError:
            self.logger.warning("PostgreSQL writer not available (psycopg2 not installed)")
            return False
        except Exception as e:
            self.logger.error(f"Database write failed: {e}")
            return False

    def _send_to_icinga(self, health_data: Dict[str, Any]) -> Dict[str, Any]:
        """Send health checks to Icinga.

        Args:
            health_data: Health data dictionary

        Returns:
            Dictionary mapping check name to API response
        """
        try:
            from ...monitoring.icinga_client import IcingaClient

            client = IcingaClient()
            return client.send_health_from_json(health_data)

        except ImportError:
            self.logger.warning("Icinga client not available")
            return {}
        except Exception as e:
            self.logger.error(f"Icinga send failed: {e}")
            return {}

    def _create_failure_result(
        self,
        start_time: float,
        status: str,
        message: str,
        error: str,
    ) -> TaskResult:
        """Create a failure TaskResult."""
        return TaskResult(
            success=False,
            status=status,
            duration=time.time() - start_time,
            message=message,
            data={'station_id': self.station_id},
            error=error,
        )
