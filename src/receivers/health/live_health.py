"""Live health data gathering — composable function for status + health commands.

Extracts the comprehensive health check logic (receiver status, file checks,
NTRIP/RTK checks) into a reusable function used by both ``cmd_health`` (live mode)
and ``cmd_status`` (thin wrapper).
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def gather_comprehensive_health(
    station_id: str,
    station_config: Dict[str, Any],
    receiver,
    include_files: bool = True,
    include_ntrip: bool = True,
) -> Dict[str, Any]:
    """Gather all live health data: receiver metrics + file status + NTRIP.

    Args:
        station_id: Station identifier (uppercase).
        station_config: Station configuration dictionary.
        receiver: Receiver instance (from create_receiver).
        include_files: Include archive file system checks.
        include_ntrip: Include NTRIP/RTK stream health checks.

    Returns:
        Health dictionary enriched with file_status, processing_24hr, and rtk keys.
    """
    # Base health from receiver (TCP/FTP probes, metrics extraction)
    health = receiver.get_health_status()

    # NTRIP / RTK checks
    if include_ntrip:
        try:
            from ..monitoring.ntrip_client import check_ntrip_status
            from ..config.receivers_config import ReceiversConfig

            receivers_cfg = ReceiversConfig()
            ntrip_status = check_ntrip_status(
                station_id, receivers_cfg, station_config
            )
            if ntrip_status:
                health["rtk"] = {
                    "status": ntrip_status.overall_status,
                    "message": ntrip_status.message,
                    "host": ntrip_status.host,
                    "mountpoints": [
                        {
                            "name": mp.mountpoint,
                            "active": mp.is_active,
                            "data_rate": mp.data_rate_bps,
                            "error": mp.error_message,
                        }
                        for mp in ntrip_status.mountpoints
                    ],
                }

                # Bridge caster NTRIP result to metrics so db_writer persists it.
                # The caster check is the authoritative source — if the mountpoint
                # is live on ntrcaster.vedur.is, the RTK service is working.
                # This overrides any receiver-provided data (PolaRX5 SBF blocks
                # show the receiver's outbound connection, but the caster is what
                # downstream services actually consume).
                metrics = health.setdefault("metrics", {})
                if ntrip_status.mountpoints:
                    mp = ntrip_status.mountpoints[0]
                    status_str = "connected" if mp.is_active else "error"
                    metrics["ntrip_server"] = {
                        "cd_index": mp.mountpoint,
                        "status": status_str,
                        "error_code": mp.error_message,
                    }
        except Exception as e:
            logger.debug(f"RTK status check skipped for {station_id}: {e}")

    # File system checks
    if include_files:
        try:
            from ..health.file_tracker import ArchiveFileChecker, ProcessingStatusChecker

            checker = ArchiveFileChecker()
            health["file_status"] = {}

            # Check daily files (15s_24hr)
            stats = checker.check_file_status(station_id, "15s_24hr", days_back=7)
            if stats:
                health["file_status"]["15s_24hr"] = stats

            # Check hourly files (1Hz_1hr)
            stats = checker.check_file_status(station_id, "1Hz_1hr", days_back=1)
            if stats and stats.get("files_found", 0) > 0:
                health["file_status"]["1Hz_1hr"] = stats

            # Check RINEX files if directory exists
            stats = checker.check_file_status(station_id, "15s_24hr_rinex", days_back=7)
            if stats and stats.get("dir_exists", False):
                health["file_status"]["15s_24hr_rinex"] = stats

            stats = checker.check_file_status(station_id, "1Hz_1hr_rinex", days_back=1)
            if stats and stats.get("dir_exists", False) and stats.get("files_found", 0) > 0:
                health["file_status"]["1Hz_1hr_rinex"] = stats

            # Check high-rate files if directory exists
            stats = checker.check_file_status(station_id, "20Hz_1hr", days_back=1)
            if stats and stats.get("dir_exists", False) and stats.get("files_found", 0) > 0:
                health["file_status"]["20Hz_1hr"] = stats

            stats = checker.check_file_status(station_id, "50Hz_1hr", days_back=1)
            if stats and stats.get("dir_exists", False) and stats.get("files_found", 0) > 0:
                health["file_status"]["50Hz_1hr"] = stats

            # Check 24hr processing status
            proc_checker = ProcessingStatusChecker()
            proc_result = proc_checker.check_24hr_processing(station_id)
            if proc_result.get("file_exists", False):
                health["processing_24hr"] = proc_result

        except Exception as e:
            logger.debug(f"File status checks skipped for {station_id}: {e}")

    return health
