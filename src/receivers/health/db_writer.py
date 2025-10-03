"""PostgreSQL database writer for GPS receiver health data.

Writes health data to checkcomm table with backward compatibility.
"""

import logging
import json
from typing import Dict, Any, Optional
from datetime import datetime


class HealthDatabaseWriter:
    """Write health data to PostgreSQL checkcomm table."""

    def __init__(self, connection_string: Optional[str] = None):
        """Initialize database writer.

        Args:
            connection_string: PostgreSQL connection string
                             (postgresql://user:pass@host:port/dbname)
                             If None, tries to use environment variable
        """
        self.connection_string = connection_string
        self.logger = logging.getLogger("receivers.health.db")
        self._conn = None

    def connect(self) -> bool:
        """Connect to PostgreSQL database.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            import psycopg2

            if self.connection_string:
                self._conn = psycopg2.connect(self.connection_string)
            else:
                # Try environment variables
                import os
                db_host = os.getenv("POSTGRES_HOST", "localhost")
                db_port = os.getenv("POSTGRES_PORT", "5432")
                db_name = os.getenv("POSTGRES_DB", "gps")
                db_user = os.getenv("POSTGRES_USER", "gpsuser")
                db_pass = os.getenv("POSTGRES_PASSWORD", "")

                self._conn = psycopg2.connect(
                    host=db_host,
                    port=db_port,
                    database=db_name,
                    user=db_user,
                    password=db_pass,
                )

            self.logger.info("Connected to PostgreSQL database")
            return True

        except ImportError:
            self.logger.error("psycopg2 not installed - cannot connect to database")
            return False
        except Exception as e:
            self.logger.error(f"Database connection failed: {e}")
            return False

    def write_health_data(self, health_data: Dict[str, Any]) -> bool:
        """Write health data to checkcomm table.

        Args:
            health_data: Health data dictionary following health-data-spec.md

        Returns:
            True if write successful, False otherwise
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            station_id = health_data.get("station_id", "UNKN")
            timestamp = health_data.get("timestamp", datetime.utcnow().isoformat())

            # Parse timestamp if string
            if isinstance(timestamp, str):
                # Remove 'Z' suffix if present
                timestamp = timestamp.rstrip('Z')
                dt = datetime.fromisoformat(timestamp)
            else:
                dt = timestamp

            # Extract metrics for legacy columns
            metrics = health_data.get("metrics", {})
            temperature = None
            voltage = None

            if "temperature" in metrics:
                temperature = metrics["temperature"].get("value")
            if "power" in metrics:
                voltage = metrics["power"].get("voltage")

            # Prepare JSONB columns
            connection_data = health_data.get("connection", {})

            # Build router status JSONB (router_ping data)
            rout_stat = json.dumps(connection_data.get("router_ping", {}))

            # Build receiver status JSONB (http_port + protocol data)
            recv_stat = json.dumps({
                "http_port": connection_data.get("http_port", {}),
                "protocol": connection_data.get("protocol", {}),
            })

            # Full metrics JSONB
            recv_metrics = json.dumps(metrics)

            # Data quality JSONB
            data_quality = json.dumps(health_data.get("data_quality", {}))

            # Overall status
            overall_status = health_data.get("overall_status", "unknown")

            # Insert or update
            with self._conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO checkcomm (
                        sid, timestamp,
                        recv_temp, recv_volt,
                        rout_stat, recv_stat, recv_metrics, data_quality,
                        overall_status
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (sid, timestamp)
                    DO UPDATE SET
                        recv_temp = EXCLUDED.recv_temp,
                        recv_volt = EXCLUDED.recv_volt,
                        rout_stat = EXCLUDED.rout_stat,
                        recv_stat = EXCLUDED.recv_stat,
                        recv_metrics = EXCLUDED.recv_metrics,
                        data_quality = EXCLUDED.data_quality,
                        overall_status = EXCLUDED.overall_status
                """, (
                    station_id, dt,
                    temperature, voltage,
                    rout_stat, recv_stat, recv_metrics, data_quality,
                    overall_status
                ))

            self._conn.commit()
            self.logger.info(f"Wrote health data for {station_id} to database")
            return True

        except Exception as e:
            self.logger.error(f"Failed to write health data to database: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            self.logger.debug("Closed database connection")

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
