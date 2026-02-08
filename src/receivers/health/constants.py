"""Dashboard and monitoring constants - single source of truth.

Centralizes color schemes and threshold constants used across Grafana
dashboards, CLI output, and health monitoring code.

The threshold values here are for dashboard display only. For metric
evaluation thresholds (voltage, temperature, etc.), use metrics.py
ThresholdConfig which supports per-receiver-type overrides via YAML.
"""


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
    was checked.
    """

    GREEN_MAX = 7200  # 2 hours - recently checked
    YELLOW_MAX = 86400  # 24 hours - stale


class SatelliteThresholds:
    """Standardized satellite count thresholds.

    Used across overview and detail dashboards for consistent display.
    The metrics.py ThresholdConfig uses warning=8, critical=4 for
    Icinga alerting. Dashboard display uses higher thresholds for
    visual color coding.

    Standardized on 16 for green status per user decision.
    """

    GREEN_MIN = 16  # Healthy: >= 16 satellites (green)
    YELLOW_MIN = 8  # Warning: 8-15 satellites (yellow)
    # Critical: < 8 satellites (red)
