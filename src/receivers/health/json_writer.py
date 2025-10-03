"""JSON file writer for GPS receiver health data.

Saves health data to status_1hr/health/ directory in standardized JSON format.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any
from datetime import datetime


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

        Creates directory structure: base_path/station/status_1hr/health/
        Filename format: station_YYYYMMDD_HHMMSS.json

        Args:
            health_data: Health data dictionary following health-data-spec.md

        Returns:
            Path to written JSON file

        Raises:
            OSError: If file write fails
        """
        # Create health directory
        health_dir = self.base_path / self.station_id / "status_1hr" / "health"
        health_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename with timestamp
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.station_id}_{timestamp}.json"
        filepath = health_dir / filename

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
