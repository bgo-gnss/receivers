"""Dashboard and monitoring constants - single source of truth.

Centralizes color schemes and threshold constants used across Grafana
dashboards, CLI output, and health monitoring code.

Dashboard display thresholds are loaded from database.cfg [checked] and
[dashboard_satellites] sections with fallback to the defaults below.
For metric evaluation thresholds (voltage, temperature, etc.), use
metrics.py ThresholdConfig.
"""

import configparser
import os
from pathlib import Path


def _load_cfg_value(section: str, key: str, default: int) -> int:
    """Load an int from database.cfg, returning default if missing."""
    cfg_path = (
        Path(
            os.environ.get("GPS_CONFIG_PATH", os.path.expanduser("~/.config/gpsconfig"))
        )
        / "database.cfg"
    )
    if not cfg_path.exists():
        return default
    parser = configparser.ConfigParser()
    parser.read(str(cfg_path))
    try:
        return parser.getint(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
        return default


class Colors:
    """Grafana dashboard color scheme (hex colors)."""

    GREEN = "#73BF69"
    YELLOW = "#FADE2A"
    RED = "#F2495C"
    BLUE = "#5794F2"
    GREY = "#808080"
    ORANGE = "#FF9830"
    DARK_GREEN = "#37872D"


class CheckedThresholds:
    """Thresholds for "Last Checked" display (seconds).

    Used in Grafana dashboard to color-code how recently a station
    was checked. Values loaded from database.cfg [checked] section.
    """

    GREEN_MAX = _load_cfg_value("checked", "green_max", 7200)
    YELLOW_MAX = _load_cfg_value("checked", "yellow_max", 86400)


class SatelliteThresholds:
    """Standardized satellite count thresholds for dashboard display.

    These are separate from the alerting thresholds in metrics.py
    (warning=8, critical=4). Dashboard display uses higher thresholds
    for visual color coding. Values loaded from database.cfg
    [dashboard_satellites] section.
    """

    GREEN_MIN = _load_cfg_value("dashboard_satellites", "green_min", 16)
    YELLOW_MIN = _load_cfg_value("dashboard_satellites", "yellow_min", 8)
    # Critical: < YELLOW_MIN satellites (red)
