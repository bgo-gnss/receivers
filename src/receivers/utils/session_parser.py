"""Session parsing utilities for receivers package.

This module provides common session parsing functionality that is used
across different receiver types to maintain consistency.
"""

from typing import Dict, Tuple


def parse_session_parameters(session: str) -> Tuple[str, str, str]:
    """Parse session string into component parameters.

    This function abstracts the common logic for parsing session strings
    like '15s_24hr' or '1Hz_1hr' into their constituent parts.

    Args:
        session: Session string (e.g., '15s_24hr', '1Hz_1hr')

    Returns:
        Tuple of (acquisition_frequency, file_frequency, gtimes_frequency)
        - acquisition_frequency: Data acquisition rate (e.g., "15s", "1Hz")
        - file_frequency: File creation frequency (e.g., "24hr", "1hr")
        - gtimes_frequency: gtimes-compatible frequency (e.g., "1D", "1H")
    """
    # Extract session parameters
    parts = session.split("_")
    if len(parts) >= 2:
        acquisition_freq = parts[0]  # e.g., "15s", "1Hz"
        file_freq = parts[1]  # e.g., "24hr", "1hr"
    else:
        acquisition_freq = "15s"
        file_freq = "24hr"

    # Map frequency to gtimes format
    frequency_mapping = {
        "24hr": "1D",  # Daily files
        "1hr": "1H",  # Hourly files
    }
    gtimes_freq = frequency_mapping.get(file_freq, "1D")

    return acquisition_freq, file_freq, gtimes_freq


def get_frequency_mapping() -> Dict[str, str]:
    """Get standard frequency mapping for session types.

    Returns:
        Dictionary mapping file frequencies to gtimes frequencies
    """
    return {
        "24hr": "1D",  # Daily files
        "1hr": "1H",  # Hourly files
    }
