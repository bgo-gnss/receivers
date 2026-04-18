"""Classify download error messages into categories.

Maps raw error messages from FTP, network, and receiver failures to
standardized categories for dashboards and backoff logic.
"""

import re
from typing import Tuple

# Ordered list of (compiled_pattern, category).  First match wins.
_PATTERNS: list[Tuple[re.Pattern[str], str]] = [
    # Dead FTP connection (sendall on None socket)
    (
        re.compile(r"NoneType.*sendall|has no attribute.*sendall", re.I),
        "dead_connection",
    ),
    (
        re.compile(r"broken pipe|connection reset|connection aborted", re.I),
        "dead_connection",
    ),
    # Stall / watchdog timeout
    (re.compile(r"stall|watchdog|no data received|0%.*kill", re.I), "stall_timeout"),
    (re.compile(r"timed?\s*out|timeout", re.I), "stall_timeout"),
    # File not found on receiver
    (re.compile(r"550|not found|no such file", re.I), "file_not_found"),
    # Host unreachable / ping failed
    (
        re.compile(r"unreachable|ping.*fail|no route|network is down", re.I),
        "unreachable",
    ),
    (re.compile(r"connection refused|connect fail", re.I), "unreachable"),
    # Auth failure
    (
        re.compile(r"530|login.*fail|auth|permission denied|access denied", re.I),
        "auth_failed",
    ),
    # Disk problems
    (re.compile(r"disk.*full|no space|unmounted|disk.*error", re.I), "disk_error"),
    # Validation failures
    (
        re.compile(r"validation fail|corrupt|invalid.*file|size mismatch", re.I),
        "validation_failed",
    ),
]


def classify_download_error(message: str) -> str:
    """Classify an error message into a standardized category.

    Args:
        message: Raw error message string from a download failure.

    Returns:
        One of: 'dead_connection', 'stall_timeout', 'file_not_found',
        'unreachable', 'auth_failed', 'disk_error', 'validation_failed',
        'unknown'.
    """
    if not message:
        return "unknown"
    for pattern, category in _PATTERNS:
        if pattern.search(message):
            return category
    return "unknown"
