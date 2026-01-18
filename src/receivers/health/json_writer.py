"""JSON file writer for GPS receiver health data.

Saves health data to JSON files in status_1hr/json/:
- Live snapshots: STATION_YYYYMMDD_HHMMSS.json
- Daily timeseries: STATION_YYYYMMDD_health.json
- latest.json symlink for monitoring
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime, timezone


class HealthJSONWriter:
    """Write health data to JSON files."""

    def __init__(self, base_path: str, station_id: str):
        """Initialize JSON writer.

        Args:
            base_path: Base data directory path (e.g., '/data/2025/oct')
            station_id: Station identifier
        """
        self.base_path = Path(base_path)
        self.station_id = station_id.upper()
        self.logger = logging.getLogger(f"receivers.health.json.{station_id}")

    def write_health_data(self, health_data: Dict[str, Any]) -> Path:
        """Write health data to JSON file.

        Creates directory structure: base_path/station/status_1hr/json/
        Filename format: station_YYYYMMDD_HHMMSS.json

        Args:
            health_data: Health data dictionary following health-data-spec.md

        Returns:
            Path to written JSON file

        Raises:
            OSError: If file write fails
        """
        # Create json directory (unified location for all health JSON)
        json_dir = self.base_path / self.station_id / "status_1hr" / "json"
        json_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename with timestamp
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{self.station_id}_{timestamp}.json"
        filepath = json_dir / filename

        # Write JSON file
        try:
            with open(filepath, 'w') as f:
                json.dump(health_data, f, indent=2, default=str)

            self.logger.info(f"Wrote health data to {filepath}")
            return filepath

        except Exception as e:
            self.logger.error(f"Failed to write health JSON: {e}")
            raise

    def write_latest_symlink(self, json_path: Path) -> None:
        """Create/update 'latest.json' symlink pointing to most recent health file.

        Args:
            json_path: Path to the JSON file to link to
        """
        try:
            health_dir = json_path.parent
            latest_link = health_dir / "latest.json"

            # Remove existing symlink if present
            if latest_link.is_symlink() or latest_link.exists():
                latest_link.unlink()

            # Create new symlink
            latest_link.symlink_to(json_path.name)
            self.logger.debug(f"Updated latest.json symlink -> {json_path.name}")

        except Exception as e:
            self.logger.warning(f"Failed to create latest.json symlink: {e}")

    def read_latest_health(self) -> Dict[str, Any]:
        """Read most recent health data from latest.json symlink.

        Returns:
            Health data dictionary or empty dict if not found
        """
        health_dir = self.base_path / self.station_id / "status_1hr" / "health"
        latest_link = health_dir / "latest.json"

        if not latest_link.exists():
            self.logger.debug("No latest.json found")
            return {}

        try:
            with open(latest_link, 'r') as f:
                return json.load(f)

        except Exception as e:
            self.logger.error(f"Failed to read latest health data: {e}")
            return {}

    # ========================================================================
    # Daily Time-Series Methods (v2.0)
    # ========================================================================

    def write_daily_health_data(
        self,
        health_data: Dict[str, Any],
        date: datetime,
        force: bool = False
    ) -> Optional[Path]:
        """Write daily time-series health data to JSON file (v2.0 format).

        Creates directory structure: base_path/station/status_1hr/json/
        Filename format: station_YYYYMMDD_health.json

        Args:
            health_data: Daily health data following v2.0 schema
            date: Date for this health data
            force: Force overwrite even if source data unchanged

        Returns:
            Path to written JSON file, or None if skipped (no changes)

        Raises:
            OSError: If file write fails
        """
        # Create json directory
        json_dir = self.base_path / self.station_id / "status_1hr" / "json"
        json_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename with date only
        date_str = date.strftime("%Y%m%d")
        filename = f"{self.station_id}_{date_str}_health.json"
        filepath = json_dir / filename

        # Check if file exists and needs updating
        if filepath.exists() and not force:
            needs_update = self._check_needs_update(filepath, health_data)
            if not needs_update:
                self.logger.info(f"Skipping {filename}: No source changes detected")
                return None
            else:
                self.logger.info(f"Updating {filename}: New source data detected")

        # Write JSON file
        try:
            with open(filepath, 'w') as f:
                json.dump(health_data, f, indent=2, default=str)

            self.logger.info(f"Wrote daily health data to {filepath}")
            self.logger.info(f"  Samples: {health_data.get('sample_count', 0)}")
            self.logger.info(f"  Completeness: {health_data.get('extraction_metadata', {}).get('data_quality', {}).get('completeness', 0)}%")

            return filepath

        except Exception as e:
            self.logger.error(f"Failed to write daily health JSON: {e}")
            raise

    def write_daily_latest_symlink(self, json_path: Path) -> None:
        """Create/update 'latest.json' symlink in json/ directory.

        Args:
            json_path: Path to the JSON file to link to
        """
        try:
            json_dir = json_path.parent
            latest_link = json_dir / "latest.json"

            # Remove existing symlink if present
            if latest_link.is_symlink() or latest_link.exists():
                latest_link.unlink()

            # Create new symlink
            latest_link.symlink_to(json_path.name)
            self.logger.debug(f"Updated json/latest.json symlink -> {json_path.name}")

        except Exception as e:
            self.logger.warning(f"Failed to create latest.json symlink: {e}")

    def _check_needs_update(
        self,
        existing_file: Path,
        new_data: Dict[str, Any]
    ) -> bool:
        """Check if existing daily health file needs updating.

        Compares source file information to determine if re-extraction needed.

        Args:
            existing_file: Path to existing JSON file
            new_data: New health data dictionary

        Returns:
            True if file needs updating, False if unchanged
        """
        try:
            with open(existing_file, 'r') as f:
                existing_data = json.load(f)

            # Compare file tracking data
            existing_files = existing_data.get('data_files', {}).get('status_1hr', {}).get('files', [])
            new_files = new_data.get('data_files', {}).get('status_1hr', {}).get('files', [])

            # Check if number of files changed
            if len(existing_files) != len(new_files):
                return True

            # Check if any file status changed from 'not_downloaded' to 'included'
            existing_filenames = {f['filename']: f['status'] for f in existing_files}
            new_filenames = {f['filename']: f['status'] for f in new_files}

            for filename, new_status in new_filenames.items():
                existing_status = existing_filenames.get(filename)
                if existing_status != new_status:
                    return True

            # No changes detected
            return False

        except Exception as e:
            self.logger.warning(f"Error comparing existing file, will overwrite: {e}")
            return True  # If we can't read existing, safer to update

    def read_daily_health(self, date: datetime) -> Optional[Dict[str, Any]]:
        """Read daily health data for a specific date.

        Args:
            date: Date to read data for

        Returns:
            Health data dictionary or None if not found
        """
        json_dir = self.base_path / self.station_id / "status_1hr" / "json"
        date_str = date.strftime("%Y%m%d")
        filename = f"{self.station_id}_{date_str}_health.json"
        filepath = json_dir / filename

        if not filepath.exists():
            self.logger.debug(f"No daily health file found for {date.date()}")
            return None

        try:
            with open(filepath, 'r') as f:
                return json.load(f)

        except Exception as e:
            self.logger.error(f"Failed to read daily health data: {e}")
            return None
