"""Import JSON health data to PostgreSQL database.

Reads v2.0 daily health JSON files and imports timeseries data to the
checkcomm table for Grafana visualization.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class HealthJsonImporter:
    """Import health JSON files to PostgreSQL database."""

    def __init__(self, connection_string: Optional[str] = None):
        """Initialize importer.

        Args:
            connection_string: PostgreSQL connection string
                             (postgresql://user:pass@host:port/dbname)
                             If None, uses environment variables
        """
        self.connection_string = connection_string
        self._conn = None

    def connect(self, database: str = "gps_health") -> bool:
        """Connect to PostgreSQL database.

        Args:
            database: Database name (default: gps_health)

        Returns:
            True if connection successful
        """
        try:
            import psycopg2

            if self.connection_string:
                self._conn = psycopg2.connect(self.connection_string)
            else:
                import os
                db_host = os.getenv("POSTGRES_HOST", "localhost")
                db_port = os.getenv("POSTGRES_PORT", "5432")
                db_name = os.getenv("POSTGRES_DB", database)
                db_user = os.getenv("POSTGRES_USER", os.getenv("USER", "postgres"))
                db_pass = os.getenv("POSTGRES_PASSWORD", "")

                self._conn = psycopg2.connect(
                    host=db_host,
                    port=db_port,
                    database=db_name,
                    user=db_user,
                    password=db_pass,
                )

            logger.info(f"Connected to PostgreSQL database")
            return True

        except ImportError:
            logger.error("psycopg2 not installed - run: pip install psycopg2-binary")
            return False
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            return False

    def close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _parse_timestamp(self, ts_str: str) -> datetime:
        """Parse ISO8601 timestamp string."""
        # Handle Z suffix
        ts_str = ts_str.rstrip('Z')
        return datetime.fromisoformat(ts_str)

    def _determine_status(self, voltage: Optional[float]) -> str:
        """Determine overall status based on voltage.

        Thresholds:
        - OK: 11.8V - 15.0V
        - Warning: < 11.8V or > 15.0V
        - Critical: < 11.0V
        """
        if voltage is None:
            return "unknown"

        if voltage < 11.0:
            return "critical"
        elif voltage < 11.8 or voltage > 15.0:
            return "warning"
        else:
            return "healthy"

    def import_json_file(
        self,
        json_path: Path,
        resolution: str = "minute"
    ) -> Tuple[int, int]:
        """Import a single JSON health file.

        Args:
            json_path: Path to JSON file
            resolution: "minute" (all samples) or "hourly" (aggregate)

        Returns:
            Tuple of (rows_imported, rows_skipped)
        """
        if not self._conn:
            if not self.connect():
                return 0, 0

        try:
            with open(json_path, 'r') as f:
                data = json.load(f)

            station_id = data.get("station_id", "UNKN")
            receiver_type = data.get("receiver_type", "Unknown")
            timeseries = data.get("timeseries", [])

            if not timeseries:
                logger.warning(f"No timeseries data in {json_path.name}")
                return 0, 0

            logger.info(
                f"Importing {json_path.name}: {len(timeseries)} samples "
                f"for {station_id} ({receiver_type})"
            )

            rows_imported = 0
            rows_skipped = 0

            with self._conn.cursor() as cur:
                for sample in timeseries:
                    try:
                        timestamp = self._parse_timestamp(sample["time"])

                        # Extract metrics
                        voltage = sample.get("voltage", {}).get("value")
                        temperature = sample.get("temperature", {}).get("value")
                        cpu_load = sample.get("cpu_load", {}).get("value")
                        disk_usage = sample.get("disk_usage", {}).get("value")
                        satellites = sample.get("satellites", {})

                        # Build recv_metrics JSONB
                        recv_metrics = {
                            "power": {"voltage": voltage, "unit": "V"} if voltage else {},
                            "temperature": {"value": temperature, "unit": "C"} if temperature else {},
                            "cpu_load": {"percent": cpu_load} if cpu_load else {},
                            "disk": {"usage_percent": disk_usage} if disk_usage else {},
                            "satellites": satellites,
                        }

                        # Determine status
                        overall_status = self._determine_status(voltage)

                        # Insert with UPSERT
                        cur.execute("""
                            INSERT INTO checkcomm (
                                sid, timestamp, recv_temp, recv_volt,
                                recv_metrics, overall_status
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s
                            )
                            ON CONFLICT (sid, timestamp)
                            DO UPDATE SET
                                recv_temp = EXCLUDED.recv_temp,
                                recv_volt = EXCLUDED.recv_volt,
                                recv_metrics = EXCLUDED.recv_metrics,
                                overall_status = EXCLUDED.overall_status
                        """, (
                            station_id,
                            timestamp,
                            temperature,
                            voltage,
                            json.dumps(recv_metrics),
                            overall_status,
                        ))

                        rows_imported += 1

                    except Exception as e:
                        logger.debug(f"Skipped sample: {e}")
                        rows_skipped += 1

            self._conn.commit()
            logger.info(
                f"Imported {rows_imported} rows, skipped {rows_skipped} "
                f"from {json_path.name}"
            )
            return rows_imported, rows_skipped

        except Exception as e:
            logger.error(f"Failed to import {json_path}: {e}")
            if self._conn:
                self._conn.rollback()
            return 0, 0

    def import_directory(
        self,
        json_dir: Path,
        station_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Tuple[int, int, int]:
        """Import all JSON files from a directory.

        Args:
            json_dir: Directory containing JSON health files
            station_id: Optional station filter
            start_date: Optional start date filter
            end_date: Optional end date filter

        Returns:
            Tuple of (files_processed, total_rows, total_skipped)
        """
        if not json_dir.exists():
            logger.error(f"Directory not found: {json_dir}")
            return 0, 0, 0

        # Find all health JSON files
        pattern = f"{station_id}_*_health.json" if station_id else "*_health.json"
        json_files = sorted(json_dir.glob(pattern))

        if not json_files:
            logger.warning(f"No health JSON files found in {json_dir}")
            return 0, 0, 0

        # Filter by date if specified
        if start_date or end_date:
            filtered_files = []
            for f in json_files:
                # Extract date from filename: STATION_YYYYMMDD_health.json
                try:
                    parts = f.stem.split('_')
                    if len(parts) >= 2:
                        file_date = datetime.strptime(parts[1], "%Y%m%d")
                        if start_date and file_date < start_date:
                            continue
                        if end_date and file_date > end_date:
                            continue
                        filtered_files.append(f)
                except ValueError:
                    continue
            json_files = filtered_files

        logger.info(f"Found {len(json_files)} JSON files to import")

        total_rows = 0
        total_skipped = 0
        files_processed = 0

        for json_file in json_files:
            rows, skipped = self.import_json_file(json_file)
            total_rows += rows
            total_skipped += skipped
            files_processed += 1

        logger.info(
            f"Import complete: {files_processed} files, "
            f"{total_rows} rows imported, {total_skipped} skipped"
        )

        return files_processed, total_rows, total_skipped

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


def import_station_json(
    station_id: str,
    json_dir: Optional[Path] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    database: str = "gps_health",
) -> Tuple[int, int, int]:
    """Convenience function to import JSON data for a station.

    Args:
        station_id: Station identifier
        json_dir: Directory with JSON files (auto-detected if None)
        start_date: Optional start date
        end_date: Optional end date
        database: Database name

    Returns:
        Tuple of (files_processed, total_rows, total_skipped)
    """
    # Auto-detect json_dir from data path if not provided
    if json_dir is None:
        from ..config.receivers_config import get_receivers_config
        config = get_receivers_config()
        data_prepath = config.get_data_prepath()

        # Try current month first
        now = datetime.now()
        month_abbr = now.strftime("%b").lower()
        year = now.strftime("%Y")

        json_dir = Path(data_prepath) / year / month_abbr / station_id / "status_1hr" / "json"

        if not json_dir.exists():
            logger.error(f"JSON directory not found: {json_dir}")
            return 0, 0, 0

    with HealthJsonImporter() as importer:
        importer.connect(database=database)
        return importer.import_directory(
            json_dir,
            station_id=station_id,
            start_date=start_date,
            end_date=end_date,
        )
