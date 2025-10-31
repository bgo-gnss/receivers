"""Trimble NetR5 receiver implementation.

NetR5 receivers use older firmware with specific behavioral differences from NetR9:
- CACHEDIR prefix in download URLs (e.g., /CACHEDIR656804383/download/Internal/)
- Some firmware versions pad station IDs with underscores to 10 characters
- Directory listings use standard /Internal/ paths (CACHEDIR only for downloads)
- Commonly used by NATT (Landmælingar Íslands) stations requiring HTTP Basic Auth

Implementation Note:
    NetR5 uses the same HTTP API as NetR9 with firmware-specific quirks.
    The NetR9HTTPDownloader auto-detects whether a receiver uses CACHEDIR prefix,
    making NetR5 largely compatible with NetR9 implementation.
"""

import logging
from typing import Any, Dict

from .netr9 import NetR9


class NetR5(NetR9):
    """Trimble NetR5 receiver implementation.

    NetR5 receivers are older Trimble receivers with firmware-specific behaviors:

    CACHEDIR Auto-discovery:
        NetR5 firmware uses a CACHEDIR prefix in download URLs that must be
        auto-discovered. The NetR9HTTPDownloader automatically:
        1. Tests standard /Internal/ path
        2. If that fails, fetches root page to find CACHEDIR{number} links
        3. Caches discovered path for subsequent downloads

        Example discovered URLs:
        - Directory listing: /prog/show?directory&path=/Internal/202510/15s_24hr
        - File download: /CACHEDIR656804383/download/Internal/202510/15s_24hr/FILE.T02

    Underscore Padding Firmware Bug:
        Some NetR5 receivers pad station IDs with underscores to 10 characters.
        Configure with: receiver_firmware_underscore_pad = true

        Example: ISAF (4 chars) → ISAF______ (10 chars)
        Filename: ISAF______202510130000a.T02

    HTTP Basic Authentication:
        NetR5 receivers commonly require HTTP Basic Auth (especially NATT stations).
        Configure with: receiver_user and receiver_pwd in stations.cfg

        Example:
            receiver_type = NetR5
            receiver_user = LMI
            receiver_pwd = password123

    NATT Stations:
        NetR5 receivers are commonly used by NATT (Landmælingar Íslands):
        - ISAF: Primary NetR5 station with underscore padding
        - All NATT stations require HTTP Basic Auth
        - Non-standard ports (typically 7000, ISAF uses 80)

    Supported Sessions:
        - 15s_24hr: Daily 15-second data files
        - 1Hz_1hr: Hourly 1Hz data files

    See Also:
        - NetR9: Base class with full implementation
        - NetR9HTTPDownloader: HTTP client with CACHEDIR auto-discovery
        - TrimbleHTTPClient: HTTP Basic Auth support
    """

    def __init__(self, station_id: str, station_info: Dict[str, Any]):
        """Initialize NetR5 receiver.

        Args:
            station_id: Station identifier
            station_info: Station configuration dictionary with NetR5-specific options:
                - receiver_firmware_underscore_pad: Enable underscore padding (optional)
                - receiver_user: HTTP Basic Auth username (optional)
                - receiver_pwd: HTTP Basic Auth password (optional)
        """
        # Initialize using NetR9 base class
        super().__init__(station_id, station_info)

        # Update logger message
        self.logger.info(f"Initialized NetR5 receiver for {self.station_id}")

        # Log NetR5-specific configuration if present
        receiver_config = station_info.get("receiver", {})
        if receiver_config.get("firmware_underscore_pad"):
            self.logger.info(f"NetR5 underscore padding enabled for {station_id}")
        if receiver_config.get("user"):
            self.logger.info(f"HTTP Basic Auth enabled for {station_id}")

    def get_station_info(self) -> Dict[str, Any]:
        """Get station information and configuration.

        Returns:
            Dictionary with station information (receiver_type updated to NetR5)
        """
        info = super().get_station_info()
        # Override receiver_type to correctly identify as NetR5
        info["receiver_type"] = "NetR5"
        return info

    def download_data(self, *args, **kwargs) -> Dict[str, Any]:
        """Download data from NetR5 receiver.

        Uses NetR9 implementation with automatic CACHEDIR detection.
        See NetR9.download_data() for full documentation.

        Returns:
            Dictionary with download results (receiver_type updated to NetR5)
        """
        result = super().download_data(*args, **kwargs)
        # Override receiver_type to correctly identify as NetR5
        result["receiver_type"] = "NetR5"
        return result

    def get_health_status(self) -> Dict[str, Any]:
        """Get comprehensive health status from NetR5 receiver.

        Uses NetR9 implementation.
        See NetR9.get_health_status() for full documentation.

        Returns:
            Dictionary with health metrics (receiver_type updated to NetR5)
        """
        health = super().get_health_status()
        # Override receiver_type to correctly identify as NetR5
        health["receiver_type"] = "NetR5"
        return health
