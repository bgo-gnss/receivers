"""PostgreSQL database writer for GPS receiver health data.

Writes health data to block-aligned tables (block_power_status, block_receiver_status, etc.)
with backward compatibility via checkcomm view.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Type alias for psycopg2 connection (to avoid import at module level)
Connection = Any


class HealthDatabaseWriter:
    """Write health data to PostgreSQL block tables.

    The database schema follows a block-aligned structure where each table
    corresponds to a Septentrio SBF block type. This allows easy extensibility
    and clear data lineage.

    Tables:
        - stations: Station metadata
        - block_power_status: PowerStatus (SBF 4101)
        - block_receiver_status: ReceiverStatus2 (SBF 4014)
        - block_disk_status: DiskStatus (SBF 4105)
        - block_pvt_geodetic: PVTGeodetic2 (SBF 4007)
        - block_pos_covariance: PosCovGeodetic1 (SBF 5905)
        - block_ntrip_server: NTRIPServerStatus (SBF 4043)
        - agg_hourly: Hourly aggregates
        - agg_daily: Daily aggregates
    """

    def __init__(self, connection_string: Optional[str] = None):
        """Initialize database writer.

        Args:
            connection_string: PostgreSQL connection string
                             (postgresql://user:pass@host:port/dbname)
                             If None, uses environment variables or defaults
        """
        self.connection_string = connection_string
        self.logger = logging.getLogger("receivers.health.db")
        self._conn: Optional[Connection] = None
        self._station_cache: set = set()  # Cache of known station IDs

    def connect(self, database: str = "gps_health") -> bool:
        """Connect to PostgreSQL database.

        Args:
            database: Database name (default: gps_health)

        Returns:
            True if connection successful, False otherwise
        """
        try:
            import psycopg2

            if self.connection_string:
                self._conn = psycopg2.connect(self.connection_string)
            else:
                db_host = os.getenv("POSTGRES_HOST", "localhost")
                db_port = os.getenv("POSTGRES_PORT", "5432")
                db_name = os.getenv("POSTGRES_DB", database)
                db_user = os.getenv("POSTGRES_USER", os.getenv("USER", "bgo"))
                db_pass = os.getenv("POSTGRES_PASSWORD", "")

                self._conn = psycopg2.connect(
                    host=db_host,
                    port=db_port,
                    database=db_name,
                    user=db_user,
                    password=db_pass,
                )

            self.logger.info(f"Connected to PostgreSQL database: {db_name}")
            return True

        except ImportError:
            self.logger.error("psycopg2 not installed - run: pip install psycopg2-binary")
            return False
        except Exception as e:
            self.logger.error(f"Database connection failed: {e}")
            return False

    def _ensure_station(self, station_id: str, receiver_type: str = "PolaRX5") -> bool:
        """Ensure station exists in stations table.

        Args:
            station_id: Station identifier (e.g., 'ISFS')
            receiver_type: Receiver type (e.g., 'PolaRX5')

        Returns:
            True if station exists or was created
        """
        if station_id in self._station_cache:
            return True

        if not self._conn:
            return False

        try:
            with self._conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO stations (sid, receiver_type)
                    VALUES (%s, %s)
                    ON CONFLICT (sid) DO UPDATE SET
                        updated_at = NOW()
                    RETURNING sid
                """, (station_id, receiver_type))
                self._conn.commit()
                self._station_cache.add(station_id)
                return True
        except Exception as e:
            self.logger.error(f"Failed to ensure station {station_id}: {e}")
            self._conn.rollback()
            return False

    def _parse_timestamp(self, timestamp: Any) -> datetime:
        """Parse timestamp from various formats to datetime.

        Args:
            timestamp: Timestamp as string, datetime, or None

        Returns:
            datetime object (timezone-aware UTC)
        """
        if timestamp is None:
            return datetime.now(timezone.utc)
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                return timestamp.replace(tzinfo=timezone.utc)
            return timestamp
        if isinstance(timestamp, str):
            # Remove 'Z' suffix if present
            ts = timestamp.rstrip('Z')
            if '+' not in ts and 'T' in ts:
                ts += '+00:00'
            return datetime.fromisoformat(ts)
        return datetime.now(timezone.utc)

    def write_health_data(self, health_data: Dict[str, Any]) -> bool:
        """Write health data to appropriate block tables.

        This method dispatches data to the correct block tables based on
        what's available in the health_data dictionary.

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
            receiver_type = health_data.get("receiver_type", "PolaRX5")
            timestamp = self._parse_timestamp(health_data.get("timestamp"))

            # Ensure station exists
            if not self._ensure_station(station_id, receiver_type):
                return False

            metrics = health_data.get("metrics", {})

            # Write to block_power_status
            if "power" in metrics:
                self._write_power_status(station_id, timestamp, metrics["power"])

            # Write to block_receiver_status
            if any(k in metrics for k in ["cpu_load", "temperature", "uptime_seconds"]):
                self._write_receiver_status(station_id, timestamp, metrics)

            # Write to block_disk_status
            if "disk" in metrics:
                self._write_disk_status(station_id, timestamp, metrics["disk"])

            # Write to block_pvt_geodetic (from data_quality or position data)
            data_quality = health_data.get("data_quality", {})
            if data_quality or "position" in metrics:
                self._write_pvt_geodetic(station_id, timestamp, data_quality, metrics)

            # Write to block_ntrip_server
            if "ntrip" in metrics:
                self._write_ntrip_status(station_id, timestamp, metrics["ntrip"])

            self._conn.commit()
            self.logger.debug(f"Wrote health data for {station_id} at {timestamp}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to write health data to database: {e}")
            if self._conn:
                self._conn.rollback()
            return False

    def _write_power_status(self, sid: str, ts: datetime, power: Dict[str, Any]) -> None:
        """Write to block_power_status table."""
        voltage = power.get("voltage")
        power_source = power.get("source", "Vin")

        with self._conn.cursor() as cur:
            cur.execute("""
                INSERT INTO block_power_status (sid, ts, voltage, power_source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (sid, ts) DO UPDATE SET
                    voltage = EXCLUDED.voltage,
                    power_source = EXCLUDED.power_source
            """, (sid, ts, voltage, power_source))

    def _write_receiver_status(self, sid: str, ts: datetime, metrics: Dict[str, Any]) -> None:
        """Write to block_receiver_status table."""
        cpu_load = None
        temperature = None
        uptime = None

        if "cpu_load" in metrics:
            cpu_load = metrics["cpu_load"].get("percent")
        if "temperature" in metrics:
            temperature = metrics["temperature"].get("value")
        uptime = metrics.get("uptime_seconds")

        with self._conn.cursor() as cur:
            cur.execute("""
                INSERT INTO block_receiver_status (sid, ts, cpu_load, temperature, uptime_seconds)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (sid, ts) DO UPDATE SET
                    cpu_load = EXCLUDED.cpu_load,
                    temperature = EXCLUDED.temperature,
                    uptime_seconds = EXCLUDED.uptime_seconds
            """, (sid, ts, cpu_load, temperature, uptime))

    def _write_disk_status(self, sid: str, ts: datetime, disk: Dict[str, Any]) -> None:
        """Write to block_disk_status table."""
        used_mb = disk.get("used_mb")
        total_mb = disk.get("total_mb")
        usage_percent = disk.get("usage_percent")

        with self._conn.cursor() as cur:
            cur.execute("""
                INSERT INTO block_disk_status (sid, ts, used_mb, total_mb, usage_percent)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (sid, ts) DO UPDATE SET
                    used_mb = EXCLUDED.used_mb,
                    total_mb = EXCLUDED.total_mb,
                    usage_percent = EXCLUDED.usage_percent
            """, (sid, ts, used_mb, total_mb, usage_percent))

    def _write_pvt_geodetic(
        self, sid: str, ts: datetime, data_quality: Dict[str, Any], metrics: Dict[str, Any]
    ) -> None:
        """Write to block_pvt_geodetic table."""
        # Extract from data_quality (live health) or position metrics
        fix_type = data_quality.get("fix_type")
        nr_sv = data_quality.get("satellites_used") or data_quality.get("nr_sv")
        h_accuracy = data_quality.get("h_accuracy")
        v_accuracy = data_quality.get("v_accuracy")

        # Skip if no meaningful data
        if not any([fix_type, nr_sv, h_accuracy, v_accuracy]):
            return

        with self._conn.cursor() as cur:
            cur.execute("""
                INSERT INTO block_pvt_geodetic (sid, ts, fix_type, nr_sv, h_accuracy, v_accuracy)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (sid, ts) DO UPDATE SET
                    fix_type = COALESCE(EXCLUDED.fix_type, block_pvt_geodetic.fix_type),
                    nr_sv = COALESCE(EXCLUDED.nr_sv, block_pvt_geodetic.nr_sv),
                    h_accuracy = COALESCE(EXCLUDED.h_accuracy, block_pvt_geodetic.h_accuracy),
                    v_accuracy = COALESCE(EXCLUDED.v_accuracy, block_pvt_geodetic.v_accuracy)
            """, (sid, ts, fix_type, nr_sv, h_accuracy, v_accuracy))

    def _write_ntrip_status(self, sid: str, ts: datetime, ntrip: Dict[str, Any]) -> None:
        """Write to block_ntrip_server table."""
        cd_index = ntrip.get("cd_index", "NTR1")
        status = ntrip.get("status")
        error_code = ntrip.get("error_code")

        with self._conn.cursor() as cur:
            cur.execute("""
                INSERT INTO block_ntrip_server (sid, ts, cd_index, status, error_code)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (sid, ts, cd_index) DO UPDATE SET
                    status = EXCLUDED.status,
                    error_code = EXCLUDED.error_code
            """, (sid, ts, cd_index, status, error_code))

    def write_timeseries_sample(
        self,
        station_id: str,
        timestamp: datetime,
        sample: Dict[str, Any],
        receiver_type: str = "PolaRX5"
    ) -> bool:
        """Write a single timeseries sample to block tables.

        This is optimized for batch inserts from historical SBF extraction.

        Args:
            station_id: Station identifier
            timestamp: Sample timestamp
            sample: Sample dictionary with voltage, cpu_load, temperature, etc.
            receiver_type: Receiver type

        Returns:
            True if successful
        """
        if not self._conn:
            if not self.connect():
                return False

        try:
            if not self._ensure_station(station_id, receiver_type):
                return False

            ts = self._parse_timestamp(timestamp)

            # Power status
            if "voltage" in sample:
                voltage = sample["voltage"]
                if isinstance(voltage, dict):
                    voltage = voltage.get("value")
                with self._conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO block_power_status (sid, ts, voltage)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (sid, ts) DO UPDATE SET voltage = EXCLUDED.voltage
                    """, (station_id, ts, voltage))

            # Receiver status
            cpu = sample.get("cpu_load")
            if isinstance(cpu, dict):
                cpu = cpu.get("value")
            temp = sample.get("temperature")
            if isinstance(temp, dict):
                temp = temp.get("value")

            if cpu is not None or temp is not None:
                with self._conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO block_receiver_status (sid, ts, cpu_load, temperature)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (sid, ts) DO UPDATE SET
                            cpu_load = COALESCE(EXCLUDED.cpu_load, block_receiver_status.cpu_load),
                            temperature = COALESCE(EXCLUDED.temperature, block_receiver_status.temperature)
                    """, (station_id, ts, cpu, temp))

            # Disk status
            disk = sample.get("disk_usage")
            if isinstance(disk, dict):
                disk = disk.get("value")
            if disk is not None:
                with self._conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO block_disk_status (sid, ts, usage_percent)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (sid, ts) DO UPDATE SET usage_percent = EXCLUDED.usage_percent
                    """, (station_id, ts, disk))

            # Satellite visibility (aggregated count only)
            satellites = sample.get("satellites")
            if isinstance(satellites, dict):
                nr_sv = satellites.get("total")
                if nr_sv is not None:
                    with self._conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO block_pvt_geodetic (sid, ts, nr_sv)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (sid, ts) DO UPDATE SET nr_sv = EXCLUDED.nr_sv
                        """, (station_id, ts, nr_sv))

            return True

        except Exception as e:
            self.logger.error(f"Failed to write timeseries sample: {e}")
            return False

    def write_timeseries_batch(
        self,
        station_id: str,
        samples: List[Dict[str, Any]],
        receiver_type: str = "PolaRX5",
        commit_interval: int = 100
    ) -> int:
        """Write multiple timeseries samples efficiently.

        Args:
            station_id: Station identifier
            samples: List of sample dictionaries with 'time' key
            receiver_type: Receiver type
            commit_interval: Commit every N samples

        Returns:
            Number of samples successfully written
        """
        if not self._conn:
            if not self.connect():
                return 0

        if not self._ensure_station(station_id, receiver_type):
            return 0

        written = 0
        for i, sample in enumerate(samples):
            ts = sample.get("time")
            if ts and self.write_timeseries_sample(station_id, ts, sample, receiver_type):
                written += 1

            if (i + 1) % commit_interval == 0:
                self._conn.commit()
                self.logger.debug(f"Committed {i + 1} samples for {station_id}")

        self._conn.commit()
        self.logger.info(f"Wrote {written}/{len(samples)} samples for {station_id}")
        return written

    def compute_hourly_aggregate(self, station_id: str, hour: datetime) -> bool:
        """Compute hourly aggregate for a station.

        Args:
            station_id: Station identifier
            hour: Hour to aggregate (will be truncated)

        Returns:
            True if successful
        """
        if not self._conn:
            return False

        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT compute_hourly_aggregate(%s, %s)", (station_id, hour))
            self._conn.commit()
            return True
        except Exception as e:
            self.logger.error(f"Failed to compute hourly aggregate: {e}")
            self._conn.rollback()
            return False

    def close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            self._station_cache.clear()
            self.logger.debug("Closed database connection")

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
