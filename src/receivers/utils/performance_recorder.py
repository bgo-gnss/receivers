"""Performance recording utilities for receivers package.

This module provides common performance metrics recording functionality
that is used across different receiver types to maintain consistency.
"""

import logging
from typing import Any, Dict


def record_performance_metrics(station_id: str, metrics: Dict[str, Any], logger: logging.Logger = None):
    """Record performance metrics for adaptive timeout system.

    This function abstracts the common logic for recording performance metrics
    across different receiver implementations, providing a consistent interface
    to the gps_parser performance tracking system.

    Args:
        station_id: Station identifier
        metrics: Performance metrics dictionary
        logger: Optional logger for error reporting
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    try:
        # Import here to avoid circular imports
        import sys
        sys.path.append('../gps_parser/src')
        import gps_parser

        # NOTE: record_performance_data() method not yet implemented in gps_parser.ConfigParser
        # This is reserved for future integration with adaptive timeout system
        # For now, performance metrics are logged locally in receivers package
        # parser = gps_parser.ConfigParser()
        # parser.record_performance_data(station_id, metrics)

        logger.debug(f"Performance metrics for {station_id}: {metrics}")

    except ImportError:
        logger.debug("gps_parser not available - skipping performance metrics")
    except Exception as e:
        logger.warning(f"Failed to record performance metrics for {station_id}: {e}")


def create_performance_metrics(
    success: bool,
    duration: float,
    bytes_downloaded: int = 0,
    connection_time: float = 0.0,
    **additional_metrics
) -> Dict[str, Any]:
    """Create standardized performance metrics dictionary.

    Args:
        success: Whether the operation was successful
        duration: Total operation duration in seconds
        bytes_downloaded: Number of bytes downloaded
        connection_time: Time spent connecting in seconds
        **additional_metrics: Additional metrics to include

    Returns:
        Standardized performance metrics dictionary
    """
    metrics = {
        'success': success,
        'duration': duration,
        'bytes_downloaded': bytes_downloaded,
        'connection_time': connection_time
    }

    # Add any additional metrics
    metrics.update(additional_metrics)

    return metrics