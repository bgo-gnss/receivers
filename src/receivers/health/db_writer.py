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
        - block_pvt_geodetic: PVTGeodetic2 (SBF 4007) - position and satellite count
        - block_satellite_tracking: ChannelStatus (SBF 4013) - constellation breakdown
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
            from .database_factory import DatabaseConnectionFactory

            self._conn = DatabaseConnectionFactory.get_connection(
                database=database,
                connection_string=self.connection_string,
            )
            self.logger.info("Connected to PostgreSQL database")
            return True

        except ImportError:
            self.logger.error("psycopg2 not installed - run: pip install psycopg2-binary")
            return False
        except Exception as e:
            self.logger.error(f"Database connection failed: {e}")
            return False

    def _ensure_station(
        self,
        station_id: str,
        receiver_type: str = "PolaRX5",
        power_type: Optional[str] = None,
    ) -> bool:
        """Ensure station exists in stations table.

        Args:
            station_id: Station identifier (e.g., 'ISFS')
            receiver_type: Receiver type (e.g., 'PolaRX5')
            power_type: Power supply type ('battery' or 'mains')

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
                    INSERT INTO stations (sid, receiver_type, power_type)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (sid) DO UPDATE SET
                        receiver_type = EXCLUDED.receiver_type,
                        power_type = COALESCE(EXCLUDED.power_type, stations.power_type),
                        updated_at = NOW()
                    RETURNING sid
                """, (station_id, receiver_type, power_type))
                self._conn.commit()
                self._station_cache.add(station_id)
                return True
        except Exception as e:
            self.logger.error(f"Failed to ensure station {station_id}: {e}")
            self._conn.rollback()
            return False

    def _update_station_identity(
        self, station_id: str, timestamp: Any, identity: Dict[str, Any]
    ) -> None:
        """Update station identity columns and write to block_receiver_setup.

        Updates the stations table with the latest identity data and writes
        a timestamped record to block_receiver_setup for historical tracking.

        Args:
            station_id: Station identifier
            timestamp: Check timestamp
            identity: Identity dict with receiver_model, firmware_version, serial_number
        """
        firmware = identity.get("firmware_version")
        model = identity.get("receiver_model")
        serial = identity.get("serial_number")

        if not any([firmware, model, serial]):
            return

        ts = self._parse_timestamp(timestamp)

        try:
            with self._conn.cursor() as cur:
                # Update stations table with latest identity
                cur.execute("""
                    UPDATE stations SET
                        firmware_version = COALESCE(%s, firmware_version),
                        detected_model = COALESCE(%s, detected_model),
                        serial_number = COALESCE(%s, serial_number),
                        identity_last_checked = NOW()
                    WHERE sid = %s
                """, (firmware, model, serial, station_id))

                # Write historical record to block_receiver_setup
                cur.execute("""
                    INSERT INTO block_receiver_setup (sid, ts, rx_name, rx_version, rx_serial_number)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (sid, ts) DO UPDATE SET
                        rx_name = COALESCE(EXCLUDED.rx_name, block_receiver_setup.rx_name),
                        rx_version = COALESCE(EXCLUDED.rx_version, block_receiver_setup.rx_version),
                        rx_serial_number = COALESCE(EXCLUDED.rx_serial_number, block_receiver_setup.rx_serial_number)
                """, (station_id, ts, model, firmware, serial))

            # Check for mismatch
            from .receiver_fingerprint import check_identity_mismatch
            configured_type = None
            # Look up configured type from cache or query
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT receiver_type FROM stations WHERE sid = %s",
                    (station_id,),
                )
                row = cur.fetchone()
                if row:
                    configured_type = row[0]

            if configured_type:
                mismatch = check_identity_mismatch(configured_type, identity)
                if mismatch:
                    self.logger.warning(f"[{station_id}] {mismatch}")

        except Exception as e:
            self.logger.debug(f"Station identity update failed for {station_id}: {e}")

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
            power_type = health_data.get("power_type")
            timestamp = self._parse_timestamp(health_data.get("timestamp"))

            # Ensure station exists
            if not self._ensure_station(station_id, receiver_type, power_type):
                return False

            # Persist receiver identity if available
            identity = health_data.get("receiver_identity")
            if not identity:
                # build_health_status puts it under the receiver type key
                receiver_key = receiver_type.lower()
                rx_specific = health_data.get(receiver_key, {})
                if isinstance(rx_specific, dict) and rx_specific.get("receiver_model"):
                    identity = rx_specific
            if identity:
                self._update_station_identity(station_id, timestamp, identity)

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
            if "ntrip_server" in metrics:
                self._write_ntrip_status(station_id, timestamp, metrics["ntrip_server"])
            elif "ntrip" in metrics:
                self._write_ntrip_status(station_id, timestamp, metrics["ntrip"])

            # Write to block_ntrip_client
            if "ntrip_client" in metrics:
                self._write_ntrip_client(station_id, timestamp, metrics["ntrip_client"])

            # Write to block_satellite_tracking (constellation breakdown)
            if "satellites" in metrics:
                self._write_satellite_tracking(station_id, timestamp, metrics["satellites"])

            # Write to block_health_summary (composite status + port checks)
            overall_status = health_data.get("overall_status")
            ports = metrics.get("ports")
            if overall_status or ports:
                status_details = self._build_status_details(metrics, overall_status)
                self._write_health_summary(
                    station_id, timestamp, overall_status, ports, status_details
                )

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
        rx_status = None

        if "cpu_load" in metrics:
            cpu_load = metrics["cpu_load"].get("percent")
        if "temperature" in metrics:
            temperature = metrics["temperature"].get("value")
        # Support both PolaRX5 flat format and Trimble nested format
        uptime = metrics.get("uptime_seconds")
        if uptime is None:
            uptime_data = metrics.get("uptime", {})
            if isinstance(uptime_data, dict) and uptime_data.get("seconds"):
                uptime = uptime_data["seconds"]
        rx_status = metrics.get("rx_status")

        with self._conn.cursor() as cur:
            cur.execute("""
                INSERT INTO block_receiver_status (sid, ts, cpu_load, temperature, uptime_seconds, rx_status)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (sid, ts) DO UPDATE SET
                    cpu_load = EXCLUDED.cpu_load,
                    temperature = EXCLUDED.temperature,
                    uptime_seconds = EXCLUDED.uptime_seconds,
                    rx_status = EXCLUDED.rx_status
            """, (sid, ts, cpu_load, temperature, uptime, rx_status))

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
        # Extract from metrics.position (live TCP extraction) or data_quality (historical)
        position = metrics.get("position", {})

        # Prefer position metrics, fall back to data_quality
        # Handle both PolaRX5 (fix_mode) and Trimble (fix_type) field names
        fix_type = position.get("fix_mode") or position.get("fix_type") or data_quality.get("fix_type")
        # Truncate to fit VARCHAR column (apply migration 005 to widen to 50)
        if fix_type and len(fix_type) > 50:
            fix_type = fix_type[:50]
        nr_sv = position.get("satellites_used") or data_quality.get("satellites_used") or data_quality.get("nr_sv")
        h_accuracy = position.get("h_accuracy_m") or data_quality.get("h_accuracy")
        v_accuracy = position.get("v_accuracy_m") or data_quality.get("v_accuracy")
        latitude = position.get("latitude")
        longitude = position.get("longitude")
        height = position.get("height")

        # Skip if no meaningful data
        if not any([fix_type, nr_sv, h_accuracy, v_accuracy, latitude, longitude]):
            return

        with self._conn.cursor() as cur:
            cur.execute("""
                INSERT INTO block_pvt_geodetic (sid, ts, fix_type, nr_sv, h_accuracy, v_accuracy, latitude, longitude, height)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sid, ts) DO UPDATE SET
                    fix_type = COALESCE(EXCLUDED.fix_type, block_pvt_geodetic.fix_type),
                    nr_sv = COALESCE(EXCLUDED.nr_sv, block_pvt_geodetic.nr_sv),
                    h_accuracy = COALESCE(EXCLUDED.h_accuracy, block_pvt_geodetic.h_accuracy),
                    v_accuracy = COALESCE(EXCLUDED.v_accuracy, block_pvt_geodetic.v_accuracy),
                    latitude = COALESCE(EXCLUDED.latitude, block_pvt_geodetic.latitude),
                    longitude = COALESCE(EXCLUDED.longitude, block_pvt_geodetic.longitude),
                    height = COALESCE(EXCLUDED.height, block_pvt_geodetic.height)
            """, (sid, ts, fix_type, nr_sv, h_accuracy, v_accuracy, latitude, longitude, height))

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

    def _write_ntrip_client(self, sid: str, ts: datetime, ntrip: Dict[str, Any]) -> None:
        """Write to block_ntrip_client table."""
        cd_index = ntrip.get("cd_index", "NTR1")
        status = ntrip.get("status")
        error_code = ntrip.get("error_code")

        with self._conn.cursor() as cur:
            cur.execute("""
                INSERT INTO block_ntrip_client (sid, ts, cd_index, status, error_code)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (sid, ts, cd_index) DO UPDATE SET
                    status = EXCLUDED.status,
                    error_code = EXCLUDED.error_code
            """, (sid, ts, cd_index, status, error_code))

    def _write_satellite_tracking(
        self, sid: str, ts: datetime, satellites: Dict[str, Any]
    ) -> None:
        """Write to block_satellite_tracking table."""
        total = satellites.get("total")
        by_const = satellites.get("by_constellation", {})

        if total is None and not by_const:
            return

        try:
            with self._conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO block_satellite_tracking
                        (sid, ts, total, gps, glonass, galileo, beidou, sbas, qzss, irnss)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sid, ts) DO UPDATE SET
                        total = EXCLUDED.total,
                        gps = EXCLUDED.gps,
                        glonass = EXCLUDED.glonass,
                        galileo = EXCLUDED.galileo,
                        beidou = EXCLUDED.beidou,
                        sbas = EXCLUDED.sbas,
                        qzss = EXCLUDED.qzss,
                        irnss = EXCLUDED.irnss
                """, (
                    sid, ts, total,
                    by_const.get("GPS"),
                    by_const.get("GLONASS"),
                    by_const.get("Galileo"),
                    by_const.get("BeiDou"),
                    by_const.get("SBAS"),
                    by_const.get("QZSS"),
                    by_const.get("IRNSS")
                ))
        except Exception as e:
            self.logger.warning(f"block_satellite_tracking write failed: {e}")

    def _write_health_summary(
        self,
        sid: str,
        ts: datetime,
        overall_status: Optional[str],
        ports: Optional[Dict[str, Any]],
        status_details: Optional[str] = None,
    ) -> None:
        """Write to block_health_summary table.

        Persists the composite overall_status (computed from all metrics by the
        health parser) and port check results (FTP, HTTP, control).
        """
        ftp = ports.get("ftp", {}) if ports else {}
        http = ports.get("http", {}) if ports else {}
        control = ports.get("control", {}) if ports else {}

        try:
            with self._conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO block_health_summary
                        (sid, ts, overall_status, ftp_open, http_open, control_open,
                         ftp_port, http_port, control_port, status_details)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sid, ts) DO UPDATE SET
                        overall_status = EXCLUDED.overall_status,
                        ftp_open = EXCLUDED.ftp_open,
                        http_open = EXCLUDED.http_open,
                        control_open = EXCLUDED.control_open,
                        ftp_port = EXCLUDED.ftp_port,
                        http_port = EXCLUDED.http_port,
                        control_port = EXCLUDED.control_port,
                        status_details = EXCLUDED.status_details
                """, (
                    sid, ts, overall_status,
                    ftp.get("open"), http.get("open"), control.get("open"),
                    ftp.get("port"), http.get("port"), control.get("port"),
                    status_details,
                ))
        except Exception as e:
            self.logger.debug(f"block_health_summary write failed: {e}")

    def _build_status_details(
        self,
        metrics: Dict[str, Any],
        overall_status: Optional[str],
    ) -> Optional[str]:
        """Build a short description of what is causing non-healthy status.

        Scans individual metric statuses and port states to produce a
        comma-separated list like ``"FTP down, NTRIP error"``.

        Returns:
            Detail string, or None when status is healthy/unknown.
        """
        if overall_status not in ("critical", "warning"):
            return None

        problems: List[str] = []

        # Friendly labels for metric keys
        labels = {
            "power": "Voltage",
            "temperature": "Temperature",
            "cpu_load": "CPU",
            "disk": "Disk",
            "satellites": "Satellites",
            "ntrip_server": "NTRIP",
            "ntrip_client": "NTRIP client",
            "data_streams": "Data streams",
            "logging_sessions": "Logging",
        }

        for key, label in labels.items():
            metric = metrics.get(key)
            if not isinstance(metric, dict):
                continue
            status = metric.get("status", "").lower()
            if status in ("critical", "warning"):
                problems.append(label)

        # Port checks
        ports = metrics.get("ports")
        if isinstance(ports, dict):
            for name in ("ftp", "http", "control"):
                port_info = ports.get(name, {})
                if isinstance(port_info, dict) and not port_info.get("open", True):
                    detail = port_info.get("detail", "")
                    if detail == "refused":
                        problems.append(f"{name.upper()} refused (port forward missing?)")
                    else:
                        problems.append(f"{name.upper()} down")

        return ", ".join(problems) if problems else None

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

            # Position data (from metrics.position)
            position = sample.get("position")
            if isinstance(position, dict):
                lat = position.get("latitude")
                lon = position.get("longitude")
                height = position.get("height")
                # Handle both live format (h_accuracy_m) and historical format (h_accuracy)
                h_accuracy = position.get("h_accuracy_m") or position.get("h_accuracy")
                v_accuracy = position.get("v_accuracy_m") or position.get("v_accuracy")
                nr_sv = position.get("satellites_used")
                # Handle both live format (fix_mode) and historical format (fix_type)
                fix_mode = position.get("fix_mode") or position.get("fix_type")

                if lat is not None or lon is not None or nr_sv is not None:
                    with self._conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO block_pvt_geodetic (sid, ts, fix_type, nr_sv, latitude, longitude, height, h_accuracy, v_accuracy)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (sid, ts) DO UPDATE SET
                                fix_type = COALESCE(EXCLUDED.fix_type, block_pvt_geodetic.fix_type),
                                nr_sv = COALESCE(EXCLUDED.nr_sv, block_pvt_geodetic.nr_sv),
                                latitude = COALESCE(EXCLUDED.latitude, block_pvt_geodetic.latitude),
                                longitude = COALESCE(EXCLUDED.longitude, block_pvt_geodetic.longitude),
                                height = COALESCE(EXCLUDED.height, block_pvt_geodetic.height),
                                h_accuracy = COALESCE(EXCLUDED.h_accuracy, block_pvt_geodetic.h_accuracy),
                                v_accuracy = COALESCE(EXCLUDED.v_accuracy, block_pvt_geodetic.v_accuracy)
                        """, (station_id, ts, fix_mode, nr_sv, lat, lon, height, h_accuracy, v_accuracy))

            # Satellite visibility (aggregated count and constellation breakdown)
            satellites = sample.get("satellites")
            if isinstance(satellites, dict):
                nr_sv = satellites.get("total")
                # Handle both live format (by_constellation) and historical format (by_system)
                by_const = satellites.get("by_constellation", {}) or satellites.get("by_system", {})

                # Store total in block_pvt_geodetic if not already stored via position
                if nr_sv is not None and not sample.get("position"):
                    with self._conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO block_pvt_geodetic (sid, ts, nr_sv)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (sid, ts) DO UPDATE SET
                                nr_sv = COALESCE(EXCLUDED.nr_sv, block_pvt_geodetic.nr_sv)
                        """, (station_id, ts, nr_sv))

                # Store constellation breakdown in block_satellite_tracking if table exists
                if by_const:
                    try:
                        with self._conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO block_satellite_tracking
                                    (sid, ts, total, gps, glonass, galileo, beidou, sbas, qzss, irnss)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (sid, ts) DO UPDATE SET
                                    total = EXCLUDED.total,
                                    gps = EXCLUDED.gps,
                                    glonass = EXCLUDED.glonass,
                                    galileo = EXCLUDED.galileo,
                                    beidou = EXCLUDED.beidou,
                                    sbas = EXCLUDED.sbas,
                                    qzss = EXCLUDED.qzss,
                                    irnss = EXCLUDED.irnss
                            """, (
                                station_id, ts, nr_sv,
                                by_const.get("GPS"),
                                by_const.get("GLONASS"),
                                by_const.get("Galileo"),
                                by_const.get("BeiDou"),
                                by_const.get("SBAS"),
                                by_const.get("QZSS"),
                                by_const.get("IRNSS")
                            ))
                    except Exception as e:
                        # Table may not exist yet, log and continue
                        self.logger.debug(f"block_satellite_tracking not available: {e}")

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
